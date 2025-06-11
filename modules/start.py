# modules/start.py

import os
from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from config import SALAM_DIR
from modules.footle import cmd_footle  # команда Footle
from aiogram.fsm.context import FSMContext # <-- ДОБАВЬ ЭТОТ ИМПОРТ
from modules.solo_guess import start_solo_game # <-- ДОБАВЬ ЭТОТ ИМПОРТ

router = Router()

WELCOME_PHOTO = FSInputFile(
    os.path.join(SALAM_DIR, "retro_myach.png"),
    filename="retro_myach.png"
)

# Inline-кнопки для первого поста
INLINE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(
            text="Подписаться на «Ретро Мяч!»",
            url="https://t.me/rretroball"
        ),
        InlineKeyboardButton(text="Хорошо, понял", callback_data="ack")
    ]
])

# Reply-кнопки (ReplyKeyboard) для выбора игры
GAME_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Footle"), KeyboardButton(text="Solo Guess")]
    ],
    resize_keyboard=True
)

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    # 1) Отправляем баннер + inline-кнопки
    await message.answer_photo(
        photo=WELCOME_PHOTO,
        caption=(
            "👋 Привет! Я — бот от владельца канала «Ретро Мяч!». 🔔\n"
            "Подпишитесь, чтобы не пропустить новые игры и конкурсы!"
        ),
        reply_markup=INLINE_KB
    )

@router.callback_query(lambda c: c.data == "ack")
async def cb_ack(callback: types.CallbackQuery):
    await callback.answer()  # скрываем индикатор
    # 2) После нажатия «Хорошо, понял» отправляем текст с выбором и показываем Reply-клавиатуру
    await callback.message.answer(
        "Я готов предложить тебе весёлые футбольные мини-игры:\n"
        "• Footle – угадай футбольную фамилию за 6 попыток.\n"
        "• Solo Guess – отгадай футболиста по фото.\n\n"
        "⚽️ Выбери игру, в которую хочешь сыграть:",
        reply_markup=GAME_KEYBOARD
    )

@router.message(lambda m: m.text == "Footle")
async def on_text_footle(message: types.Message):
    # Удаляем сообщение пользователя «Footle»
    try:
        await message.delete()
    except:
        pass

    # Скрываем Reply-клавиатуру и выводим подробные правила Footle
    rules_text = (
        "📝 *Правила Footle:*\n\n"
        "1. Вы должны угадать футбольную фамилию за **6 попыток**.\n"
        "2. Каждое загаданное слово состоит из N букв (N зависит от слова).\n"
        "3. Все фамилии нужно вводить **латиницей (английскими буквами)**.\n\n"
        "После каждой попытки бот покажет подсказку:\n"
        "• 🟩 — буква стоит на правильной позиции.\n"
        "• 🟨 — буква есть в слове, но не на этом месте.\n"
        "• ⬜ — буквы нет в слове.\n\n"
        "*Пример:* Если загаданное слово `messi`, а вы введёте `metty`, то подсказка будет:\n"
        "`🟩⬜⬜⬜⬜` (буква “m” зелёная, остальные — нет).\n\n"
        "Удачи! Введите первую фамилию:"
    )
    await message.answer(
        text=rules_text,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

    # Запускаем Footle – показывает пустую доску, и бот ждёт ввод первой попытки
    await cmd_footle(message)


@router.message(lambda m: m.text == "Solo Guess")
async def on_text_solo(message: types.Message, state: FSMContext):  # <-- ДОБАВЬ state: FSMContext
    try:
        await message.delete()
    except:
        pass

    # Сразу запускаем игру с первого уровня
    await start_solo_game(message, state, level=1)
