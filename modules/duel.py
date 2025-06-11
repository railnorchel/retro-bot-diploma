# modules/duel.py

import asyncio
import json
import random
import time
import logging
from typing import Dict, Any, Optional
from thefuzz import fuzz
import aiosqlite

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest
from bot import bot
from config import DB_PATH, DUEL_WORDS_JSON, BASE_DIR

router = Router()
logger = logging.getLogger(__name__)

# --- Глобальные переменные и настройки ---
DUEL_TOTAL_ROUNDS = 5
DUEL_TIMEOUT = 15
POINTS_BASE = 10

duel_timers: Dict[int, asyncio.Task] = {}
duel_sequences: Dict[str, list[dict]] = {}
DUEL_WORDS: list[dict] = []


# --- Инициализация ---

async def init_duel_db() -> None:
    """Создаёт/обновляет таблицы для дуэлей, добавляя новые поля."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duel_games (
                id TEXT PRIMARY KEY, chat_id INTEGER, player1 INTEGER, player2 INTEGER,
                round INTEGER, total_rounds INTEGER, current_word TEXT, current_photo TEXT,
                round_start_time INTEGER, score1 INTEGER DEFAULT 0, score2 INTEGER DEFAULT 0,
                rounds_won1 INTEGER DEFAULT 0, rounds_won2 INTEGER DEFAULT 0,
                status TEXT, winner INTEGER, created_at INTEGER, ended_at INTEGER
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS duel_leaderboard (
                user_id INTEGER PRIMARY KEY, wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0,
                win_streak INTEGER DEFAULT 0
            );
        """)
        try:
            await db.execute("ALTER TABLE duel_games ADD COLUMN rounds_won1 INTEGER DEFAULT 0;")
            await db.execute("ALTER TABLE duel_games ADD COLUMN rounds_won2 INTEGER DEFAULT 0;")
        except aiosqlite.OperationalError: pass
        try:
            await db.execute("ALTER TABLE duel_leaderboard ADD COLUMN win_streak INTEGER DEFAULT 0;")
        except aiosqlite.OperationalError: pass
        await db.commit()


@router.startup()
async def on_startup_duel():
    global DUEL_WORDS
    await init_duel_db()
    try:
        with open(DUEL_WORDS_JSON, encoding="utf-8") as f:
            levels_data = json.load(f)
        flat_list = [player for level_players in levels_data.values() for player in level_players]
        DUEL_WORDS = flat_list
        if DUEL_WORDS:
            logger.info(f"Дуэли: Успешно загружено {len(DUEL_WORDS)} игроков.")
        else:
            logger.error(f"Дуэли: Данные из {DUEL_WORDS_JSON} загружены, но список игроков пуст.")
    except Exception as e:
        logger.error(f"Дуэли: КРИТИЧЕСКАЯ ОШИБКА загрузки данных из {DUEL_WORDS_JSON}: {e}")


# --- Утилиты ---

def mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def get_duel_invite_keyboard(player1_id: int, player2_id: int) -> types.InlineKeyboardMarkup:
    accept_callback = f"duel_accept:{player1_id}:{player2_id}"
    decline_callback = f"duel_decline:{player1_id}:{player2_id}"
    buttons = [
        [
            types.InlineKeyboardButton(text="✅ Принять", callback_data=accept_callback),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=decline_callback)
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


async def schedule_round_timeout(chat_id: int, duel_id: str, current_round: int):
    async def _timeout_task():
        await asyncio.sleep(DUEL_TIMEOUT)
        if duel_timers.get(chat_id) is task:
            await on_round_timeout(chat_id, duel_id, current_round)

    task = asyncio.create_task(_timeout_task())
    duel_timers[chat_id] = task


async def cancel_round_timeout(chat_id: int):
    task = duel_timers.pop(chat_id, None)
    if task: task.cancel()


# --- Основная логика дуэли ---

async def start_duel_round(duel_id: str, chat_id: int, current_round: int):
    player_sequence = duel_sequences.get(duel_id, [])
    if not player_sequence or len(player_sequence) < current_round:
        logger.error(f"Ошибка в дуэли {duel_id}: нет игрока для раунда {current_round}")
        await bot.send_message(chat_id, "Произошла внутренняя ошибка. Дуэль прервана.")
        return

    player_data = player_sequence[current_round - 1]
    word, photo_file = player_data["canonical_name"].lower(), player_data["photo_file"]
    now_ts = int(time.time())

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE duel_games SET round=?, current_word=?, current_photo=?, round_start_time=? WHERE id=?",
            (current_round, word, photo_file, now_ts, duel_id))
        await db.commit()

    caption = f"🏁 <b>Раунд {current_round}/{DUEL_TOTAL_ROUNDS}</b> — угадайте футболиста!"
    photo_path = BASE_DIR / "footphoto" / photo_file

    if photo_path.exists():
        await bot.send_photo(chat_id, FSInputFile(photo_path), caption=caption, parse_mode=ParseMode.HTML)
    else:
        logger.warning(f"Фото не найдено для дуэли: {photo_path}")
        await bot.send_message(chat_id, f"{caption}\n(Ошибка: не удалось загрузить фото)", parse_mode=ParseMode.HTML)

    await cancel_round_timeout(chat_id)
    await schedule_round_timeout(chat_id, duel_id, current_round)


