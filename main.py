import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from calendar import monthrange
from urllib.parse import quote
from dataclasses import dataclass
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Request, HTTPException
import httpx
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")


# =========================
# ДАННЫЕ
# =========================

@dataclass
class Deck:
    id: int
    name: str
    cards: list[str]
    mode: str
    win_rate: float
    games: int
    source: str = "manual"


decks = [
    Deck(
        id=1,
        name="Hog Rider Cycle",
        cards=[
            "Hog Rider",
            "Musketeer",
            "Cannon",
            "Fireball",
            "The Log",
            "Ice Spirit",
            "Skeletons",
            "Ice Golem",
        ],
        mode="ladder",
        win_rate=54.2,
        games=12000,
    ),
    Deck(
        id=2,
        name="Giant Graveyard",
        cards=[
            "Giant",
            "Graveyard",
            "Baby Dragon",
            "Tornado",
            "Poison",
            "Barbarian Barrel",
            "Ice Wizard",
            "Tombstone",
        ],
        mode="ladder",
        win_rate=53.7,
        games=9800,
    ),
]


favorites: dict[int, set[int]] = {}

next_deck_id = 3


# =========================
# СОСТОЯНИЯ
# =========================

class AddDeck(StatesGroup):
    name = State()
    cards = State()
    mode = State()
    win_rate = State()
    games = State()


class Search(StatesGroup):
    searching = State()


# =========================
# КЛАВИАТУРЫ
# =========================

def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔥 Мета", callback_data="meta"),
                InlineKeyboardButton(text="🏆 Топ колоды", callback_data="top"),
            ],
            [
                InlineKeyboardButton(text="👑 Топ 100 игроков", callback_data="top100"),
                InlineKeyboardButton(text="🎯 Испытания", callback_data="challenges"),
            ],
            [
                InlineKeyboardButton(text="🔍 Поиск", callback_data="search"),
                InlineKeyboardButton(text="⭐ Избранное", callback_data="favorites"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="add_deck"),
            ],
        ]
    )


def back_button():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")]
        ]
    )


def deck_keyboard(deck_id: int, starred: bool = False):
    # Отображаем звезду в зависимости от того, есть ли в избранном
    star_text = "⭐" if starred else "☆"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🃏 Открыть", callback_data=f"deck:{deck_id}"),
                InlineKeyboardButton(text=star_text, callback_data=f"fav:{deck_id}"),
            ]
        ]
    )


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def get_deck(deck_id: int) -> Deck | None:
    return next((deck for deck in decks if deck.id == deck_id), None)


def deck_text(deck: Deck) -> str:
    cards = "\n".join(f"• {card}" for card in deck.cards)

    return (
        f"🃏 <b>{deck.name}</b>\n\n"
        f"{cards}\n\n"
        f"🎮 Режим: <b>{deck.mode}</b>\n"
        f"📈 Win rate: <b>{deck.win_rate}%</b>\n"
        f"⚔️ Игр: <b>{deck.games:,}</b>\n"
        f"📡 Источник: <b>{deck.source}</b>"
    )


def is_admin(user_id: int) -> bool:
    admin_ids = os.getenv("ADMIN_IDS", "")

    if not admin_ids:
        return False

    try:
        admins = {int(x.strip()) for x in admin_ids.split(",") if x.strip()}
    except ValueError:
        return False

    return user_id in admins



# =========================
# ОФИЦИАЛЬНЫЙ API CLASH ROYALE
# =========================

def api_is_ready() -> bool:
    return bool(CR_API_KEY)


async def cr_get(path: str, params: dict | None = None):
    if not CR_API_KEY:
        raise RuntimeError("CLASH_ROYALE_API_KEY не задан.")

    headers = {
        "Authorization": f"Bearer {CR_API_KEY}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.get(
            f"{CR_API_BASE}{path}",
            headers=headers,
            params=params,
        )

        if response.status_code >= 400:
            body_preview = response.text[:500]
            raise RuntimeError(
                f"API {response.status_code} для {path}: {body_preview}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"API вернул не-JSON для {path}: {response.text[:300]}"
            ) from exc


