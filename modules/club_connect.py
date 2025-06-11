# modules/club_connect.py
import asyncio
import time
import json
import random
import aiosqlite
import logging

from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Set
from aiogram import Router, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest
from fuzzywuzzy import fuzz
from bot import bot
from config import DB_PATH, CLUB_PLAYERS_JSON

logger = logging.getLogger(__name__)
router = Router()

# --- КОНФИГУРАЦИЯ ИГРЫ ---
FIXED_CLUBS_FOR_TESTING = False
MOVE_TIMEOUT_SECONDS = 30


# --------------------------

class ClubConnectStates(StatesGroup):
    waiting_for_cell_choice = State()
    waiting_for_player_name = State()


active_turn_timers: Dict[int, asyncio.Task] = {}
active_ttt_games: Dict[int, Dict[str, Any]] = {}

CLUB_PLAYERS: Dict[str, Set[str]] = {}
ALL_CLUBS: list[str] = []


def _normalize_club_name(raw_name: str) -> str:
    name = raw_name.strip().lower()
    # ВАЖНО: Дополни эту функцию всеми вариантами написания твоих 10 клубов из JSON
    # и к какому каноническому виду их приводить (все в нижнем регистре).
    name_replacements = {
        "пари сен-жермен": "псж",
        "бавария мюнхен": "бавария",
        "манчестер сити": "ман сити",
        # "real madrid": "реал мадрид", # Если в JSON могут быть англ. названия
        # "fc barcelona": "барселона",
        # "atletico madrid": "атлетико мадрид"
    }
    for old, new in name_replacements.items():
        name = name.replace(old, new)
    # Убедимся, что канонические названия не заменяются дальше
    # (например, если "псж" было в исходном ключе, оно останется "псж")
    canonical_names = ["псж", "бавария", "ман сити", "реал мадрид", "атлетико мадрид", "барселона", "челси", "ювентус",
                       "интер милан", "ливерпуль"]
    if name not in canonical_names:
        # Если после замен имя все еще не каноническое, возможно, нужна доп. логика или оно уже было каноническим
        pass  # Оставляем как есть, если не подошло под замены, но оно должно быть одним из 10
    return name


def load_and_process_club_players_data_from_pairs(file_path: Path) -> Tuple[Dict[str, Set[str]], list[str]]:
    processed_players_by_club: Dict[str, Set[str]] = {}
    all_found_club_names: Set[str] = set()
    if not file_path.exists():
        logger.error(f"Файл данных игроков {file_path} не найден!")
        return {}, []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data_pairs_format = json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки или парсинга JSON из {file_path}: {e}", exc_info=True)
        return {}, []
    if not isinstance(data_pairs_format, dict):
        logger.error(f"Содержимое {file_path} не является словарем. Ожидался формат пар клубов.")
        return {}, []
    for club_pair_key, players_list_in_pair in data_pairs_format.items():
        try:
            club1_raw, club2_raw = club_pair_key.split("↔")
            club1_normalized = _normalize_club_name(club1_raw)
            club2_normalized = _normalize_club_name(club2_raw)
        except ValueError:
            logger.warning(f"Неверный формат ключа пары клубов: '{club_pair_key}'. Пропускаем.")
            continue
        if not club1_normalized or not club2_normalized:
            logger.warning(f"Одно из названий клубов пустое после нормализации в ключе: '{club_pair_key}'. Пропускаем.")
            continue
        all_found_club_names.add(club1_normalized);
        all_found_club_names.add(club2_normalized)
        if not isinstance(players_list_in_pair, list):
            logger.warning(f"Для пары '{club_pair_key}' значение не список. Пропускаем.")
            continue
        for player_entry in players_list_in_pair:
            if not isinstance(player_entry, dict):
                logger.warning(f"Запись игрока для '{club_pair_key}' не словарь: {player_entry}. Пропускаем.")
                continue
            player_surname_raw = player_entry.get("Игрок")
            if not player_surname_raw or not isinstance(player_surname_raw, str):
                logger.warning(
                    f"Отсутствует/некорректная фамилия игрока в {player_entry} для '{club_pair_key}'. Пропускаем.")
                continue
            player_surname = player_surname_raw.strip().lower()
            if not player_surname: continue
            processed_players_by_club.setdefault(club1_normalized, set()).add(player_surname)
            processed_players_by_club.setdefault(club2_normalized, set()).add(player_surname)
    final_club_list = sorted(list(all_found_club_names))
    logger.info(
        f"Обработано. Уникальных клубов: {len(final_club_list)}. Записей фамилий: {sum(len(s) for s in processed_players_by_club.values())}")
    # Фильтруем processed_players_by_club, оставляя только те клубы, которые есть в final_club_list (на всякий случай)
    # И убеждаемся, что в ALL_CLUBS только те клубы, для которых есть игроки.
    final_processed_players = {club: players for club, players in processed_players_by_club.items() if
                               club in final_club_list and players}
    final_club_list_with_players = sorted(list(final_processed_players.keys()))

    return final_processed_players, final_club_list_with_players


CLUB_PLAYERS, ALL_CLUBS = load_and_process_club_players_data_from_pairs(Path(CLUB_PLAYERS_JSON))

async def init_ttt_db() -> None:
    """Инициализирует или обновляет таблицы для Club Connect."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Убрали PRIMARY KEY с chat_id, чтобы хранить историю
        # Добавили все необходимые поля для восстановления
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ttt_games (
                game_id TEXT PRIMARY KEY, -- Уникальный ID игры
                chat_id INTEGER NOT NULL,
                player_x_id INTEGER NOT NULL,
                player_o_id INTEGER NOT NULL,
                board_state TEXT,
                current_turn_symbol TEXT,
                clubs_rows TEXT,
                clubs_cols TEXT,
                round_start_time INTEGER,
                status TEXT, -- 'active', 'finished', 'canceled'
                winner_id INTEGER,
                created_at INTEGER,
                ended_at INTEGER
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ttt_leaderboard (
                user_id INTEGER PRIMARY KEY,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0
            );
        """)
        # Безопасное добавление колонок, если их нет
        try:
            # Эти колонки уже были, но для надежности оставим
            await db.execute("ALTER TABLE ttt_games ADD COLUMN board_state TEXT;")
            await db.execute("ALTER TABLE ttt_games ADD COLUMN current_turn_symbol TEXT;")
        except aiosqlite.OperationalError:
            pass # Колонки уже существуют
        await db.commit()