async def on_round_timeout(chat_id: int, duel_id: str, timed_out_round: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM duel_games WHERE id=? AND status='active'", (duel_id,))
        duel = await cursor.fetchone()
    if not duel or duel["round"] != timed_out_round: return
    duel_timers.pop(chat_id, None)
    await bot.send_message(chat_id,
                           f"⏱ <b>Раунд {timed_out_round}:</b> никто не успел за {DUEL_TIMEOUT} сек.\nФамилия: <b>{duel['current_word'].upper()}</b>",
                           parse_mode=ParseMode.HTML)
    await advance_round_or_finish(duel)


async def advance_round_or_finish(duel: aiosqlite.Row):
    await asyncio.sleep(2)
    next_round = duel['round'] + 1
    if next_round > duel['total_rounds']:
        await finalize_duel(duel)
    else:
        await bot.send_message(duel['chat_id'], f"🔜 Подготовка к раунду {next_round}/{duel['total_rounds']}…")
        await asyncio.sleep(2)
        await start_duel_round(duel['id'], duel['chat_id'], next_round)


async def finalize_duel(duel: aiosqlite.Row):
    """🔥 УЛУЧШЕНО: Завершает дуэль с правильным обновлением лидерборда через ON CONFLICT."""
    p1, p2, s1, s2 = duel['player1'], duel['player2'], duel['score1'], duel['score2']
    r_won1, r_won2 = duel['rounds_won1'], duel['rounds_won2']
    winner, loser = None, None

    async with aiosqlite.connect(DB_PATH) as db:
        if s1 > s2:
            winner, loser = p1, p2
            # Победитель: +1 победа, +1 к серии
            await db.execute("""
                INSERT INTO duel_leaderboard (user_id, wins, win_streak) VALUES (?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET wins = wins + 1, win_streak = win_streak + 1;
            """, (winner,))
            # Проигравший: +1 поражение, сброс серии
            await db.execute("""
                INSERT INTO duel_leaderboard (user_id, losses, win_streak) VALUES (?, 1, 0)
                ON CONFLICT(user_id) DO UPDATE SET losses = losses + 1, win_streak = 0;
            """, (loser,))
        elif s2 > s1:
            winner, loser = p2, p1
            # Победитель: +1 победа, +1 к серии
            await db.execute("""
                INSERT INTO duel_leaderboard (user_id, wins, win_streak) VALUES (?, 1, 1)
                ON CONFLICT(user_id) DO UPDATE SET wins = wins + 1, win_streak = win_streak + 1;
            """, (winner,))
            # Проигравший: +1 поражение, сброс серии
            await db.execute("""
                INSERT INTO duel_leaderboard (user_id, losses, win_streak) VALUES (?, 1, 0)
                ON CONFLICT(user_id) DO UPDATE SET losses = losses + 1, win_streak = 0;
            """, (loser,))
        else:  # Ничья
            # Оба игрока: +1 ничья, сброс серии
            for player_id in (p1, p2):
                await db.execute("""
                    INSERT INTO duel_leaderboard (user_id, draws, win_streak) VALUES (?, 1, 0)
                    ON CONFLICT(user_id) DO UPDATE SET draws = draws + 1, win_streak = 0;
                """, (player_id,))

        win_streak = 0
        if winner:
            cursor = await db.execute("SELECT win_streak FROM duel_leaderboard WHERE user_id = ?", (winner,))
            streak_row = await cursor.fetchone()
            if streak_row: win_streak = streak_row[0]

        await db.execute("UPDATE duel_games SET status='finished', winner=?, ended_at=? WHERE id=?",
                         (winner, int(time.time()), duel['id']))
        await db.commit()

    p1_user, p2_user = await bot.get_chat(p1), await bot.get_chat(p2)
    text = (f"🎉 <b>Дуэль завершена!</b>\n\n"
            f"{mention(p1, p1_user.full_name)} (выиграл {r_won1} раундов) — <b>{s1}</b> очков\n"
            f"{mention(p2, p2_user.full_name)} (выиграл {r_won2} раундов) — <b>{s2}</b> очков\n\n")

    if winner:
        winner_user = await bot.get_chat(winner)
        text += f"🏆 Победитель: {mention(winner, winner_user.full_name)}"
        if win_streak >= 2:
            text += f"\n🔥 <b>Серия побед: {win_streak}!</b>"
    else:
        text += "🤝 <b>Ничья!</b>"

    rematch_keyboard = None
    if winner and loser: # Кнопка реванша только если есть победитель и проигравший
        rematch_callback = f"duel_rematch:{winner}:{loser}"
        rematch_keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="🔄 Реванш!", callback_data=rematch_callback)]
        ])

    await bot.send_message(duel['chat_id'], text, parse_mode=ParseMode.HTML, reply_markup=rematch_keyboard)
    await cancel_round_timeout(duel['chat_id'])
    duel_sequences.pop(duel['id'], None)