def card_names(raw_cards) -> list[str]:
    result = []
    for card in raw_cards or []:
        if isinstance(card, dict):
            name = card.get("name")
            if name:
                result.append(str(name))
    return result


def deck_signature(cards: list[str]) -> tuple[str, ...]:
    return tuple(sorted(cards, key=str.lower))


def format_last_updated() -> str:
    if not cache_updated_at:
        return "ещё не обновлялись"
    local = cache_updated_at.astimezone()
    return local.strftime("%d.%m.%Y %H:%M")


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    absolute = year * 12 + (month - 1) + delta
    return absolute // 12, absolute % 12 + 1


def candidate_season_ids(count: int = 4) -> list[str]:
    """
    Clash Royale season IDs use YYYY-MM in season ranking endpoints.
    Try the current month first, then recent months.
    """
    now = datetime.now(timezone.utc)
    result = []

    for delta in range(0, -count, -1):
        year, month = shift_month(now.year, now.month, delta)
        result.append(f"{year:04d}-{month:02d}")

    return result


def parse_ranking_items(data) -> list[dict]:
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in ("items", "players"):
        value = data.get(key)
        if isinstance(value, list):
            return value

    nested = data.get("data")
    if isinstance(nested, list):
        return nested

    if isinstance(nested, dict):
        for key in ("items", "players"):
            value = nested.get(key)
            if isinstance(value, list):
                return value

    return []


async def fetch_top_100_players() -> list[dict]:
    """
    Получаем Top 100 из официального Supercell API.

    1) Сначала пробуем live Ranked endpoint.
    2) Если он пуст (это бывает в начале сезона), пробуем
       season-specific Path of Legend endpoints вида:
       /locations/global/pathoflegend/YYYY-MM/rankings/players
    3) Trophy Road оставляем только последним fallback.
    """
    global leaderboard_season_id, leaderboard_source_label

    diagnostics: list[str] = []

    # Текущий live Ranked leaderboard.
    live_endpoint = "/locations/global/pathoflegend/players"

    try:
        data = await cr_get(live_endpoint, {"limit": 100})
        items = parse_ranking_items(data)

        logging.info(
            "Live Ranked leaderboard: players=%s, keys=%s",
            len(items),
            list(data.keys()) if isinstance(data, dict) else "-",
        )

        if items:
            leaderboard_season_id = None
            leaderboard_source_label = "текущий Ranked"
            return items[:100]

        diagnostics.append(f"{live_endpoint}: items=0")

    except Exception as exc:
        logging.exception("Live Ranked leaderboard failed")
        diagnostics.append(f"{live_endpoint}: {exc}")

    # Более надёжный сезонный endpoint.
    for season_id in candidate_season_ids(4):
        endpoint = (
            f"/locations/global/pathoflegend/"
            f"{season_id}/rankings/players"
        )

        try:
            data = await cr_get(endpoint, {"limit": 100})
            items = parse_ranking_items(data)

            logging.info(
                "Season Ranked leaderboard %s: players=%s, keys=%s",
                season_id,
                len(items),
                list(data.keys()) if isinstance(data, dict) else "-",
            )

            if items:
                leaderboard_season_id = season_id

                current_id = candidate_season_ids(1)[0]
                if season_id == current_id:
                    leaderboard_source_label = (
                        f"Ranked, сезон {season_id}"
                    )
                else:
                    leaderboard_source_label = (
                        f"последний доступный Ranked, сезон {season_id}"
                    )

                return items[:100]

            diagnostics.append(f"{endpoint}: items=0")

        except Exception as exc:
            logging.warning(
                "Season leaderboard %s unavailable: %s",
                season_id,
                exc,
            )
            diagnostics.append(f"{endpoint}: {exc}")

    # Последний fallback: старый Trophy Road leaderboard.
    trophy_endpoint = "/locations/global/rankings/players"

    try:
        data = await cr_get(trophy_endpoint, {"limit": 100})
        items = parse_ranking_items(data)

        logging.info(
            "Trophy Road leaderboard fallback: players=%s",
            len(items),
        )

        if items:
            leaderboard_season_id = None
            leaderboard_source_label = "Trophy Road (fallback)"
            return items[:100]

        diagnostics.append(f"{trophy_endpoint}: items=0")

    except Exception as exc:
        logging.exception("Trophy Road leaderboard failed")
        diagnostics.append(f"{trophy_endpoint}: {exc}")

    raise RuntimeError(
        "Не удалось получить мировой топ игроков. "
        + " | ".join(diagnostics)
    )


