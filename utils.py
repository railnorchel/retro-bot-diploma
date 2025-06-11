# utils.py

from pathlib import Path
import json
from difflib import SequenceMatcher
from aiogram import types

def load_json(path: Path) -> dict:
    """
    Загружает и возвращает содержимое JSON-файла по заданному пути.
    """
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def is_match(user_input: str, correct: str, threshold: float = 75) -> bool:
    """
    Сравнивает user_input и correct (оба приводятся к lower/strip),
    возвращает True, если процент похожести ≥ threshold.
    """
    ratio = SequenceMatcher(None,
                            user_input.strip().lower(),
                            correct.strip().lower()
                           ).ratio() * 100
    return ratio >= threshold

def load_photo(path: Path) -> types.FSInputFile:
    """
    Оборачивает путь к файлу в FSInputFile для отправки в Telegram.
    """
    return types.FSInputFile(path)