async def load_active_games_from_db():
    """Загружает активные игры из БД в память при старте бота."""
    global active_ttt_games
    logger.info("Загрузка активных игр Club Connect из БД...")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM ttt_games WHERE status = 'active'")
        active_games_rows = await cursor.fetchall()

    loaded_count = 0
    for game_row in active_games_rows:
        try:
            player_x = await bot.get_chat(game_row['player_x_id'])
            player_o = await bot.get_chat(game_row['player_o_id'])

            game_data = {
                "game_id": game_row['game_id'],
                "chat_id": game_row['chat_id'],
                "player_x_id": player_x.id,
                "player_o_id": player_o.id,
                "player_x_user": player_x,
                "player_o_user": player_o,
                "board_state": game_row['board_state'],
                "current_turn_symbol": game_row['current_turn_symbol'],
                "clubs_rows": game_row['clubs_rows'].split(','),
                "clubs_cols": game_row['clubs_cols'].split(','),
                "round_start_time": game_row['round_start_time'],
                "status": "active",
                "winner_id": None,
                "created_at": game_row['created_at']
            }
            active_ttt_games[game_row['chat_id']] = game_data

            # Перезапускаем таймер для текущего хода
            current_player_id = player_x.id if game_data['current_turn_symbol'] == 'X' else player_o.id
            asyncio.create_task(
                start_turn_timer(game_row['chat_id'], game_data['current_turn_symbol'], current_player_id))

            loaded_count += 1
        except Exception as e:
            logger.error(f"Не удалось восстановить игру {game_row['game_id']} из чата {game_row['chat_id']}: {e}")
            # Можно пометить игру как 'error' в БД
            async with aiosqlite.connect(DB_PATH) as db_err:
                await db_err.execute("UPDATE ttt_games SET status = 'error' WHERE game_id = ?", (game_row['game_id'],))
                await db_err.commit()

    if loaded_count > 0:
        logger.info(f"Успешно восстановлено {loaded_count} активных игр.")

@router.startup()
async def on_startup_club_connect():
    await init_ttt_db()
    await load_active_games_from_db()  # <--- ДОБАВИТЬ ЭТУ СТРОКУ
    if not CLUB_PLAYERS:
        logger.warning("ClubConnect: CLUB_PLAYERS пуст.")
    elif not ALL_CLUBS:
        logger.warning("ClubConnect: ALL_CLUBS пуст.")
    logger.info(
        f"ClubConnect запущен. Тест.режим: {FIXED_CLUBS_FOR_TESTING}. Клубов в ALL_CLUBS: {len(ALL_CLUBS)}. Таймер: {MOVE_TIMEOUT_SECONDS}с.")


def mention_user(u: types.User) -> str:
    name = u.full_name.replace("<", "<").replace(">", ">") if u.full_name else (
        u.username.replace("<", "<").replace(">", ">") if u.username else str(u.id))
    return f'<a href="tg://user?id={u.id}">{name}</a>'

def get_ttt_invite_keyboard(player1_id: int, player2_id: int) -> types.InlineKeyboardMarkup:
    """Создает инлайн-клавиатуру для приглашения в игру Club Connect."""
    accept_callback = f"ttt_accept:{player1_id}:{player2_id}"
    decline_callback = f"ttt_decline:{player1_id}:{player2_id}"
    buttons = [
        [
            types.InlineKeyboardButton(text="✅ Принять", callback_data=accept_callback),
            types.InlineKeyboardButton(text="❌ Отклонить", callback_data=decline_callback)
        ]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)
def _pick_three_clubs_for_both_random_fallback() -> Tuple[list[str], list[str]]:
    global ALL_CLUBS
    if not ALL_CLUBS or len(ALL_CLUBS) < 6: logger.warning(f"Fallback: Мало клубов ({len(ALL_CLUBS)})."); return [], []
    shuffled = random.sample(ALL_CLUBS, k=len(ALL_CLUBS));
    r, c = shuffled[:3], shuffled[3:6];
    return r, c


def pick_three_clubs_for_both() -> Tuple[list[str], list[str]]:
    global ALL_CLUBS
    if FIXED_CLUBS_FOR_TESTING:
        # Пример фиксированных клубов (должны быть нормализованы, если нужно)
        fixed_r_normalized = [_normalize_club_name(c) for c in ["реал мадрид", "челси", "псж"]]
        fixed_c_normalized = [_normalize_club_name(c) for c in ["барселона", "интер милан", "ювентус"]]
        valid_r = [c for c in fixed_r_normalized if c in ALL_CLUBS]
        valid_c = [c for c in fixed_c_normalized if c in ALL_CLUBS]
        if len(valid_r) == 3 and len(valid_c) == 3:
            logger.info(f"Фикс.клубы: R={valid_r},C={valid_c}");
            return valid_r, valid_c
        else:
            logger.warning(f"Не все фикс.клубы найдены. R:{valid_r},C:{valid_c}. ALL_CLUBS: {ALL_CLUBS}. Рандом.");
            return _pick_three_clubs_for_both_random_fallback()
    else:
        if not ALL_CLUBS or len(ALL_CLUBS) < 6:
            logger.warning(f"Мало клубов ({len(ALL_CLUBS)}) для 3+3. Игра может не начаться.");
            return [], []
        shuffled = random.sample(ALL_CLUBS, k=len(ALL_CLUBS));
        clubs_rows = shuffled[:3]
        clubs_cols = shuffled[3:6]
        logger.info(f"Рандом.клубы: R={clubs_rows},C={clubs_cols}");
        return clubs_rows, clubs_cols


def check_winner(bs: str) -> Optional[str]:
    lines = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)];
    for i, j, k in lines:
        if bs[i] != "_" and bs[i] == bs[j] == bs[k]: return bs[i]
    return None


def render_board_mono_and_markup(bs: str, cr: list[str], cc: list[str]) -> Tuple[str, InlineKeyboardMarkup]:
    def sc(n: str) -> str:
        return _normalize_club_name(n)[:4].capitalize().ljust(5)

    hdr = "      " + "".join([sc(cn) for cn in cc]);
    bl = ["<code>" + hdr.rstrip() + "</code>"];
    kbb = []
    for ri, crn in enumerate(cr):
        lt = sc(crn) + "| ";
        br = []
        for ci in range(len(cc)):
            idx = ri * len(cc) + ci;
            sym = bs[idx] if idx < len(bs) else " "
            cd, ie = ("⬜️", True) if sym == "_" else (("❌" if sym == "X" else "⭕️"), False)
            lt += cd + "  ";
            br.append(InlineKeyboardButton(text=f"{ri + 1},{ci + 1}" if ie else cd,
                                           callback_data=f"ttt_cell_{ri}_{ci}" if ie else "ttt_ignore"))
        bl.append("<code>" + lt.rstrip() + "</code>");
        kbb.append(br)
    return "\n".join(bl), InlineKeyboardMarkup(inline_keyboard=kbb)


async def безопасное_редактирование_разметки(m: Optional[types.Message], rmk: Optional[InlineKeyboardMarkup]):
    if not m: return
    try:
        await m.edit_reply_markup(reply_markup=rmk)
    except TelegramBadRequest as e:
        logger.warning(f"Не ред.разметку (msg_id {m.message_id}): {e}")
    except Exception as e:
        logger.error(f"Ошибка ред.разметки (msg_id {m.message_id}): {e}", exc_info=True)