async def fetch_player_battlelog(player_tag: str) -> list[dict]:
    encoded_tag = quote(player_tag.lstrip("#"), safe="")
    return await cr_get(f"/players/%23{encoded_tag}/battlelog")


def extract_decks_from_battlelog(
    battlelog: list[dict],
    player_tag: str,
) -> list[dict]:
    """
    Возвращает недавние 1v1 Ranked/PvP колоды игрока вместе с результатом.
    """
    player_tag = player_tag.lstrip("#").upper()
    extracted: list[dict] = []

    for battle in battlelog or []:
        battle_type = str(battle.get("type", "")).lower()

        if battle_type not in {"pathoflegend", "pvp"}:
            continue

        team = battle.get("team") or []
        opponent = battle.get("opponent") or []

        if len(team) != 1 or len(opponent) != 1:
            continue

        chosen = team[0]
        chosen_tag = str(chosen.get("tag", "")).lstrip("#").upper()
        if chosen_tag != player_tag:
            continue

        cards = card_names(chosen.get("cards"))
        if len(cards) != 8:
            continue

        my_crowns = int(chosen.get("crowns", 0) or 0)
        enemy_crowns = int(opponent[0].get("crowns", 0) or 0)

        if my_crowns > enemy_crowns:
            result = "win"
        elif my_crowns < enemy_crowns:
            result = "loss"
        else:
            result = "draw"

        extracted.append({
            "cards": cards,
            "result": result,
            "battle_type": battle_type,
            "battle_time": battle.get("battleTime"),
        })

    return extracted


async def refresh_live_data() -> tuple[int, int]:
    global top_players_cache, meta_decks_cache, cache_updated_at

    if not api_is_ready():
        return 0, 0

    async with refresh_lock:
        players = await fetch_top_100_players()

        deck_stats: dict[tuple[str, ...], dict] = {}
        semaphore = asyncio.Semaphore(8)

        async def load_player(index: int, player: dict) -> dict:
            item = dict(player)
            tag = str(player.get("tag", ""))
            item["recent_deck"] = []

            if not tag:
                return item

            try:
                async with semaphore:
                    battles = await fetch_player_battlelog(tag)

                rows = extract_decks_from_battlelog(battles, tag)

                if rows:
                    item["recent_deck"] = rows[0]["cards"]

                if index < 30:
                    for row in rows:
                        signature = deck_signature(row["cards"])
                        stat = deck_stats.setdefault(
                            signature,
                            {"games": 0, "wins": 0, "losses": 0, "draws": 0},
                        )
                        stat["games"] += 1
                        if row["result"] == "win":
                            stat["wins"] += 1
                        elif row["result"] == "loss":
                            stat["losses"] += 1
                        else:
                            stat["draws"] += 1

            except Exception:
                logging.exception("Не удалось получить battlelog игрока %s", tag)

            return item

        enriched_players = await asyncio.gather(
            *(load_player(i, player) for i, player in enumerate(players[:100]))
        )

        meta = []
        for signature, stat in deck_stats.items():
            decisive = stat["wins"] + stat["losses"]
            win_rate = round(stat["wins"] / decisive * 100, 1) if decisive else None

            meta.append({
                "cards": list(signature),
                "games": stat["games"],
                "wins": stat["wins"],
                "losses": stat["losses"],
                "draws": stat["draws"],
                "win_rate": win_rate,
                "source": "Supercell API / Top 30 Ranked players",
            })

        meta.sort(
            key=lambda x: (
                x["games"],
                x["win_rate"] if x["win_rate"] is not None else -1,
            ),
            reverse=True,
        )

        top_players_cache = enriched_players
        meta_decks_cache = meta[:20]
        cache_updated_at = datetime.now(timezone.utc)

        return len(top_players_cache), len(meta_decks_cache)


