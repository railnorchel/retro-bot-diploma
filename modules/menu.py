# modules/menu.py

import os
from pathlib import Path

from aiogram.types import (
    FSInputFile,
    BufferedInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from bot import bot

# -------------------------------------------------------------------
#             Настройка: директория с фотографиями игроков
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent       # корень проекта
FOOTPHOTO_DIR = BASE_DIR / "footphoto"        # папка, где лежат все .jpg

def _load_photo(filename: str) -> BufferedInputFile:
    """
    Открывает файл <FOOTPHOTO_DIR>/<filename> и возвращает BufferedInputFile.
    """
    path = FOOTPHOTO_DIR / filename
    data = path.read_bytes()
    return BufferedInputFile(data, filename=filename)


# -------------------------------------------------------------------
#               Игроки уровня "Лёгкий" (easy)
# -------------------------------------------------------------------
players_easy = {
    "Лионель Месси": {
        "photo_file": _load_photo("messi.jpg"),
        "answer":      "месси",
        "position":    "Нападающий",
        "nationality": "Аргентина"
    },
    "Килиан Мбаппе": {
        "photo_file": _load_photo("mbappe.jpg"),
        "answer":      "мбаппе",
        "position":    "Нападающий",
        "nationality": "Франция"
    },
    "Криштиану Роналду": {
        "photo_file": _load_photo("ronaldo.jpg"),
        "answer":      "роналду",
        "position":    "Нападающий",
        "nationality": "Португалия"
    },
    "Неймар": {
        "photo_file": _load_photo("neymar.jpg"),
        "answer":      "неймар",
        "position":    "Нападающий",
        "nationality": "Бразилия"
    },
    "Роберт Левандовский": {
        "photo_file": _load_photo("levandowski.jpg"),
        "answer":      "левандовский",
        "position":    "Нападающий",
        "nationality": "Польша"
    },
}


# -------------------------------------------------------------------
#            Игроки уровня "Средний" (medium)
# -------------------------------------------------------------------
players_medium = {
    "Кевин Де Брёйне": {
        "photo_file": _load_photo("debruyne.jpg"),
        "answer":      "дебрёйне",
        "position":    "Полузащитник",
        "nationality": "Бельгия"
    },
    "Антуан Гризманн": {
        "photo_file": _load_photo("griezmann.jpg"),
        "answer":      "гризманн",
        "position":    "Нападающий / Полузащитник",
        "nationality": "Франция"
    },
    "Тьерри Анри": {
        "photo_file": _load_photo("henry.jpg"),
        "answer":      "анри",
        "position":    "Нападающий",
        "nationality": "Франция"
    },
    "Н'Голо Канте": {
        "photo_file": _load_photo("kante.jpg"),
        "answer":      "канте",
        "position":    "Опорный полузащитник",
        "nationality": "Франция"
    },
    "Лука Модрич": {
        "photo_file": _load_photo("modric.jpg"),
        "answer":      "модрич",
        "position":    "Полузащитник",
        "nationality": "Хорватия"
    },
}


# -------------------------------------------------------------------
#            Игроки уровня "Сложный" (hard)
# -------------------------------------------------------------------
players_hard = {
    "Марио Балотелли": {
        "photo_file": _load_photo("balotelli.jpg"),
        "answer":      "балотелли",
        "position":    "Нападающий",
        "nationality": "Италия"
    },
    "Филиппе Коутиньо": {
        "photo_file": _load_photo("coutinho.jpg"),
        "answer":      "коутиньо",
        "position":    "Полузащитник",
        "nationality": "Бразилия"
    },
    "Диего Форлан": {
        "photo_file": _load_photo("forlan.jpg"),
        "answer":      "форлан",
        "position":    "Нападающий",
        "nationality": "Уругвай"
    },
    "Пепе": {
        "photo_file": _load_photo("pepe.jpg"),
        "answer":      "пепе",
        "position":    "Защитник",
        "nationality": "Португалия"
    },
    "Ривалдо": {
        "photo_file": _load_photo("rivaldo.jpg"),
        "answer":      "ривалдо",
        "position":    "Нападающий",
        "nationality": "Бразилия"
    },
}


# -------------------------------------------------------------------
#          Собираем все уровни в один словарь "levels"
# -------------------------------------------------------------------
levels = {
    "easy":   players_easy,
    "medium": players_medium,
    "hard":   players_hard
}

# Отображаемые названия уровней (для подписи)
level_names = {
    "easy":   "Лёгкий",
    "medium": "Средний",
    "hard":   "Сложный"
}

# -------------------------------------------------------------------
#        Вспомогательные глобальные структуры
# -------------------------------------------------------------------
active_games = {}      # текущие сессии {user_id: { … }}
user_stats = {}        # статистика по Solo Guess {user_id: {"correct", "incorrect", "incorrect_list"}}
last_footballer = {}   # последний показанный игрок {user_id: "Полное имя"}
user_remaining = {}    # оставшиеся игроки в очереди {user_id: [список ключей из levels[level]]}


# -------------------------------------------------------------------
#       Функции для управления очередью и удалением inline-кнопок
# -------------------------------------------------------------------
def remove_player(user_id: int, chosen: str):
    """
    Удаляем <chosen> (ключ словаря) из списка user_remaining[user_id],
    чтобы игроки не повторялись.
    """
    if user_id in user_remaining:
        user_remaining[user_id] = [
            nm for nm in user_remaining[user_id] if nm.lower() != chosen.lower()
        ]


async def lock_previous_card(user_id: int, chat_id: int):
    """
    Если у пользователя есть сообщение с фото (msg_id), убираем у него inline-кнопки
    (edit_message_reply_markup с пустым reply_markup).
    """
    session = active_games.get(user_id)
    if session and session.get("msg_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=session["msg_id"],
                reply_markup=None
            )
        except:
            pass


# -------------------------------------------------------------------
#       Функции, возвращающие inline-клавиатуры
# -------------------------------------------------------------------
def get_game_keyboard() -> InlineKeyboardMarkup:
    """
    Возвращает InlineKeyboardMarkup с двумя кнопками:
     [ "Подсказка" (callback_data="hint"),  "Сдаться" (callback_data="give_up") ]
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подсказка", callback_data="hint"),
                InlineKeyboardButton(text="Сдаться", callback_data="give_up")
            ],
        ]
    )


def get_game_keyboard_no_hint() -> InlineKeyboardMarkup:
    """
    Когда убрать кнопку "Подсказка" — оставляем только "Сдаться".
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сдаться", callback_data="give_up")
            ],
        ]
    )


def get_restart_keyboard() -> InlineKeyboardMarkup:
    """
    После завершения уровня иногда можно показывать кнопку «В меню» (callback_data="go_to_final_menu").
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="В меню", callback_data="go_to_final_menu")
            ],
        ]
    )