async def _turn_timeout_fired(chat_id: int, expected_turn_symbol: str, timed_out_player_id: int):
    global active_ttt_games, active_turn_timers
    logger.info(f"Таймер истек chat={chat_id},ход={expected_turn_symbol} от {timed_out_player_id}");
    game = active_ttt_games.get(chat_id)
    if game and game["status"] == "active" and game["current_turn_symbol"] == expected_turn_symbol and \
            ((expected_turn_symbol == "X" and game["player_x_id"] == timed_out_player_id) or \
             (expected_turn_symbol == "O" and game["player_o_id"] == timed_out_player_id)):
        tpo = game["player_x_user"] if expected_turn_symbol == "X" else game["player_o_user"];
        ns = "O" if expected_turn_symbol == "X" else "X";
        npo = game["player_o_user"] if expected_turn_symbol == "X" else game["player_x_user"]
        await bot.send_message(chat_id,
                               f"⏱ Время вышло для {mention_user(tpo)} ({expected_turn_symbol})!\nХод к {mention_user(npo)} ({ns}).")
        game["current_turn_symbol"] = ns;
        game["round_start_time"] = int(time.time());
        await _update_ttt_game_in_db(game)
        btxt, bmkp = render_board_mono_and_markup(game["board_state"], game["clubs_rows"], game["clubs_cols"]);
        mp = [btxt, f"Ход: {mention_user(npo)} ({ns}). Выберите клетку."]
        await bot.send_message(chat_id, "\n".join(mp), reply_markup=bmkp, parse_mode="HTML");
        asyncio.create_task(start_turn_timer(chat_id, game["current_turn_symbol"], npo.id))
    else:
        logger.info(f"Таймер chat={chat_id}(ход {expected_turn_symbol}) истек,но игра/ход изменились.")

    # Удаляем задачу из словаря только если она там есть и это та самая задача, которая сейчас выполняется
    # asyncio.current_task() здесь будет ссылаться на задачу _turn_timeout_fired, а не на исходный таймер _timer_logic
    # Поэтому просто удаляем по chat_id, если задача завершена
    task_in_dict = active_turn_timers.get(chat_id)
    if task_in_dict and task_in_dict.done():  # Если задача завершилась (не важно, отменена или выполнена)
        if chat_id in active_turn_timers:  # Доп. проверка перед удалением
            del active_turn_timers[chat_id]
        logger.debug(f"Завершившаяся/отмененная задача таймера удалена из active_turn_timers для chat_id={chat_id}")


async def start_turn_timer(chat_id: int, turn_symbol: str, player_id: int):
    global active_turn_timers
    cancel_turn_timer(chat_id)  # Отменяем любой предыдущий таймер для этого чата
    logger.info(
        f"Запуск таймера на {MOVE_TIMEOUT_SECONDS}с для chat_id={chat_id}, ход: {turn_symbol} (игрок {player_id})")

    async def _timer_logic_internal():  # Переименовал, чтобы не конфликтовать с локальной переменной task
        global active_turn_timers
        # Получаем ссылку на текущую задачу, чтобы сравнить в finally
        this_task_obj = asyncio.current_task()
        try:
            await asyncio.sleep(MOVE_TIMEOUT_SECONDS)
            logger.debug(f"Asyncio.sleep завершен для таймера chat_id={chat_id} (ожидался ход {turn_symbol})")
            # Проверяем, что этот таймер все еще актуален (т.е. не был заменен новым вызовом start_turn_timer, который уже отменил бы этот)
            # и что это именно та задача, которая сейчас должна сработать
            if chat_id in active_turn_timers and active_turn_timers[chat_id] is this_task_obj:
                await _turn_timeout_fired(chat_id, turn_symbol, player_id)
        except asyncio.CancelledError:
            logger.info(f"Таймер для chat_id={chat_id} (ход {turn_symbol}, игрок {player_id}) был корректно отменен.")
            # Ничего не делаем, если отменен
        finally:
            # Убеждаемся, что задача удалена из словаря, если она там еще есть и это она
            if chat_id in active_turn_timers and active_turn_timers[chat_id] is this_task_obj:
                del active_turn_timers[chat_id]
                logger.debug(
                    f"Задача таймера (после _timer_logic_internal) удалена из active_turn_timers для chat_id={chat_id}")

    task_obj = asyncio.create_task(_timer_logic_internal())  # Переименовал task -> task_obj
    active_turn_timers[chat_id] = task_obj


def cancel_turn_timer(chat_id: int):
    global active_turn_timers
    task_to_cancel = active_turn_timers.pop(chat_id, None)  # Удаляем и получаем задачу
    if task_to_cancel and not task_to_cancel.done():
        task_to_cancel.cancel()
        logger.info(f"Таймер для chat_id={chat_id} отменен.")
    elif task_to_cancel:  # Задача была, но уже выполнена (done)
        logger.debug(f"Попытка отменить уже выполненный/отмененный таймер для chat_id={chat_id}.")
    else:  # Задачи не было
        logger.debug(f"Попытка отменить несуществующий таймер для chat_id={chat_id}.")


@router.message(Command("ttt"))
async def cmd_ttt_start(message: types.Message):
    """Отправляет приглашение на игру Club Connect по реплаю на сообщение."""
    chat_id = message.chat.id
    initiator = message.from_user

    if not initiator: return

    # Проверка, что команда отправлена в группе
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("❌ Эта игра доступна только в группах.")
        return

    # Проверка, что это ответ на сообщение
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("❌ Чтобы начать игру, ответьте на сообщение оппонента командой /ttt.")
        return

    opponent = message.reply_to_message.from_user

    # Проверки на себя, бота и активную игру
    if opponent.id == initiator.id:
        await message.answer("❌ Нельзя играть с собой.")
        return
    if opponent.is_bot:
        await message.answer("❌ Нельзя играть с ботом.")
        return
    if chat_id in active_ttt_games and active_ttt_games[chat_id]["status"] == "active":
        await message.answer("❌ В этом чате уже идёт игра. Отмените её командой /cancel.")
        return

    # Создаем и отправляем приглашение
    keyboard = get_ttt_invite_keyboard(initiator.id, opponent.id)
    invite_text = (
        f"⚽️ {mention_user(opponent)}, игрок {mention_user(initiator)} "
        f"вызывает тебя на игру «Крестики-Нолики»!\n\n"  # <-- ИЗМЕНЕНО
        f"Принимаешь вызов?"
    )

    sent_message = await message.answer(invite_text, reply_markup=keyboard, parse_mode="HTML")

    # Добавляем авто-отклонение через 60 секунд (как в дуэлях)
    async def auto_decline_task():
        await asyncio.sleep(60)
        try:
            await bot.edit_message_text(
                chat_id=sent_message.chat.id,
                message_id=sent_message.message_id,
                text=f"Вызов на игру от {mention_user(initiator)} "
                     f"для {mention_user(opponent)} истёк.",
                reply_markup=None,
                parse_mode="HTML"
            )
        except TelegramBadRequest:
            # Сообщение уже было изменено или удалено, ничего страшного
            pass

    asyncio.create_task(auto_decline_task())