async def data_refresh_loop():
    # При старте пытаемся заполнить данные. Ошибка API не должна уронить веб-сервис.
    while True:
        try:
            if api_is_ready():
                top_count, meta_count = await refresh_live_data()
                logging.info(
                    "Live data refreshed: top=%s, meta_decks=%s",
                    top_count,
                    meta_count,
                )
            else:
                logging.warning(
                    "CLASH_ROYALE_API_KEY не задан — используются тестовые данные."
                )
        except Exception:
            logging.exception("Live data refresh failed")

        # Обновление раз в 30 минут.
        await asyncio.sleep(1800)


async def ensure_live_data() -> bool:
    if not api_is_ready():
        logging.warning("CLASH_ROYALE_API_KEY не задан.")
        return False

    if top_players_cache:
        return True

    try:
        await refresh_live_data()
        return bool(top_players_cache)
    except Exception:
        logging.exception("Initial live data refresh failed")
        return False


# =========================
# BOT (dispatcher with storage)
# =========================

dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🏠 <b>Clash Decks</b>\n\n"
        "Актуальные колоды Clash Royale.\n\n"
        "Выбери нужный раздел:",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "home")
async def home(callback: CallbackQuery):
    # Используем edit_text — если сообщение было удалено/неизменено, это не критично
    await callback.message.edit_text("🏠 <b>Clash Decks</b>\n\n" "Выбери раздел:", reply_markup=main_menu())
    await callback.answer()


# =========================
# МЕТА
# =========================

@dp.callback_query(F.data == "meta")
async def meta(callback: CallbackQuery):
    await callback.answer("Обновляю данные…")
    await ensure_live_data()

    if not meta_decks_cache:
        sorted_decks = sorted(decks, key=lambda x: x.win_rate, reverse=True)
        msg = (
            "🔥 <b>МЕТА</b>\n\n"
            "Live-данные пока недоступны, поэтому показываю тестовые колоды.\n\n"
        )
        for i, deck in enumerate(sorted_decks[:5], 1):
            msg += (
                f"{i}. <b>{deck.name}</b>\n"
                f"📈 {deck.win_rate}% WR\n"
                f"⚔️ {deck.games:,} игр\n\n"
            )
    else:
        msg = (
            "🔥 <b>АКТУАЛЬНАЯ МЕТА</b>\n\n"
            "Выборка: последние 1v1 бои топ-30 игроков текущего Ranked.\n"
            f"🕒 Обновлено: {format_last_updated()}\n\n"
        )

        for i, item in enumerate(meta_decks_cache[:10], 1):
            wr = f"{item['win_rate']}% WR" if item["win_rate"] is not None else "WR —"
            msg += (
                f"<b>{i}.</b> {' • '.join(item['cards'])}\n"
                f"📈 {wr}   ⚔️ {item['games']} матчей\n\n"
            )

    await callback.message.edit_text(msg, reply_markup=back_button())


# =========================
# ТОП
# =========================

@dp.callback_query(F.data == "top")
async def top(callback: CallbackQuery):
    sorted_decks = sorted(decks, key=lambda x: x.games, reverse=True)

    text = "🏆 <b>ТОП КОЛОД</b>\n\n"

    for deck in sorted_decks[:5]:
        text += f"🃏 <b>{deck.name}</b>\n" f"⚔️ {deck.games:,} игр\n" f"📈 {deck.win_rate}% WR\n\n"

    await callback.message.edit_text(text, reply_markup=back_button())
    await callback.answer()



# =========================
# ТОП 100 ИГРОКОВ
# =========================

def top100_keyboard(offset: int = 0):
    buttons = []

    if offset > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"top100:{max(0, offset - 10)}",
            )
        )

    if offset + 10 < min(100, len(top_players_cache)):
        buttons.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=f"top100:{offset + 10}",
            )
        )

    rows = [buttons] if buttons else []
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_player(position: int, player: dict) -> str:
    name = player.get("name", "Unknown")
    tag = player.get("tag", "")
    elo = player.get("eloRating")
    trophies = player.get("trophies")
    deck = player.get("recent_deck") or []

    if elo is not None:
        rating = f"{elo} рейтинга"
    elif trophies is not None:
        rating = f"{trophies} 🏆"
    else:
        rating = "рейтинг —"

    result = f"<b>{position}. {name}</b> — {rating}\n"
    if tag:
        result += f"<code>{tag}</code>\n"

    if deck:
        result += "🃏 " + " • ".join(deck)
    else:
        result += "🃏 Нет подходящей колоды в последних боях"

    return result


