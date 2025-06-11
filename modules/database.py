# modules/database.py

import aiosqlite
import datetime
import logging
from pathlib import Path
from typing import Optional

# --- Константы ---
BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "bot.db"
logger = logging.getLogger(__name__)

# --- Утилиты для работы с БД ---

def period_key() -> str:
    """Возвращает ключ периода для статистики Footle."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

# --- Инициализация БД и таблиц ---

async def init_db():
    """
    Инициализирует таблицы в базе данных, если их нет:
    - footle_state    — текущее состояние Footle
    - user_rating     — очки пользователей
    - solo_progress   — сохранённый уровень Solo Guess
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS footle_state (
                user_id  INTEGER,
                period   TEXT,
                attempts INTEGER DEFAULT 0,
                solved   INTEGER DEFAULT 0,
                PRIMARY KEY(user_id, period)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_rating (
                user_id INTEGER PRIMARY KEY,
                points  INTEGER DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS solo_progress (
                user_id INTEGER PRIMARY KEY,
                level   INTEGER NOT NULL
            );
            """
        )
        await db.commit()

    logger.info("База данных инициализирована.")

# --- Footle state ---

async def get_footle_state(uid: int) -> tuple[int, int]:
    """Получает состояние игры Footle для пользователя."""
    p = period_key()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT attempts, solved FROM footle_state WHERE user_id=? AND period=?",
            (uid, p)
        )
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO footle_state(user_id, period) VALUES(?,?)",
                (uid, p)
            )
            await db.commit()
            return 0, 0
        return row  # (attempts, solved)

async def save_footle_state(uid: int, attempts: int, solved: int):
    """Сохраняет состояние игры Footle для пользователя."""
    p = period_key()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE footle_state SET attempts=?, solved=? WHERE user_id=? AND period=?",
            (attempts, solved, uid, p)
        )
        await db.commit()

# --- User rating ---

async def add_rating(uid: int, pts: int):
    """Добавляет очки к рейтингу пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT points FROM user_rating WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE user_rating SET points=? WHERE user_id=?",
                (row[0] + pts, uid)
            )
        else:
            await db.execute(
                "INSERT INTO user_rating(user_id, points) VALUES(?,?)",
                (uid, pts)
            )
        await db.commit()

async def get_rating(uid: int) -> int:
    """Возвращает текущий рейтинг пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT points FROM user_rating WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

# --- Solo Guess progress ---

async def get_solo_level(uid: int) -> Optional[int]:
    """
    Возвращает последний сохранённый уровень Solo Guess для пользователя,
    или None, если прогресс не найден.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT level FROM solo_progress WHERE user_id=?",
            (uid,)
        )
        row = await cur.fetchone()
        return row[0] if row else None

async def set_solo_level(uid: int, level: int):
    """
    Сохраняет или обновляет прогресс Solo Guess:
    сохраняет уровень level для пользователя uid.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO solo_progress(user_id, level) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET level=excluded.level
            """,
            (uid, level)
        )
        await db.commit()
# --- Новые методы для Solo Guess ---

async def get_solo_level(uid: int) -> int:
    """Возвращает текущий уровень Solo Guess для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT level FROM solo_progress WHERE user_id=?", (uid,)
        )
        row = await cur.fetchone()
        return row[0] if row else 1

async def set_solo_level(uid: int, level: int):
    """Устанавливает (или обновляет) уровень Solo Guess для пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO solo_progress(user_id, level) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET level=excluded.level",
            (uid, level)
        )
        await db.commit()