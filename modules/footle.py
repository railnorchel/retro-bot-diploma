# modules/footle.py

import datetime
import random
import csv
import logging
from pathlib import Path

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from bot import bot
from modules.database import add_rating, get_rating, init_db
from modules.solo_guess import start_solo_game

logger = logging.getLogger(__name__)
router = Router()

# --- Константы и загрузка данных ---
CSV_PATH = Path(__file__).parent.parent / "data" / "footle_list.csv"
MAX_ATTEMPTS = 6
GREEN, YELLOW, GRAY, BLACK = "🟩", "🟨", "⬜", "⬛"

sessions: dict[int, dict] = {}
RUSSIAN_WORDS: list[str] = []
VALID_WORDS: set[str] = set()

with open(CSV_PATH, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        en, ru = row["en"].strip().lower(), row["ru"].strip().lower()
        if en:
            VALID_WORDS.add(en)
        if ru:
            VALID_WORDS.add(ru)
            if ' ' not in ru:
                RUSSIAN_WORDS.append(ru)

# --- Инициализация БД на старте ---
@router.startup()
async def on_startup():
    await init_db()

# --- Утилиты для отрисовки ---
def make_hint(guess: str, target: str) -> str:
    hint = [GRAY] * len(target)
    target_list = list(target)
    guess_list = list(guess)
    # зелёные
    for i in range(len(target)):
        if i < len(guess_list) and guess_list[i] == target_list[i]:
            hint[i] = GREEN
            target_list[i] = None
            guess_list[i] = None
    # жёлтые
    for i in range(len(target)):
        if guess_list[i] is not None and guess_list[i] in target_list:
            hint[i] = YELLOW
            target_list[target_list.index(guess_list[i])] = None
            guess_list[i] = None
    return "".join(hint)

def render_board(guesses: list[str], target: str) -> str:
    lines = []
    word_len = len(target)
    for guess in guesses:
        hint_emojis = make_hint(guess, target)
        parts = [f"{hint_emojis[i]}{guess[i].upper()}" for i in range(word_len)]
        lines.append(" ".join(parts))
    remaining = MAX_ATTEMPTS - len(guesses)
    placeholder = " ".join(BLACK * word_len)
    for _ in range(remaining):
        lines.append(placeholder)
    return "\n\n".join(lines)

# --- Клавиатуры ---
def get_giveup_keyboard() -> types.InlineKeyboardMarkup:
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    builder.button(text="🏳️ Сдаться", callback_data="giveup_footle")
    return builder.as_markup()

def get_after_game_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    Reply-клавиатура после окончания Footle:
      🔄 Новая игра (Footle) | 🎯 Угадай игрока (Solo)
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔄 Новая игра (Footle)"),
                KeyboardButton(text="🎯 Угадай игрока (Solo)")
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# --- Старт игры ---
@router.message(Command("footle"))
async def cmd_footle(message: types.Message):
    uid = message.from_user.id
    if uid in sessions:
        await message.answer(
            "У вас уже активная игра. Завершите её, чтобы начать новую.",
            reply_markup=get_giveup_keyboard()
        )
        return

    random.seed(datetime.datetime.now())
    word = random.choice(RUSSIAN_WORDS)
    board = render_board([], word)

    sent = await message.answer(
        f"⚽️ <b>Footle</b> — угадайте фамилию из {len(word)} букв.\n\n"
        f"<code>{board}</code>\n\nВведите первую попытку:",
        parse_mode="HTML",
        reply_markup=get_giveup_keyboard()
    )
    sessions[uid] = {"word": word, "guesses": [], "message_id": sent.message_id}

@router.callback_query(F.data == "giveup_footle")
async def handle_giveup_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    session = sessions.pop(uid, None)
    if not session:
        await callback.answer("Игра не найдена.", show_alert=True)
        return

    word = session["word"]
    await callback.answer()

    text = (
        f"🏳️ Вы сдались. Загаданное слово было: <b>{word.upper()}</b>\n\nЧто дальше?"
    )

    await callback.message.answer(
        text=text,
        parse_mode="HTML",
        reply_markup=get_after_game_reply_keyboard()
    )


# --- Обработка попыток угадывания ---
@router.message(lambda msg: msg.from_user.id in sessions)
async def handle_guess(message: types.Message):
    uid = message.from_user.id
    session = sessions.get(uid)
    if not session:
        return

    guess = message.text.strip().lower()
    word = session["word"]

    if len(guess) != len(word) or not guess.isalpha() or guess not in VALID_WORDS:
        return

    try:
        await bot.delete_message(uid, message.message_id)
    except TelegramBadRequest:
        pass

    session["guesses"].append(guess)
    board = render_board(session["guesses"], word)
    is_win = (guess == word)
    is_over = (len(session["guesses"]) >= MAX_ATTEMPTS)

    # --- ИЗМЕНЕНИЯ НАЧИНАЮТСЯ ЗДЕСЬ ---

    if is_win or is_over:
        del sessions[uid]

        # 1. Редактируем игровое поле, убирая клавиатуру "Сдаться"
        try:
            await bot.edit_message_text(
                chat_id=uid,
                message_id=session["message_id"],
                text=f"<code>{board}</code>",
                parse_mode="HTML",
                reply_markup=None
            )
        except TelegramBadRequest:
            pass

        # 2. Формируем текст и отправляем НОВОЕ сообщение с Reply-клавиатурой
        if is_win:
            await add_rating(uid, 10000)
            pts = await get_rating(uid)
            final_text = (
                f"🎉 <b>ПОБЕДА!</b> Угадали «{word.upper()}» за "
                f"{len(session['guesses'])} ходов!\n"
                f"🏆 +10000 очков. Баланс: {pts}.\n\nЧто дальше?"
            )
        else: #
            final_text = (
                f"⛔️ <b>Поражение.</b> Ходы закончились. Слово: "
                f"<b>{word.upper()}</b>\n\nЧто дальше?"
            )

        await message.answer(
            text=final_text,
            parse_mode="HTML",
            reply_markup=get_after_game_reply_keyboard()
        )

    else:
        remaining = MAX_ATTEMPTS - len(session["guesses"])
        text = (
            f"⚽️ <b>Footle</b> — угадайте фамилию из {len(word)} букв.\n\n"
            f"<code>{board}</code>\n\n"
            f"Осталось ходов: <b>{remaining}</b>. Ваш следующий ход?"
        )
        reply = get_giveup_keyboard()

        try:
            await bot.edit_message_text(
                text=text,
                chat_id=uid,
                message_id=session["message_id"],
                parse_mode="HTML",
                reply_markup=reply
            )
        except TelegramBadRequest:
            await message.answer(text, parse_mode="HTML", reply_markup=reply)



# --- Ловим нажатия Reply-кнопок после игры ---
@router.message(lambda msg: msg.text == "🔄 Новая игра (Footle)")
async def cmd_restart_footle(message: types.Message):
    await message.answer(
        "🔄 Запускаю новую игру Footle...",
        reply_markup=ReplyKeyboardRemove()
    )
    await cmd_footle(message)

@router.message(lambda msg: msg.text == "🎯 Угадай игрока (Solo)")
async def cmd_start_solo_from_footle(message: types.Message, state: FSMContext):
    await message.answer(
        "🔄 Переключаюсь на Solo Guess...",
        reply_markup=ReplyKeyboardRemove()
    )
    await start_solo_game(message, state)