@dp.callback_query(F.data == "top100")
async def top100(callback: CallbackQuery):
    await callback.answer("Загружаю топ игроков…")
    await ensure_live_data()

    if not top_players_cache:
        await callback.message.edit_text(
            "👑 <b>ТОП 100 ИГРОКОВ</b>\n\n"
            "API доступен, но текущий leaderboard пока пуст.\n"
            "Бот автоматически пробует последние сезонные рейтинги.",
            reply_markup=back_button(),
        )
        return

    await show_top100_page(callback, 0)


@dp.callback_query(F.data.startswith("top100:"))
async def top100_page(callback: CallbackQuery):
    try:
        offset = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        offset = 0

    await callback.answer()
    await show_top100_page(callback, max(0, min(90, offset)))


async def show_top100_page(callback: CallbackQuery, offset: int):
    items = top_players_cache[offset:offset + 10]

    msg = (
        "👑 <b>ТОП 100 ИГРОКОВ</b>\n"
        f"Источник: {leaderboard_source_label}\n"
        f"🕒 Обновлено: {format_last_updated()}\n\n"
    )

    for i, player in enumerate(items, offset + 1):
        msg += format_player(i, player) + "\n\n"

    msg += f"Страница {offset // 10 + 1}/10"

    await callback.message.edit_text(
        msg,
        reply_markup=top100_keyboard(offset),
    )



# =========================
# ИСПЫТАНИЯ
# =========================

@dp.callback_query(F.data == "challenges")
async def challenges(callback: CallbackQuery):
    await callback.message.edit_text(
        "🎯 <b>ИСПЫТАНИЯ</b>\n\n"
        "Пока здесь нет активных испытаний.\n\n"
        "В следующей версии сюда подключим "
        "актуальные испытания и лучшие колоды "
        "для каждого из них.",
        reply_markup=back_button(),
    )
    await callback.answer()


# =========================
# ПОИСК
# =========================

@dp.callback_query(F.data == "search")
async def search_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Search.searching)
    await callback.message.edit_text(
        "🔍 Напиши название карты или архетип.\n\n"
        "Например:\n"
        "<code>Hog Rider</code>\n"
        "<code>Graveyard</code>",
        reply_markup=back_button(),
    )
    await callback.answer()


@dp.message(Search.searching, F.text)
async def search_message(message: Message, state: FSMContext):
    # message.text гарантировано не None из-за F.text фильтра
    query = message.text.strip().lower()

    results: list[Deck] = []

    for deck in decks:
        if query in deck.name.lower() or any(query in card.lower() for card in deck.cards):
            results.append(deck)

    if not results:
        await message.answer("😔 Ничего не найдено.", reply_markup=main_menu())
        await state.clear()
        return

    user_id = message.from_user.id
    user_favorites = favorites.get(user_id, set())

    for deck in results[:10]:
        starred = deck.id in user_favorites
        await message.answer(deck_text(deck), reply_markup=deck_keyboard(deck.id, starred=starred))

    await state.clear()


# =========================
# КОЛОДА
# =========================

@dp.callback_query(F.data.startswith("deck:"))
async def show_deck(callback: CallbackQuery):
    try:
        deck_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор колоды", show_alert=True)
        return

    deck = get_deck(deck_id)

    if not deck:
        await callback.answer("Колода не найдена", show_alert=True)
        return

    user_id = callback.from_user.id
    starred = deck_id in favorites.get(user_id, set())

    await callback.message.edit_text(deck_text(deck), reply_markup=deck_keyboard(deck.id, starred=starred))
    await callback.answer()


# =========================
# ИЗБРАННОЕ
# =========================