@router.message(Command("duel"))
async def cmd_duel_start(message: types.Message):
    """Отправляет приглашение на дуэль по реплаю на сообщение."""
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("❌ Дуэли доступны только в группах.")

    # Проверяем, есть ли вообще ответ на сообщение
    if not message.reply_to_message:
        return await message.answer("❌ Чтобы начать дуэль, ответьте на сообщение оппонента командой /duel.")

    # 🔥 ГЛАВНАЯ ПРОВЕРКА НА АНОНИМНОСТЬ!
    # Если у отвеченного сообщения нет автора (from_user is None),
    # значит, это анонимный админ, канал или служебное сообщение.
    if not message.reply_to_message.from_user:
        return await message.answer("❌ Нельзя вызвать на дуэль анонимного админа, канал или служебное сообщение.")

    # Если мы прошли проверку выше, значит, автор точно есть.
    initiator = message.from_user
    opponent = message.reply_to_message.from_user

    # Теперь твои старые проверки будут работать безопасно
    if initiator.id == opponent.id:
        return await message.answer("❌ Нельзя дуэлиться с самим собой.")

    if opponent.is_bot:
        return await message.answer("❌ Нельзя дуэлиться с ботом.")

    if not DUEL_WORDS:
        return await message.answer("❌ Ошибка сервера: не загружены игроки для дуэли. Сообщите администратору.")

    # Проверка на активную дуэль в чате
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM duel_games WHERE chat_id=? AND status='active'", (message.chat.id,))
        if await cursor.fetchone():
            return await message.answer("❌ В этом чате уже идёт дуэль. Отмените её через /cancel_duel.")

    # --- Весь остальной код для отправки приглашения остается без изменений ---
    keyboard = get_duel_invite_keyboard(initiator.id, opponent.id)
    invite_text = (
        f"⚔️ {mention(opponent.id, opponent.full_name)}, игрок {mention(initiator.id, initiator.full_name)} "
        f"вызывает тебя на дуэль «Угадай футболиста»!\n\n"
        f"Принимаешь вызов?"
    )
    sent_message = await message.answer(invite_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    async def auto_decline_task():
        await asyncio.sleep(60)
        try:
            await bot.edit_message_text(
                chat_id=sent_message.chat.id,
                message_id=sent_message.message_id,
                text=f"Вызов на дуэль от {mention(initiator.id, initiator.full_name)} "
                     f"для {mention(opponent.id, opponent.full_name)} истёк.",
                reply_markup=None,
                parse_mode=ParseMode.HTML
            )
        except TelegramBadRequest:
            pass

    asyncio.create_task(auto_decline_task())


@router.message(Command("cancel_duel"))
async def cmd_cancel_duel(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM duel_games WHERE chat_id=? AND status='active'", (message.chat.id,))
        duel = await cursor.fetchone()
    if not duel: return await message.answer("В этом чате нет активных дуэлей для отмены.")
    if message.from_user.id not in (duel["player1"], duel["player2"]): return await message.answer("Отменить дуэль может только один из участников.")
    await cancel_round_timeout(message.chat.id)
    duel_sequences.pop(duel["id"], None)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE duel_games SET status='canceled', ended_at=? WHERE id=?", (int(time.time()), duel["id"]))
        await db.commit()
    await message.answer(f"❌ {mention(message.from_user.id, message.from_user.full_name)} отменил(а) дуэль.", parse_mode=ParseMode.HTML)


@router.message(Command("duel_leaderboard"))
async def cmd_duel_leaderboard(message: types.Message):
    lines = [
        "🏆 <b>Топ-10 игроков: «Угадай футболиста»</b>",
        "<i>(Победы - Поражения - Ничьи)</i>\n"
    ]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM duel_leaderboard ORDER BY wins DESC, win_streak DESC, losses ASC LIMIT 10"
            )
            rows = await cursor.fetchall()

        if not rows:
            await message.answer("🏆 Таблица лидеров «Угадай футболиста» пока пуста.")
            return

        # Словарь с медалями
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        for i, r in enumerate(rows, 1):
            try:
                user = await bot.get_chat(r["user_id"])
                name_mention = mention(user.id, user.full_name)
            except TelegramBadRequest:
                name_mention = f"Игрок <code>{r['user_id']}</code>"

            stats_line = f"<b>{r['wins']}-{r['losses']}-{r['draws']}</b>"

            win_streak = r['win_streak'] if 'win_streak' in r.keys() else 0
            streak = f" 🔥{win_streak}" if win_streak >= 2 else ""

            # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
            # Получаем медаль или номер с точкой
            place = medals.get(i, f"{i}.")

            lines.append(f"{place} {name_mention} — {stats_line}{streak}")

        await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Ошибка duel_leaderboard: {e}", exc_info=True)
        await message.answer("❌ Ошибка при загрузке таблицы лидеров.")


@router.callback_query(F.data.startswith("duel_accept:"))
async def handle_duel_accept(callback: types.CallbackQuery):
    _, p1_id, p2_id = callback.data.split(":")
    player1_id, player2_id = int(p1_id), int(p2_id)
    if callback.from_user.id != player2_id:
        return await callback.answer("Это приглашение не для вас!", show_alert=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Вызов принят!")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM duel_games WHERE chat_id=? AND status='active'", (callback.message.chat.id,))
        if await cursor.fetchone():
            return await callback.message.answer("Пока вы думали, в чате уже началась другая дуэль.")
    initiator, opponent = await bot.get_chat(player1_id), await bot.get_chat(player2_id)
    ts = int(time.time())
    duel_id = f"{callback.message.chat.id}_{player1_id}_{player2_id}_{ts}"
    duel_sequences[duel_id] = random.sample(DUEL_WORDS, k=DUEL_TOTAL_ROUNDS)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO duel_games (id, chat_id, player1, player2, round, total_rounds, status, created_at) VALUES (?, ?, ?, ?, 1, ?, 'active', ?)",
                         (duel_id, callback.message.chat.id, player1_id, player2_id, DUEL_TOTAL_ROUNDS, ts))
        await db.commit()
    await callback.message.answer(
        f"🆚 <b>Дуэль принята!</b>\n{mention(initiator.id, initiator.full_name)} vs {mention(opponent.id, opponent.full_name)}\n"
        f"Раунд 1/{DUEL_TOTAL_ROUNDS} начнётся через 3 секунды…",
        parse_mode=ParseMode.HTML)
    await asyncio.sleep(3)
    await start_duel_round(duel_id, callback.message.chat.id, 1)


