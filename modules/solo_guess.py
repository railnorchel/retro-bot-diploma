# modules/solo_guess.py

import logging
import random
import asyncio

from modules.database import get_solo_level, set_solo_level
from aiogram.types import ReplyKeyboardRemove
from typing import Optional  # для аннотаций
from aiogram import Router, types, F
from aiogram.enums import ParseMode
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.exceptions import TelegramBadRequest

from bot import bot
from config import SOLO_PLAYERS_JSON, BASE_DIR  # пути к данным :contentReference[oaicite:0]{index=0}
from utils import load_json, is_match
from aiogram.types import FSInputFile
# Конфигурация
PHOTOS_DIR = BASE_DIR / "footphoto"
TOTAL_QUESTIONS_PER_LEVEL = 5
FUZZY_THRESHOLD = 75  # тот же порог, что был в оригинале :contentReference[oaicite:1]{index=1}

router = Router()
logger = logging.getLogger(__name__)

# Загрузка данных
try:
    SOLO_PLAYERS_DATA = load_json(SOLO_PLAYERS_JSON)
except Exception as e:
    SOLO_PLAYERS_DATA = {}
    logger.error(f"Ошибка загрузки {SOLO_PLAYERS_JSON}: {e}")

# Тексты
CORRECT_ANSWER_PHRASES = [
    "✅ В яблочко! Это он.", "🎯 Точно в цель!", "🥳 Есть контакт! Правильно.",
    "😎 Узнал, хорош!", "💯 Идеально!", "Так и есть! 👍", "Именно он! Красава!",
]
INCORRECT_ANSWER_PHRASES = [
    "❌ Эх, мимо кассы.", "🤔 Нет, это не он.", "Почти, но нет. 😬",
    "Не угадал. Попробуй в следующий раз!", "Увы, неверно. 🤷‍♂️", "Другой вариант, бро.",
]
GIVE_UP_PHRASES = [
    "🏳️ Окей, этот раунд пропущен.", "Понял, сдаешься. Идем дальше!",
    "Этот орешек оказался крепким. Следующий!", "Засчитано как пропуск. Погнали дальше!",
]
HINT_PHRASES = [
    "Держи наводку: 🕵️‍♂️", "Вот тебе маленький секрет: 😉",
    "Может, это поможет? 👇", "Лови подсказочку: 💡", "Смотри, что есть: 👀",
]
QUESTION_PHRASES = [
    "Так-с, кто же этот модник? 🤔", "А этого узнаешь? 🧐",
    "Поднапряги память! Кто на фото? 🧠", "Следующий на очереди! Твоя догадка? 👇",
    "Этот парень легенда! Или нет? 😏 Кто это?",
]
LEVEL_COMPLETE_PERFECT = [
    "Ты просто футбольный гений! 🧠✨ Идеальный результат!",
    "Вау! 5 из 5! Ты знаешь всех наперечет! 🐐",
]
LEVEL_COMPLETE_GOOD = [
    "Отличный результат! Почти идеально. 👍", "Хорош! Видно, что ты в теме. 💪",
]
LEVEL_COMPLETE_BAD = [
    "Неплохо, но есть куда расти! В следующий раз будет лучше. 😉",
    "Похоже, сегодня не твой день. Но не вешай нос! 🏈",
]

# Состояния FSM
class SoloGuessStates(StatesGroup):
    in_game = State()
    waiting_for_choice = State()

# Клавиатуры
def get_game_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💡 Подсказка", callback_data="solo_hint"),
            InlineKeyboardButton(text="🏳️ Сдаться", callback_data="solo_give_up"),
        ]
    ])

def get_game_keyboard_no_hint() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏳️ Сдаться", callback_data="solo_give_up"),
        ]
    ])

def get_level_complete_keyboard(next_level: int) -> ReplyKeyboardMarkup:
    buttons = []
    if str(next_level) in SOLO_PLAYERS_DATA:
        buttons.append(KeyboardButton(text=f"Уровень {next_level}"))
    buttons.append(KeyboardButton(text="Footle"))
    return ReplyKeyboardMarkup(
        keyboard=[buttons],
        resize_keyboard=True,
        one_time_keyboard=True
    )