@router.callback_query(F.data.startswith("ttt_accept:"))
async def cq_ttt_accept(callback: types.CallbackQuery, state: FSMContext):
    """Обрабатывает принятие приглашения на игру."""
    global active_ttt_games
    _, p1_id, p2_id = callback.data.split(":")
    player_x_id, player_o_id = int(p1_id), int(p2_id)

    # Проверка, что кнопку нажал именно тот, кого вызывали
    if callback.from_user.id != player_o_id:
        await callback.answer("Это приглашение не для вас!", show_alert=True)
        return

    await callback.answer("Вызов принят! Начинаем игру...")
    # Убираем кнопки с сообщения о приглашении
    await callback.message.edit_reply_markup(reply_markup=None)

    # Проверка на случай, если кто-то успел начать другую игру
    if callback.message.chat.id in active_ttt_games and active_ttt_games[callback.message.chat.id][
        "status"] == "active":
        await callback.message.answer("Пока вы думали, в чате уже началась другая игра.")
        return

    # --- СЮДА ПЕРЕЕХАЛА ВСЯ ЛОГИКА СОЗДАНИЯ ИГРЫ ИЗ СТАРОЙ cmd_ttt_start ---
    initiator = await bot.get_chat(player_x_id)
    opponent = await bot.get_chat(player_o_id)
    chat_id = callback.message.chat.id
    now_ts = int(time.time())
    game_id = f"ttt_{chat_id}_{now_ts}"  # Создаем уникальный ID

    if not ALL_CLUBS:
        await callback.message.answer("⚠️ Ошибка: Список клубов пуст. Не могу начать игру.")
        return

    clubs_r, clubs_c = pick_three_clubs_for_both()
    if not clubs_r or not clubs_c or len(clubs_r) != 3 or len(clubs_c) != 3:
        await callback.message.answer(
            f"⚠️ Ошибка: Для игры нужно 3x3 клуба. Выбрано {len(clubs_r)}x{len(clubs_c)}. Не могу начать игру.")
        return

    initial_state_str = "_" * 9

    game_data_dict = {
        "game_id": game_id,
        "chat_id": chat_id,
        "player_x_id": initiator.id,
        "player_o_id": opponent.id,
        "player_x_user": initiator,
        "player_o_user": opponent,
        "board_state": initial_state_str,
        "current_turn_symbol": "X",
        "clubs_rows": clubs_r,
        "clubs_cols": clubs_c,
        "round_start_time": now_ts,
        "status": "active",
        "winner_id": None,
        "created_at": now_ts
    }
    active_ttt_games[chat_id] = game_data_dict

    # Сохранение в БД
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ttt_games (game_id, chat_id, player_x_id, player_o_id, board_state, current_turn_symbol, clubs_rows, clubs_cols, round_start_time, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                game_id, chat_id, initiator.id, opponent.id, initial_state_str, "X",
                ",".join(clubs_r), ",".join(clubs_c), now_ts, "active", now_ts
            )
        )
        await db.commit()

    # Отправка игрового поля
    board_text_str, board_markup_obj = render_board_mono_and_markup(initial_state_str, clubs_r, clubs_c)
    message_parts_list = [
        "⚽️ <b>«Крестики-Нолики»</b> ⚽️",  # <-- ИЗМЕНЕНО
        board_text_str,
        f"Игроки: {mention_user(initiator)} (❌) vs {mention_user(opponent)} (⭕️)",
        f"Ход: {mention_user(initiator)} (❌). Выберите клетку."
    ]
    await callback.message.answer("\n".join(message_parts_list), reply_markup=board_markup_obj, parse_mode="HTML")

    # Установка FSM и запуск таймера
    await state.set_state(ClubConnectStates.waiting_for_cell_choice)
    await state.update_data(game_chat_id=chat_id)
    asyncio.create_task(start_turn_timer(chat_id, "X", initiator.id))