@router.callback_query(F.data.startswith("duel_decline:"))
async def handle_duel_decline(callback: types.CallbackQuery):
    _, p1_id, p2_id = callback.data.split(":")
    player1_id, player2_id = int(p1_id), int(p2_id)
    if callback.from_user.id not in (player1_id, player2_id):
        return await callback.answer("Это приглашение не для вас!", show_alert=True)
    initiator, opponent = await bot.get_chat(player1_id), await bot.get_chat(player2_id)
    declined_by_name = mention(callback.from_user.id, callback.from_user.full_name)
    await callback.message.edit_text(
        f"🚫 {declined_by_name} отклонил(а) вызов на дуэль от {mention(initiator.id, initiator.full_name)}.",
        parse_mode=ParseMode.HTML)
    await callback.answer("Вызов отклонен.")


@router.message(F.text & ~F.text.startswith('/'))
async def on_duel_guess(message: types.Message):
    """
    ✅ ИСПРАВЛЕНО: Обрабатывает попытку угадать фамилию в активной дуэли.
    - Регистр ввода НЕ ИМЕЕТ ЗНАЧЕНИЯ, т.к. и ввод, и ответы приводятся к нижнему регистру.
    - Проверяет ответ как по русским псевдонимам (aliases), так и по основному английскому имени (canonical_name).
    - Использует гибкое сравнение с порогом 75%.
    """
    if message.chat.type not in ("group", "supergroup"): return

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM duel_games WHERE chat_id=? AND status='active'", (message.chat.id,))
        duel = await cursor.fetchone()

    if not duel: return

    user_id = message.from_user.id
    if user_id not in (duel["player1"], duel["player2"]): return
    if not duel["current_word"]: return

    target_player_data = next((p for p in DUEL_WORDS if p['canonical_name'].lower() == duel['current_word']), None)
    if not target_player_data:
        logger.error(f"Не удалось найти данные для слова {duel['current_word']} в DUEL_WORDS.")
        return

    # 1. Приводим ввод пользователя к нижнему регистру. КАПС УБИРАЕТСЯ ЗДЕСЬ.
    user_guess = message.text.strip().lower()

    # 2. Собираем ВСЕ возможные правильные ответы в один список
    russian_aliases = target_player_data.get('aliases', [])
    canonical_name = target_player_data.get('canonical_name', '')
    all_possible_answers = russian_aliases + ([canonical_name] if canonical_name else [])

    if not all_possible_answers:
        logger.warning(f"У игрока {duel['current_word']} нет ни canonical_name, ни aliases. Его невозможно угадать.")
        return

    # 3. Ищем совпадение, приводя КАЖДЫЙ правильный ответ тоже к нижнему регистру
    is_correct = False
    for answer in all_possible_answers:
        # Приводим правильный ответ к нижнему регистру для корректного сравнения
        answer_lower = answer.lower()
        ratio = fuzz.ratio(user_guess, answer_lower)

        # 🔥 Добавил логирование для отладки. Теперь в консоли будет видно, что с чем сравнивается.
        logger.debug(f"Дуэль: Сравнение '{user_guess}' с '{answer_lower}'. Сходство: {ratio}%")

        if ratio >= 75:
            is_correct = True
            break  # Нашли совпадение, выходим из цикла

    if is_correct:
        await cancel_round_timeout(message.chat.id)

        elapsed = int(time.time()) - duel["round_start_time"]
        pts = max(1, POINTS_BASE - elapsed)

        s1, s2 = duel["score1"], duel["score2"]
        r_won1, r_won2 = duel["rounds_won1"], duel["rounds_won2"]

        if user_id == duel["player1"]:
            s1 += pts
            r_won1 += 1
        else:
            s2 += pts
            r_won2 += 1

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE duel_games SET score1=?, score2=?, rounds_won1=?, rounds_won2=? WHERE id=?",
                             (s1, s2, r_won1, r_won2, duel["id"]))
            await db.commit()

        await message.answer(
            f"✅ <b>Раунд {duel['round']}:</b> {mention(user_id, message.from_user.full_name)} угадал(а) за {elapsed} сек — +{pts} очков!",
            parse_mode=ParseMode.HTML)

        # Перезапрашиваем дуэль из БД, чтобы получить обновленные очки для следующей функции
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM duel_games WHERE id=?", (duel['id'],))
            updated_duel = await cursor.fetchone()

        if updated_duel:
            await advance_round_or_finish(updated_duel)