@dp.callback_query(F.data.startswith("fav:"))
async def favorite(callback: CallbackQuery):
    try:
        deck_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор", show_alert=True)
        return

    user_id = callback.from_user.id

    if user_id not in favorites:
        favorites[user_id] = set()

    if deck_id in favorites[user_id]:
        favorites[user_id].remove(deck_id)
        await callback.answer("Удалено из избранного")
    else:
        favorites[user_id].add(deck_id)
        await callback.answer("⭐ Добавлено в избранное")


@dp.callback_query(F.data == "favorites")
async def show_favorites(callback: CallbackQuery):
    user_id = callback.from_user.id

    user_favorites = favorites.get(user_id, set())

    if not user_favorites:
        await callback.message.edit_text("⭐ <b>ИЗБРАННОЕ</b>\n\n" "Здесь пока ничего нет.", reply_markup=back_button())
        await callback.answer()
        return

    text = "⭐ <b>ИЗБРАННОЕ</b>\n\n"

    for deck_id in user_favorites:
        deck = get_deck(deck_id)
        if deck:
            text += f"🃏 {deck.name}\n" f"📈 {deck.win_rate}% WR\n\n"

    await callback.message.edit_text(text, reply_markup=back_button())
    await callback.answer()


# =========================
# ДОБАВЛЕНИЕ КОЛОДЫ
# =========================

@dp.callback_query(F.data == "add_deck")
async def add_deck_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Эта функция доступна только администратору.", show_alert=True)
        return

    await state.set_state(AddDeck.name)

    await callback.message.edit_text("➕ <b>Добавление колоды</b>\n\n" "Шаг 1/5\n" "Напиши название колоды:")
    await callback.answer()


@dp.message(AddDeck.name, F.text)
async def add_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AddDeck.cards)
    await message.answer(
        "Шаг 2/5\n\n"
        "Напиши 8 карт через запятую.\n\n"
        "Например:\n"
        "<code>Hog Rider, Musketeer, Cannon, "
        "Fireball, The Log, Ice Spirit, Skeletons, Ice Golem</code>"
    )


@dp.message(AddDeck.cards, F.text)
async def add_cards(message: Message, state: FSMContext):
    cards = [card.strip() for card in message.text.split(",") if card.strip()]

    if len(cards) != 8:
        await message.answer("❌ Нужно указать ровно 8 карт.")
        return

    await state.update_data(cards=cards)
    await state.set_state(AddDeck.mode)
    await message.answer("Шаг 3/5\n\n" "Укажи режим.\n" "Например: ladder")


@dp.message(AddDeck.mode, F.text)
async def add_mode(message: Message, state: FSMContext):
    await state.update_data(mode=message.text.strip())
    await state.set_state(AddDeck.win_rate)
    await message.answer("Шаг 4/5\n\n" "Укажи процент побед.\n" "Например: <code>55.4</code>")


@dp.message(AddDeck.win_rate, F.text)
async def add_win_rate(message: Message, state: FSMContext):
    try:
        win_rate = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число, например 55.4")
        return

    if not (0 <= win_rate <= 100):
        await message.answer("❌ Процент побед должен быть в диапазоне 0–100.")
        return

    await state.update_data(win_rate=win_rate)
    await state.set_state(AddDeck.games)
    await message.answer("Шаг 5/5\n\n" "Сколько игр учтено?\n" "Например: <code>15000</code>")


@dp.message(AddDeck.games, F.text)
async def add_games(message: Message, state: FSMContext):
    global next_deck_id

    try:
        games = int(message.text.replace(",", ""))
    except ValueError:
        await message.answer("❌ Введи целое число.")
        return

    data = await state.get_data()

    deck = Deck(
        id=next_deck_id,
        name=data["name"],
        cards=data["cards"],
        mode=data["mode"],
        win_rate=data["win_rate"],
        games=games,
    )

    decks.append(deck)
    next_deck_id += 1

    await state.clear()

    await message.answer("✅ <b>Колода добавлена!</b>\n\n" + deck_text(deck), reply_markup=main_menu())


# =========================
# ADMIN
# =========================

@dp.message(Command("admin"))
async def admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    await message.answer("👑 <b>ADMIN</b>\n\n" "Используй кнопку «➕ Добавить» " "в главном меню.")