@router.callback_query(F.data.startswith("ttt_decline:"))
async def cq_ttt_decline(callback: types.CallbackQuery):
    """Обрабатывает отклонение приглашения на игру."""
    _, p1_id, p2_id = callback.data.split(":")
    player1_id, player2_id = int(p1_id), int(p2_id)

    # Отклонить может либо тот, кого вызвали, либо сам инициатор
    if callback.from_user.id not in (player1_id, player2_id):
        return await callback.answer("Это приглашение не для вас!", show_alert=True)

    await callback.answer("Вызов отклонен.")

    initiator = await bot.get_chat(player1_id)
    declined_by = await bot.get_chat(callback.from_user.id)

    await callback.message.edit_text(
        f"🚫 {mention_user(declined_by)} отклонил(а) вызов на игру от {mention_user(initiator)}.",
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("ttt_cell_"))  # <--- ВРЕМЕННО БЕЗ StateFilter
async def cq_ttt_cell_choice(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer()

    user_id = cb.from_user.id
    msg_obj_cb = cb.message
    chat_id_cb = msg_obj_cb.chat.id if msg_obj_cb else 0

    logger.info(f"--- cq_ttt_cell_choice (user={user_id}, chat_cb={chat_id_cb}) ---")
    current_fsm_state_for_user = await state.get_state()
    current_fsm_data_for_user = await state.get_data()
    logger.info(
        f"Data: {cb.data}, FSM state for user: {current_fsm_state_for_user}, FSM data for user: {current_fsm_data_for_user}")

    if not msg_obj_cb:
        logger.error("cq_ttt_cell_choice: cb.message is None. Невозможно продолжить.")
        return

    # --- НОВЫЙ ЛОГ ---
    logger.info(f"Содержимое active_ttt_games перед get: {active_ttt_games}")
    # ------------------
    game = active_ttt_games.get(chat_id_cb)

    if not game or game["status"] != "active":
        logger.warning(
            f"Нет активной игры в active_ttt_games для chat_id={chat_id_cb} или статус не active. Game object: {game}")
        await безопасное_редактирование_разметки(msg_obj_cb, None)
        await msg_obj_cb.answer("Игра не найдена или завершена (возможно, из-за ошибки).")
        # Очищаем состояние FSM для ТЕКУЩЕГО пользователя, если оно было связано с ЭТИМ чатом
        if current_fsm_data_for_user.get("game_chat_id") == chat_id_cb:
            await state.clear()
            logger.info(
                f"FSM состояние для user={user_id} в chat_id={chat_id_cb} очищено (игра не найдена/не активна).")
        return

    current_player_id_ingame = game["player_x_id"] if game["current_turn_symbol"] == "X" else game["player_o_id"]
    logger.info(f"Проверка хода: callback_user_id={user_id}, current_player_id_ingame={current_player_id_ingame}")

    if user_id != current_player_id_ingame:
        logger.info("Попытка хода не от текущего игрока.")
        await msg_obj_cb.answer("Сейчас не ваш ход!")
        return

        # Если это ход текущего игрока, УСТАНАВЛИВАЕМ (или обновляем) game_chat_id в его FSM
    # и переводим в состояние ожидания клетки (если он был None) или оставляем
    current_state_val = await state.get_state()
    if current_state_val is None:  # Если игрок был не в состоянии, вводим его
        await state.set_state(ClubConnectStates.waiting_for_cell_choice)
        logger.info(f"User {user_id} был в None state, установлен в waiting_for_cell_choice")

    await state.update_data(game_chat_id=chat_id_cb)
    logger.info(
        f"FSM data для user={user_id} в chat={chat_id_cb} обновлено/установлено: game_chat_id={chat_id_cb}. Состояние: {await state.get_state()}")

    parts = cb.data.split("_")
    if len(parts) != 4 or parts[0] != "ttt" or parts[1] != "cell":
        logger.warning(f"Неверный формат callback_data: {cb.data}")
        await msg_obj_cb.answer("Ошибка кнопки (формат).")
        return

    try:
        r_idx, c_idx = int(parts[2]), int(parts[3])
    except ValueError:
        logger.warning(
            f"Ошибка преобразования координат из callback_data: '{parts[2]}', '{parts[3]}' (оригинал: {cb.data})")
        await msg_obj_cb.answer("Ошибка кнопки (неверные данные координат).")
        return

    if not (0 <= r_idx < 3 and 0 <= c_idx < 3):
        logger.warning(f"Неверные координаты из callback_data: r={r_idx}, c={c_idx}")
        await msg_obj_cb.answer("Неверные координаты клетки.")
        return

    board_idx = r_idx * 3 + c_idx
    if board_idx >= len(game["board_state"]) or game["board_state"][board_idx] != "_":
        logger.info(
            f"Клетка ({r_idx + 1},{c_idx + 1}) занята или ошибка индекса (idx={board_idx}, len={len(game['board_state'])}, val='{game['board_state'][board_idx] if board_idx < len(game['board_state']) else 'OOB'}').")
        await msg_obj_cb.answer("Эта клетка уже занята!")
        return

    await безопасное_редактирование_разметки(msg_obj_cb, None)

    await msg_obj_cb.answer(f"Выбрана клетка ({r_idx + 1},{c_idx + 1}). Введите фамилию (кириллицей, нижний регистр):")

    await state.update_data(chosen_r_idx=r_idx, chosen_c_idx=c_idx)
    await state.set_state(ClubConnectStates.waiting_for_player_name)
    logger.info(f"FSM для user={user_id} переведен в waiting_for_player_name. Data: {await state.get_data()}")
    logger.info(f"--- cq_ttt_cell_choice КОНЕЦ ---")

@router.callback_query(F.data == "ttt_ignore", StateFilter(None, ClubConnectStates.waiting_for_cell_choice))
async def cq_ttt_ignore(cb: types.CallbackQuery): await cb.answer("Эта клетка занята.");logger.debug(
    f"Нажата ttt_ignore user_id={cb.from_user.id}")


@router.message(ClubConnectStates.waiting_for_player_name)
async def msg_ttt_player_name_input(message: types.Message, state: FSMContext):
    global active_ttt_games  # Для del active_ttt_games[game_id]

    fsm_data = await state.get_data()
    game_id = fsm_data.get("game_chat_id")

    logger.info(f"--- msg_ttt_player_name_input (user={message.from_user.id}, game_id={game_id}) ---")
    logger.info(f"Текст:'{message.text}', FSM data:{fsm_data}")

    r_idx, c_idx = fsm_data.get("chosen_r_idx"), fsm_data.get("chosen_c_idx")

    if game_id is None or r_idx is None or c_idx is None or game_id not in active_ttt_games:
        logger.error(f"Критическая ошибка FSM данных или игра не найдена: game_id={game_id}, r={r_idx}, c={c_idx}")
        await message.answer("Ошибка состояния игры. Пожалуйста, начните новую игру командой /ttt.")
        await state.clear()
        return

    game = active_ttt_games.get(game_id)  # Получаем актуальные данные игры
    if not game or game["status"] != "active":  # Дополнительная проверка
        logger.warning(
            f"Игра {game_id} не найдена в active_ttt_games или уже не активна (статус: {game.get('status') if game else 'N/A'})")
        await message.answer("Игра не активна или завершена.")
        await state.clear()
        return

    current_pid_ingame = game["player_x_id"] if game["current_turn_symbol"] == "X" else game["player_o_id"]
    if message.from_user.id != current_pid_ingame:
        logger.info(
            f"Ввод имени не от текущего игрока (ожидался {current_pid_ingame}, получен от {message.from_user.id})")
        return

    cancel_turn_timer(game_id)  # Игрок сделал ход (прислал сообщение), отменяем его текущий таймер

    player_name_guess_raw = message.text.strip()
    if not player_name_guess_raw:
        await message.answer("Вы не ввели фамилию. Попробуйте еще раз.")
        # Перезапускаем таймер для этого же игрока, так как он не сделал валидный ход
        asyncio.create_task(start_turn_timer(game_id, game["current_turn_symbol"], current_pid_ingame))
        return

    player_name_guess_lower = player_name_guess_raw.lower()
    board_idx = r_idx * 3 + c_idx

    # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
    club_r, club_c = game["clubs_rows"][r_idx], game["clubs_cols"][c_idx]
    # --------------------------

    players_r_set = CLUB_PLAYERS.get(club_r, set())
    players_c_set = CLUB_PLAYERS.get(club_c, set())
    valid_names_for_cell = players_r_set.intersection(players_c_set)

    logger.debug(
        f"Проверка: '{player_name_guess_lower}' для клубов ({club_r},{club_c}). Найденные валидные имена (пересечение): {valid_names_for_cell}")

    next_turn_sym = "O" if game["current_turn_symbol"] == "X" else "X"
    next_player_obj = game["player_o_user"] if game["current_turn_symbol"] == "X" else game["player_x_user"]

    pass_turn = True
    found_match_name_in_db = None

    if not valid_names_for_cell:
        logger.warning(f"Для клубов ({club_r},{club_c}) не найдено пересечений игроков в базе!")
        await message.answer(
            f"🤔 Для клубов ({club_r.capitalize()} и {club_c.capitalize()}) в базе нет общих игроков. Ход переходит к {mention_user(next_player_obj)} ({next_turn_sym}).")
    else:
        best_s = 0
        threshold = 80

        for name_db_loop_var in valid_names_for_cell:
            score = fuzz.token_set_ratio(player_name_guess_lower, name_db_loop_var)
            logger.debug(
                f"Сравнение (token_set_ratio): '{player_name_guess_lower}' vs '{name_db_loop_var}', схожесть: {score}%")
            if score > best_s:
                best_s = score
                # Если нашли совпадение выше порога, запоминаем его как кандидата
                if score >= threshold:
                    found_match_name_in_db = name_db_loop_var
                    # Если хотим самое лучшее, не делаем break. Если первое подходящее - break.
                    # Пока ищем лучшее из тех, что прошли порог.

        # После цикла проверяем, было ли найдено достаточно хорошее совпадение
        if found_match_name_in_db and best_s >= threshold:
            logger.info(
                f"Игрок '{player_name_guess_raw}' принят как '{found_match_name_in_db.capitalize()}' (лучшая схожесть: {best_s}%)")
            pass_turn = False
        else:
            logger.info(
                f"Игрок '{player_name_guess_raw}' не найден с достаточной похожестью (лучшая: {best_s}% из порога {threshold}%). Ход передается.")
            await message.answer(
                f"❌ «{player_name_guess_raw.capitalize()}» не очень похож на подходящих игроков. Ход к {mention_user(next_player_obj)} ({next_turn_sym}).")

    game_ended_this_turn = False  # Флаг, что игра завершилась на этом ходу
    if pass_turn:
        game["current_turn_symbol"] = next_turn_sym
    else:  # Успешный ход (pass_turn is False)
        logger.info(
            f"Имя '{found_match_name_in_db.capitalize() if found_match_name_in_db else player_name_guess_raw}' принято для клетки ({r_idx + 1},{c_idx + 1}).")
        new_b_list = list(game["board_state"])
        new_b_list[board_idx] = game["current_turn_symbol"]
        game["board_state"] = "".join(new_b_list)

        winner = check_winner(game["board_state"])
        if winner:
            game_ended_this_turn = True
            logger.info(f"Игра {game_id} завершена. Победитель: {winner}")
            game.update(
                {"status": "finished", "winner_id": game["player_x_id"] if winner == "X" else game["player_o_id"],
                 "ended_at": int(time.time())})
            await _update_ttt_game_in_db(game)
            w_user_obj = game["player_x_user"] if winner == "X" else game["player_o_user"]
            l_id = game["player_o_id"] if winner == "X" else game["player_x_id"]
            await _save_ttt_result_db(game["winner_id"], l_id)
            f_b_txt, _ = render_board_mono_and_markup(game["board_state"], game["clubs_rows"], game["clubs_cols"])
            await message.answer(f"{f_b_txt}\n🏆 <b>Победа {mention_user(w_user_obj)} ({winner})!</b>",
                                 parse_mode="HTML")
        elif "_" not in game["board_state"]:  # Ничья
            game_ended_this_turn = True
            logger.info(f"Игра {game_id} завершена. Ничья.")
            game.update({"status": "finished", "ended_at": int(time.time())})  # winner_id остается None
            await _update_ttt_game_in_db(game)
            await _save_ttt_draw_db(game["player_x_id"], game["player_o_id"])
            f_b_txt, _ = render_board_mono_and_markup(game["board_state"], game["clubs_rows"], game["clubs_cols"])
            await message.answer(f"{f_b_txt}\n🤝 <b>Ничья! Все клетки заполнены.</b>", parse_mode="HTML")
        else:
            # Игра продолжается, передаем ход
            game["current_turn_symbol"] = next_turn_sym
            logger.info(f"Ход передан игроку с символом {next_turn_sym}")

    if game_ended_this_turn:
        if game_id in active_ttt_games:
            del active_ttt_games[game_id]  # Удаляем игру из активных
        await state.clear()  # Очищаем состояние FSM для текущего пользователя
        return  # Выходим, новый таймер и поле не нужны

    # Если игра продолжается (ход передан или успешно сделан, но не конец игры)
    await _update_ttt_game_in_db(game)  # Сохраняем актуальное состояние игры в БД

    board_txt, board_mkp = render_board_mono_and_markup(game["board_state"], game["clubs_rows"], game["clubs_cols"])
    active_player_now_obj = game["player_x_user"] if game["current_turn_symbol"] == "X" else game["player_o_user"]
    msg_parts_upd = [board_txt,
                     f"Ход: {mention_user(active_player_now_obj)} ({game['current_turn_symbol']}). Выберите клетку."]
    await message.answer("\n".join(msg_parts_upd), reply_markup=board_mkp, parse_mode="HTML")

    # Устанавливаем состояние для ТЕКУЩЕГО пользователя (который только что сделал ход или ошибся)
    # чтобы он мог реагировать на кнопки в следующем сообщении, если ход вернется к нему
    await state.set_state(ClubConnectStates.waiting_for_cell_choice)
    await state.update_data(game_chat_id=game_id, chosen_r_idx=None, chosen_c_idx=None)  # Очищаем выбранные клетки
    logger.info(
        f"FSM для user={message.from_user.id} (после его хода/ошибки) -> waiting_for_cell_choice. Data: {await state.get_data()}")

    # Запускаем таймер для СЛЕДУЮЩЕГО игрока (кому перешел ход)
    next_player_for_timer_id = active_player_now_obj.id  # Это ID того, чей ход сейчас
    asyncio.create_task(start_turn_timer(game_id, game["current_turn_symbol"], next_player_for_timer_id))

    logger.info(f"--- msg_ttt_player_name_input КОНЕЦ ---")
async def _update_ttt_game_in_db(game_data: Dict[str, Any]): # Было g_data
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE ttt_games 
               SET state=?, turn=?, round_start_time=?, status=?, 
                   winner=?, ended_at=?, clubs_rows=?, clubs_cols=? 
               WHERE chat_id=?""",
            (
                game_data["board_state"],
                game_data["current_turn_symbol"],
                game_data["round_start_time"],
                game_data["status"],
                game_data.get("winner_id"),
                game_data.get("ended_at"),
                ",".join(game_data["clubs_rows"]),
                ",".join(game_data["clubs_cols"]),
                game_data["chat_id"]
            )
        )
        await db.commit()

async def _save_ttt_result_db(winner_id: int, loser_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ttt_leaderboard(user_id, wins)
               VALUES(?, 1)
               ON CONFLICT(user_id) DO UPDATE SET wins = wins + 1""",
            (winner_id,)
        )
        await db.execute(
            """INSERT INTO ttt_leaderboard(user_id, losses)
               VALUES(?, 1)
               ON CONFLICT(user_id) DO UPDATE SET losses = losses + 1""",
            (loser_id,)
        )
        await db.commit()