@router.callback_query(F.data.startswith("duel_rematch:"))
async def handle_duel_rematch(callback: types.CallbackQuery):
    _, p1_id, p2_id = callback.data.split(":")
    winner_id, loser_id = int(p1_id), int(p2_id)

    # Проверка: только проигравший может нажать кнопку "Реванш"
    if callback.from_user.id != loser_id:
        return await callback.answer("Только проигравший может принять реванш!", show_alert=True)

    # Убираем кнопку "Реванш" со старого сообщения
    await callback.message.edit_reply_markup(reply_markup=None)

    # Получаем объекты пользователей
    # `rematch_initiator` - это проигравший, который хочет реванш
    # `rematch_opponent` - это победитель, которому предлагают реванш
    rematch_initiator = await bot.get_chat(loser_id)
    rematch_opponent = await bot.get_chat(winner_id)

    # --- ГЛАВНОЕ ИЗМЕНЕНИЕ ЗДЕСЬ ---
    # Создаем клавиатуру, где ИНИЦИАТОРОМ (player1) становится ПРОИГРАВШИЙ,
    # а ОППОНЕНТОМ (player2) - ПОБЕДИТЕЛЬ.
    keyboard = get_duel_invite_keyboard(rematch_initiator.id, rematch_opponent.id)

    # Формируем текст приглашения
    invite_text = (
        f"⚔️ {mention(rematch_opponent.id, rematch_opponent.full_name)}, "
        f"проигравший {mention(rematch_initiator.id, rematch_initiator.full_name)} жаждет реванша и снова вызывает тебя на дуэль!"
    )

    # Отправляем новое сообщение с правильным приглашением
    await callback.message.answer(invite_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    await callback.answer("Запрос на реванш отправлен!")