# =========================
# ЗАПУСК ДЛЯ RENDER
# =========================

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL")

if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL не найден.")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET не найден в Environment Variables.")

# Официальный API Clash Royale.
# Ключ хранится только в Render Environment Variables.
CR_API_KEY = os.getenv("CLASH_ROYALE_API_KEY", "")
CR_API_BASE = os.getenv("CLASH_ROYALE_API_BASE", "https://proxy.royaleapi.dev/v1").rstrip("/")

# Кэш данных. Он обновляется в фоне и не мешает Telegram-обработчикам.
top_players_cache: list[dict] = []
meta_decks_cache: list[dict] = []
cache_updated_at: datetime | None = None

# Какой leaderboard реально удалось получить.
leaderboard_season_id: str | None = None
leaderboard_source_label: str = "не определён"

refresh_lock = asyncio.Lock()

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

app = FastAPI(title="Clash Decks")


@app.get("/")
async def root():
    return {"status": "ok", "service": "Clash Decks"}


@app.get("/health")
async def health():
    return {"status": "ok"}


refresh_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    global refresh_task

    logging.basicConfig(level=logging.INFO)

    webhook_url = f"{PUBLIC_URL.rstrip('/')}/telegram/webhook"
    await bot.set_webhook(
        webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    logging.info("Telegram webhook set: %s", webhook_url)

    refresh_task = asyncio.create_task(data_refresh_loop())


@app.on_event("shutdown")
async def on_shutdown():
    global refresh_task

    if refresh_task:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass

    # ВАЖНО: не удаляем webhook при shutdown.
    # Во время нового деплоя Render сначала запускает новый instance,
    # а затем останавливает старый. Если старый instance вызовет
    # delete_webhook(), он удалит webhook, который только что
    # установил новый instance.
    await bot.session.close()



@app.get("/api-status")
async def api_status():
    return {
        "status": "ok",
        "api_key_configured": api_is_ready(),
        "api_base": CR_API_BASE,
        "top_players": len(top_players_cache),
        "meta_decks": len(meta_decks_cache),
        "updated_at": cache_updated_at.isoformat() if cache_updated_at else None,
        "leaderboard_source": leaderboard_source_label,
        "leaderboard_season_id": leaderboard_season_id,
    }


@app.get("/webhook-status")
async def webhook_status():
    info = await bot.get_webhook_info()
    return {
        "url": info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date.isoformat() if info.last_error_date else None,
        "last_error_message": info.last_error_message,
        "max_connections": info.max_connections,
    }


@app.get("/api-test")
async def api_test():
    """
    Безопасная диагностика. API-ключ никогда не выводится.
    """
    if not api_is_ready():
        return {
            "ok": False,
            "error": "CLASH_ROYALE_API_KEY не настроен",
        }

    endpoints = [
        "/locations/global/pathoflegend/players",
    ]

    for season_id in candidate_season_ids(4):
        endpoints.append(
            f"/locations/global/pathoflegend/"
            f"{season_id}/rankings/players"
        )

    endpoints.append("/locations/global/rankings/players")

    results = []

    for endpoint in endpoints:
        try:
            data = await cr_get(endpoint, {"limit": 3})
            items = parse_ranking_items(data)

            results.append({
                "endpoint": endpoint,
                "ok": True,
                "response_type": type(data).__name__,
                "keys": list(data.keys()) if isinstance(data, dict) else None,
                "items_count": len(items),
                "sample": [
                    {
                        "name": p.get("name"),
                        "tag": p.get("tag"),
                        "rank": p.get("rank"),
                        "eloRating": p.get("eloRating"),
                    }
                    for p in items[:2]
                    if isinstance(p, dict)
                ],
            })

        except Exception as exc:
            results.append({
                "endpoint": endpoint,
                "ok": False,
                "error": str(exc)[:500],
            })

    return {
        "ok": True,
        "tested_seasons": candidate_season_ids(4),
        "results": results,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    data = await request.json()

    try:
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception:
        logging.exception("Failed to process Telegram update")
        raise HTTPException(status_code=500, detail="Update processing failed")

    return {"ok": True}