async def _save_ttt_draw_db(player_x_id: int, player_o_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        for player_id_loop_var in [player_x_id, player_o_id]: # Было p_id_loop_var
            await db.execute(
                """INSERT INTO ttt_leaderboard(user_id, draws)
                   VALUES(?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET draws = draws + 1""",
                (player_id_loop_var,)
            )
        await db.commit()


@router.message(Command("cancel"))
async def cmd_ttt_cancel(message: types.Message, state: FSMContext):
    global active_ttt_games
    chat_id = message.chat.id
    user = message.from_user
    if not user: return

    game = active_ttt_games.get(chat_id)
    if not game or game.get("status") != "active":
        await message.answer("Нет активной игры для отмены.")
        return

    # Проверки, что пользователь является участником игры...
    is_player_x = user.id == game["player_x_id"]
    is_player_o = user.id == game["player_o_id"]
    if not (is_player_x or is_player_o):
        await message.answer("Вы не являетесь участником этой игры.")
        return

    opponent_user = game["player_o_user"] if is_player_x else game["player_x_user"]
    cancel_requester_id = game.get("cancel_requester_id")

    if cancel_requester_id:
        if cancel_requester_id == opponent_user.id:
            logger.info(f"Отмена игры в чате {chat_id} подтверждена пользователем {user.id}.")

            # 1. Останавливаем таймер
            cancel_turn_timer(chat_id)

            # 2. Обновляем статус в словаре
            game["status"] = "canceled"
            game["ended_at"] = int(time.time())

            # 3. Сохраняем финальное состояние в БД
            await _update_ttt_game_in_db(game)

            # 4. Удаляем игру из активных в памяти
            if chat_id in active_ttt_games:
                del active_ttt_games[chat_id]

            # 5. Чистим FSM
            fsm_data = await state.get_data()
            if fsm_data.get("game_chat_id") == chat_id:
                await state.clear()

            # 6. Отправляем сообщение
            await message.answer(
                f"✅ Игра отменена по взаимному согласию. {mention_user(user)} подтвердил(а) отмену.",
                parse_mode="HTML"
            )
        elif cancel_requester_id == user.id:
            await message.answer("Вы уже отправили запрос на отмену. Ожидаем подтверждения от оппонента.")
    else:
        logger.info(f"Пользователь {user.id} инициировал отмену игры в чате {chat_id}.")
        game["cancel_requester_id"] = user.id
        await message.answer(
            f"❗️ {mention_user(user)} предлагает отменить игру.\n"
            f"Оппонент, {mention_user(opponent_user)}, должен также отправить команду /cancel для подтверждения.",
            parse_mode="HTML"
        )


@router.message(Command("surrender", "giveup"))
async def cmd_ttt_surrender(message: types.Message, state: FSMContext):
    global active_ttt_games
    chat_id = message.chat.id
    user = message.from_user
    if not user: return

    game = active_ttt_games.get(chat_id)
    if not game or game.get("status") != "active":
        await message.answer("Нет активной игры, чтобы сдаваться.")
        return

    # Проверки...
    is_player_x = user.id == game["player_x_id"]
    is_player_o = user.id == game["player_o_id"]
    if not (is_player_x or is_player_o):
        await message.answer("Вы не являетесь участником этой игры.")
        return

    winner_id = game["player_o_id"] if is_player_x else game["player_x_id"]
    loser_id = user.id
    winner_user = game["player_o_user"] if is_player_x else game["player_x_user"]

    logger.info(f"Игрок {loser_id} сдался в чате {chat_id}. Победитель: {winner_id}")

    # 1. Останавливаем таймер
    cancel_turn_timer(chat_id)

    # 2. Обновляем статус в словаре
    game["status"] = "finished"
    game["winner_id"] = winner_id
    game["ended_at"] = int(time.time())

    # 3. Сохраняем финальное состояние игры и результаты в лидерборд
    await _update_ttt_game_in_db(game)
    await _save_ttt_result_db(winner_id, loser_id)

    # 4. Удаляем игру из активных в памяти
    if chat_id in active_ttt_games:
        del active_ttt_games[chat_id]

    # 5. Чистим FSM
    await state.clear()

    # 6. Отправляем сообщение
    await message.answer(
        f"🏳️ {mention_user(user)} сдаётся! Победа присуждается {mention_user(winner_user)}!",
        parse_mode="HTML"
    )


@router.message(Command("draw", "peace"))
async def cmd_ttt_draw(message: types.Message, state: FSMContext):
    global active_ttt_games
    chat_id = message.chat.id
    user = message.from_user
    if not user: return

    game = active_ttt_games.get(chat_id)
    if not game or game.get("status") != "active":
        await message.answer("Нет активной игры.")
        return

    # Проверки...
    is_player_x = user.id == game["player_x_id"]
    is_player_o = user.id == game["player_o_id"]
    if not (is_player_x or is_player_o):
        await message.answer("Вы не участник этой игры.")
        return

    opponent_user = game["player_o_user"] if is_player_x else game["player_x_user"]
    draw_requester_id = game.get("draw_requester_id")

    if draw_requester_id:
        if draw_requester_id == opponent_user.id:
            logger.info(f"Ничья в чате {chat_id} подтверждена {user.id}.")

            # 1. Останавливаем таймер
            cancel_turn_timer(chat_id)

            # 2. Обновляем статус в словаре (winner_id остается None)
            game["status"] = "finished"
            game["ended_at"] = int(time.time())

            # 3. Сохраняем финальное состояние игры и результаты в лидерборд
            await _update_ttt_game_in_db(game)
            await _save_ttt_draw_db(game["player_x_id"], game["player_o_id"])

            # 4. Удаляем игру из активных в памяти
            if chat_id in active_ttt_games:
                del active_ttt_games[chat_id]

            # 5. Чистим FSM
            await state.clear()

            # 6. Отправляем сообщение
            await message.answer("🤝 Ничья по взаимному согласию!", parse_mode="HTML")
        elif draw_requester_id == user.id:
            await message.answer("Вы уже предложили ничью. Ожидаем ответа от оппонента.")
    else:
        logger.info(f"{user.id} предлагает ничью в чате {chat_id}.")
        game["draw_requester_id"] = user.id
        await message.answer(
            f"🤝 {mention_user(user)} предлагает ничью.\n"
            f"{mention_user(opponent_user)}, отправьте <code>/draw</code> для согласия.",
            parse_mode="HTML"
        )


@router.message(Command("surrender", "giveup"))
async def cmd_ttt_surrender(message: types.Message, state: FSMContext):
    """Позволяет игроку немедленно сдаться."""
    global active_ttt_games
    chat_id = message.chat.id
    user = message.from_user
    if not user: return

    game = active_ttt_games.get(chat_id)

    if not game or game.get("status") != "active":
        await message.answer("Нет активной игры, чтобы сдаваться.")
        return

    is_player_x = user.id == game["player_x_id"]
    is_player_o = user.id == game["player_o_id"]

    if not (is_player_x or is_player_o):
        await message.answer("Вы не являетесь участником этой игры.")
        return

    # Определяем победителя и проигравшего
    winner_id = game["player_o_id"] if is_player_x else game["player_x_id"]
    loser_id = user.id
    winner_user = game["player_o_user"] if is_player_x else game["player_x_user"]

    logger.info(f"Игрок {loser_id} сдался в чате {chat_id}. Победитель: {winner_id}")

    # Завершаем игру
    cancel_turn_timer(chat_id)
    game.update({
        "status": "finished",
        "winner_id": winner_id,
        "ended_at": int(time.time())
    })

    # Сохраняем результаты
    await _update_ttt_game_in_db(game)
    await _save_ttt_result_db(winner_id, loser_id)

    # Чистим кэш и состояние
    if chat_id in active_ttt_games:
        del active_ttt_games[chat_id]
    await state.clear()

    await message.answer(
        f"🏳️ {mention_user(user)} сдаётся! Победа присуждается {mention_user(winner_user)}!",
        parse_mode="HTML"
    )


@router.message(Command("draw", "peace"))
async def cmd_ttt_draw(message: types.Message, state: FSMContext):
    """Предложить или принять ничью."""
    global active_ttt_games
    chat_id = message.chat.id
    user = message.from_user
    if not user: return

    game = active_ttt_games.get(chat_id)

    if not game or game.get("status") != "active":
        await message.answer("Нет активной игры.")
        return

    is_player_x = user.id == game["player_x_id"]
    is_player_o = user.id == game["player_o_id"]

    if not (is_player_x or is_player_o):
        await message.answer("Вы не участник этой игры.")
        return

    opponent_user = game["player_o_user"] if is_player_x else game["player_x_user"]
    draw_requester_id = game.get("draw_requester_id")

    if draw_requester_id:
        if draw_requester_id == opponent_user.id:
            logger.info(f"Ничья в чате {chat_id} подтверждена {user.id}.")
            cancel_turn_timer(chat_id)
            game.update({"status": "finished", "ended_at": int(time.time())})  # winner_id остается None

            await _update_ttt_game_in_db(game)
            await _save_ttt_draw_db(game["player_x_id"], game["player_o_id"])

            if chat_id in active_ttt_games:
                del active_ttt_games[chat_id]
            await state.clear()

            await message.answer("🤝 Ничья по взаимному согласию!", parse_mode="HTML")
        elif draw_requester_id == user.id:
            await message.answer("Вы уже предложили ничью. Ожидаем ответа от оппонента.")
    else:
        logger.info(f"{user.id} предлагает ничью в чате {chat_id}.")
        game["draw_requester_id"] = user.id
        await message.answer(
            f"🤝 {mention_user(user)} предлагает ничью.\n"
            f"{mention_user(opponent_user)}, отправьте <code>/draw</code> для согласия.",
            parse_mode="HTML"
        )


@router.message(Command("ttt_mystats"))
async def cmd_ttt_mystats(message: types.Message):
    """Показывает личную статистику игрока."""
    user = message.from_user
    if not user: return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT wins, losses, draws FROM ttt_leaderboard WHERE user_id = ?", (user.id,))
        stats = await cursor.fetchone()

    if stats:
        wins, losses, draws = stats
        response = (
            f"📊 <b>Твоя статистика, {mention_user(user)}:</b>\n"
            f"🏆 Победы: <b>{wins}</b>\n"
            f"☠️ Поражения: <b>{losses}</b>\n"
            f"🤝 Ничьи: <b>{draws}</b>"
        )
    else:
        response = f"Ты еще не сыграл(а) ни одной игры, {mention_user(user)}! Начни с команды /ttt."

    await message.answer(response, parse_mode="HTML")


@router.message(Command("clubs"))
async def cmd_ttt_clubs(message: types.Message):
    """Напоминает клубы в текущей активной игре."""
    chat_id = message.chat.id
    game = active_ttt_games.get(chat_id)

    if not game or game.get("status") != "active":
        await message.answer("Сейчас нет активной игры в этом чате.")
        return

    clubs_r = ", ".join([c.capitalize() for c in game['clubs_rows']])
    clubs_c = ", ".join([c.capitalize() for c in game['clubs_cols']])

    response = (
        f"<b>Клубы в текущей игре:</b>\n"
        f"➡️ <b>По горизонтали:</b> {clubs_r}\n"
        f"⬇️ <b>По вертикали:</b> {clubs_c}"
    )
    await message.answer(response, parse_mode="HTML")


@router.message(Command("ttt_history"))
async def cmd_ttt_history(message: types.Message):
    """Показывает историю последних 5 игр в чате."""
    chat_id = message.chat.id

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT player_x, player_o, status, winner, ended_at 
               FROM ttt_games 
               WHERE chat_id = ? AND status IN ('finished', 'canceled') 
               ORDER BY ended_at DESC LIMIT 5""",
            (chat_id,)
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("В этом чате еще не было сыграно ни одной игры.")
        return

    lines = ["<b>📖 История последних 5 игр:</b>"]
    for p_x_id, p_o_id, status, winner_id, ended_at_ts in rows:
        try:
            p_x = await bot.get_chat(p_x_id)
            p_o = await bot.get_chat(p_o_id)
            p_x_mention = mention_user(p_x)
            p_o_mention = mention_user(p_o)
        except Exception:
            p_x_mention = f"Игрок({p_x_id})"
            p_o_mention = f"Игрок({p_o_id})"

        date_str = datetime.fromtimestamp(ended_at_ts).strftime('%d.%m.%Y')

        if status == 'canceled':
            lines.append(f"❌ {date_str}: Игра между {p_x_mention} и {p_o_mention} отменена.")
        elif winner_id:
            winner_mention = p_x_mention if winner_id == p_x_id else p_o_mention
            lines.append(f"🏆 {date_str}: {winner_mention} победил(а).")
        else:  # Ничья
            lines.append(f"🤝 {date_str}: Ничья между {p_x_mention} и {p_o_mention}.")

    await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("ttt_leaderboard"))
async def cmd_ttt_leaderboard(message: types.Message):
    lines = [
        "🏆 <b>Топ-10 игроков: «Крестики-Нолики»</b>",
        "<i>(Победы - Поражения - Ничьи)</i>\n"
    ]
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, wins, losses, draws FROM ttt_leaderboard ORDER BY wins DESC, draws DESC, losses ASC LIMIT 10"
            )
            rows_data = await cursor.fetchall()

        if not rows_data:
            await message.answer("🏆 Таблица лидеров «Крестики-Нолики» пока пуста.")
            return

        # Словарь с медалями
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        for rank_num, (user_id_lb, wins_lb, losses_lb, draws_lb) in enumerate(rows_data, 1):
            try:
                user_obj_lb = await bot.get_chat(user_id_lb)
                mention_lb = mention_user(user_obj_lb)
            except Exception:
                mention_lb = f"Игрок(<code>{user_id_lb}</code>)"

            # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
            # Получаем медаль или номер с точкой
            place = medals.get(rank_num, f"{rank_num}.")

            lines.append(f"{place} {mention_lb} — <b>{wins_lb}-{losses_lb}-{draws_lb}</b>")

        await message.answer("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Ошибка ttt_leaderboard: {e}", exc_info=True)
        await message.answer("❌ Ошибка при загрузке таблицы лидеров.")


@router.message(Command("ttt_help"))
async def cmd_ttt_help(message: types.Message):
    """Отправляет справку по всем командам игры."""
    help_text = [
        "<b>📜 Справка по игре Club Connect 📜</b>\n",
        "<b>Основные команды:</b>",
        "<code>/ttt</code> (в ответ на сообщение) - Начать новую игру с пользователем.",
        "<code>/cancel</code> - Предложить отмену текущей игры. Требует подтверждения от оппонента.",
        "<code>/surrender</code> - Немедленно сдаться, засчитав себе поражение.\n",
        "<b>Статистика и информация:</b>",
        "<code>/ttt_leaderboard</code> - Показать топ-5 игроков.",
        "<code>/ttt_mystats</code> - Показать вашу личную статистику.",
        "<code>/ttt_history</code> - Показать историю последних 5 игр в этом чате.",
        "<code>/clubs</code> - Напомнить, какие клубы участвуют в текущей игре.\n",
        "<b>Прочее:</b>",
        "<code>/draw</code> - Предложить ничью. Требует подтверждения.",
        "<code>/ttt_help</code> - Показать это сообщение."
    ]
    await message.answer("\n".join(help_text), parse_mode="HTML")