# Запуск игры
async def start_solo_game(
    message: types.Message,
    state: FSMContext,
    level: Optional[int] = None
):
    # Сброс FSM
    await state.clear()

    # Определяем, с какого уровня стартовать
    if level is None:
        saved = await get_solo_level(message.from_user.id)
        level = saved or 1

    if str(level) not in SOLO_PLAYERS_DATA:
        await message.answer(
            "🎉 Поздравляю, ты прошёл все уровни!",
            reply_markup=get_solo_end_reply_keyboard()
        )
        return

    # Сохраняем прогресс в БД
    await set_solo_level(message.from_user.id, level)

    # Инициализируем FSM-данные
    await state.set_state(SoloGuessStates.in_game)
    await state.update_data(level=level, question_index=0, score=0)

    # Приветственное сообщение
    await message.answer(
        f"🏆 <b>Уровень {level}</b> начался! Угадай {TOTAL_QUESTIONS_PER_LEVEL} футболистов.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML
    )

    # Первый вопрос
    await ask_question(message, state)
async def ask_question(
    message: types.Message,
    state: FSMContext,
    feedback_text: str = ""
):
    data = await state.get_data()
    level = data["level"]
    idx   = data["question_index"]

    try:
        p = SOLO_PLAYERS_DATA[str(level)][idx]
        answers = [p["canonical_name"].lower()] + [a.lower() for a in p.get("aliases", [])]
        await state.update_data(
            correct_answers=answers,
            position=p.get("position"),
            nationality=p.get("nationality")
        )

        photo_path = PHOTOS_DIR / p["photo_file"]
        if not photo_path.exists():
            raise FileNotFoundError(f"Photo not found: {photo_path}")

        caption = (f"{feedback_text}\n\n" if feedback_text else "") + \
                  f"{random.choice(QUESTION_PHRASES)} ({idx+1}/{TOTAL_QUESTIONS_PER_LEVEL})"

        sent = await bot.send_photo(
            chat_id=message.chat.id,
            photo=FSInputFile(str(photo_path), filename=photo_path.name),
            caption=caption,
            reply_markup=get_game_keyboard(),
            parse_mode=ParseMode.HTML
        )
        await state.update_data(photo_message_id=sent.message_id)

    except Exception:
        logger.exception(f"ask_question error on level {level}, idx {idx}")
        await message.answer(
            "😞 Упс, не удалось загрузить вопрос. Пожалуйста, попробуйте чуть позже.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.clear()


# Обработка ответов
@router.message(StateFilter(SoloGuessStates.in_game))
async def handle_guess(message: types.Message, state: FSMContext):
    text = message.text or ""
    data = await state.get_data()
    # проверка fuzzy через utils.is_match
    correct = any(is_match(text, variant, FUZZY_THRESHOLD) for variant in data.get("correct_answers", []))
    # снимаем inline-клавиатуру с фото
    if data.get("photo_message_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=data["photo_message_id"],
                reply_markup=None
            )
        except TelegramBadRequest:
            pass

    # обновляем счёт и статус
    await state.update_data(
        previous_round_status = "correct" if correct else "incorrect",
        score = data.get("score", 0) + (1 if correct else 0)
    )
    await proceed_to_next_question(message, state)

# Hint & Give Up
@router.callback_query(F.data=="solo_hint", StateFilter(SoloGuessStates.in_game))
async def cb_hint(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    pos_icon = {"Нападающий":"⚽","Защитник":"🛡️","Полузащитник":"🎯","Вратарь":"🧤"}.get(data.get("position"),"ℹ️")
    flag = {
        "Аргентина":"🇦🇷","Португалия":"🇵🇹","Бразилия":"🇧🇷","Франция":"🇫🇷",
    }.get(data.get("nationality"),"")
    await callback.message.answer(
        f"{random.choice(HINT_PHRASES)}\n\n{pos_icon} Позиция: <b>{data.get('position')}</b>\n{flag} Национальность: <b>{data.get('nationality')}</b>"
    )
    # убираем кнопку «Подсказка»
    if data.get("photo_message_id"):
        try:
            await bot.edit_message_reply_markup(
                callback.message.chat.id,
                data["photo_message_id"],
                reply_markup=get_game_keyboard_no_hint()
            )
        except TelegramBadRequest:
            pass

@router.callback_query(F.data=="solo_give_up", StateFilter(SoloGuessStates.in_game))
async def cb_give_up(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    # удаляем клавиатуру и переходим дальше
    data = await state.get_data()
    if data.get("photo_message_id"):
        try:
            await bot.edit_message_reply_markup(
                callback.message.chat.id,
                data["photo_message_id"],
                reply_markup=None
            )
        except TelegramBadRequest:
            pass
    await state.update_data(previous_round_status="gave_up")
    await proceed_to_next_question(callback.message, state)

# Логика перехода между вопросами и завершения уровня
async def proceed_to_next_question(message: types.Message, state: FSMContext):
    data = await state.get_data()
    status = data.get("previous_round_status")
    feedback = {
        "correct": random.choice(CORRECT_ANSWER_PHRASES),
        "incorrect": random.choice(INCORRECT_ANSWER_PHRASES),
        "gave_up": random.choice(GIVE_UP_PHRASES),
    }.get(status, "")
    next_idx = data["question_index"] + 1

    # если уровень пройден
    if next_idx >= TOTAL_QUESTIONS_PER_LEVEL:
        if feedback:
            await message.answer(feedback)
        await asyncio.sleep(1)
        await show_level_complete_menu(message, state)
    else:
        await state.update_data(question_index=next_idx)
        await ask_question(message, state, feedback_text=feedback)
async def show_level_complete_menu(
    message: types.Message,
    state: FSMContext
):
    data = await state.get_data()
    lvl = data["level"]
    score = data["score"]
    next_lvl = lvl + 1

    # Сохраняем, на каком уровне игрок теперь будет стартовать в следующий раз
    await set_solo_level(message.from_user.id, next_lvl)

    # Выбираем сообщение-фидбек
    if score == TOTAL_QUESTIONS_PER_LEVEL:
        summary = random.choice(LEVEL_COMPLETE_PERFECT)
    elif score >= TOTAL_QUESTIONS_PER_LEVEL * 0.6:
        summary = random.choice(LEVEL_COMPLETE_GOOD)
    else:
        summary = random.choice(LEVEL_COMPLETE_BAD)

    # Отправляем итоговый экран с кнопками
    await message.answer(
        f"<b>Уровень {lvl} пройден!</b>\n"
        f"Твой результат: <b>{score}/{TOTAL_QUESTIONS_PER_LEVEL}</b>\n\n"
        f"<i>{summary}</i>\n\nЧто дальше?",
        reply_markup=get_level_complete_keyboard(next_lvl),
        parse_mode=ParseMode.HTML
    )

    # Переходим в состояние, где ждём нажатия «Уровень N» или «Footle»
    await state.set_state(SoloGuessStates.waiting_for_choice)

def get_solo_end_reply_keyboard() -> ReplyKeyboardMarkup:
    """
    После того как все уровни пройдены — две кнопки:
    🔄 Начать Solo Guess заново | 🔙 Вернуться в Footle
    """
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="🔄 Начать Solo Guess заново"),
            KeyboardButton(text="🔙 Вернуться в Footle"),
        ]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# Обработка кнопок после уровня
@router.message(StateFilter(SoloGuessStates.waiting_for_choice), F.text.startswith("Уровень"))
async def handle_next_level_button(message: types.Message, state: FSMContext):
    try:
        lvl = int(message.text.split()[-1])
        await start_solo_game(message, state, level=lvl)
    except:
        await message.answer("Неверная команда. Попробуйте снова.")

@router.message(StateFilter(SoloGuessStates.waiting_for_choice), F.text=="Footle")
async def handle_to_footle(message: types.Message, state: FSMContext):
    await state.clear()
    from modules.footle import cmd_footle
    await cmd_footle(message)

@router.message(lambda msg: msg.text == "🔄 Начать Solo Guess заново")
async def cmd_restart_solo(message: types.Message, state: FSMContext):
    await message.answer(
        "🔄 Запускаю Solo Guess заново с 1 уровня…",
        reply_markup=ReplyKeyboardRemove()
    )
    await start_solo_game(message, state, level=1)

@router.message(lambda msg: msg.text == "🔙 Вернуться в Footle")
async def cmd_back_to_footle(message: types.Message, state: FSMContext):
    await message.answer(
        "🔄 Переключаюсь на Footle…",
        reply_markup=ReplyKeyboardRemove()
    )
    from modules.footle import cmd_footle
    await cmd_footle(message)
