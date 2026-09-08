import asyncio
import logging
import os
import time
import html
from pathlib import Path
from io import BytesIO
from collections import Counter
from datetime import datetime, timezone
from calendar import monthrange
from urllib.parse import quote
from dataclasses import dataclass
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update, BufferedInputFile
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
from aiogram.exceptions import TelegramBadRequest
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
LOCAL_CARD_ASSETS = {
    # normalize_card_name() сохраняет пробелы, поэтому нужен ключ с пробелом.
    "minion giant": ASSETS_DIR / "cards" / "minion_giant.png",
    # запасной alias на случай другого именования в источнике.
    "giant minion": ASSETS_DIR / "cards" / "minion_giant.png",
}
LOCAL_ELIXIR_DROP_PATH = ASSETS_DIR / "ui" / "elixir_drop.png"


def read_local_binary(path: Path) -> bytes | None:
    try:
        if path.exists() and path.is_file():
            return path.read_bytes()
    except Exception:
        logging.exception("Failed to read local asset: %s", path)
    return None


# =========================
# ДАННЫЕ
# =========================

@dataclass
class Deck:
    id: int
    name: str
    cards: list
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


def safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_card_name(name: str) -> str:
    return str(name or "").strip().lower()


def normalize_card(raw) -> dict:
    """
    Единственное внутреннее представление карты.

    API battlelog уже содержит часть полей карты. Для ручных колод
    карта может быть просто строкой — каталог /cards дополнит её позже.
    """
    if isinstance(raw, str):
        return {
            "id": None,
            "name": raw,
            "level": 0,
            "maxLevel": 0,
            "starLevel": 0,
            "evolutionLevel": 0,
            "maxEvolutionLevel": 0,
            "elixirCost": None,
            "rarity": "",
            "iconUrls": {},
        }

    if not isinstance(raw, dict):
        return normalize_card(str(raw))

    icon_urls = raw.get("iconUrls") or {}
    if not isinstance(icon_urls, dict):
        icon_urls = {}

    return {
        "id": raw.get("id"),
        "name": str(raw.get("name", "")),
        "level": safe_int(raw.get("level")),
        "maxLevel": safe_int(raw.get("maxLevel")),
        "starLevel": safe_int(raw.get("starLevel")),
        "evolutionLevel": safe_int(raw.get("evolutionLevel")),
        "maxEvolutionLevel": safe_int(raw.get("maxEvolutionLevel")),
        "elixirCost": raw.get("elixirCost"),
        "rarity": str(raw.get("rarity", "")),
        "iconUrls": icon_urls,
    }


def card_names(raw_cards) -> list[str]:
    return [
        card["name"]
        for card in (normalize_card(item) for item in (raw_cards or []))
        if card["name"]
    ]


def card_special_kind(raw_card) -> str:
    """
    API 2026 использует evolutionLevel/maxEvolutionLevel для специальных
    форм. Для отображения:
      0 -> base
      1 -> Evolution
      2+ -> Hero
    Если API изменит поля, обычная иконка всё равно останется рабочей.
    """
    card = normalize_card(raw_card)
    level = card["evolutionLevel"]
    icons = card.get("iconUrls") or {}

    if level >= 2 and icons.get("heroMedium"):
        return "hero"
    if level >= 2:
        return "hero"
    if level >= 1:
        return "evo"
    return "base"


def deck_signature(cards: list) -> tuple[str, ...]:
    """
    Группируем одинаковые колоды, но различаем Base / Evo / Hero.
    """
    tokens = []
    for raw in cards or []:
        card = normalize_card(raw)
        name = card["name"]
        if name:
            tokens.append(f"{name.lower()}::{card_special_kind(card)}")
    return tuple(sorted(tokens))


def merge_catalog_card(raw_card) -> dict:
    card = normalize_card(raw_card)
    catalog = cards_catalog_cache.get(normalize_card_name(card["name"]))

    if not catalog:
        return card

    merged = dict(catalog)
    merged.update({
        key: value
        for key, value in card.items()
        if value not in (None, "", {}, 0)
    })

    # iconUrls от battlelog обычно наиболее точные для текущей формы,
    # но каталог помогает ручным колодам.
    icons = {}
    if isinstance(catalog.get("iconUrls"), dict):
        icons.update(catalog["iconUrls"])
    if isinstance(card.get("iconUrls"), dict):
        icons.update(card["iconUrls"])
    merged["iconUrls"] = icons

    # Нули в battlelog важны: не подменяем base-карту evo-уровнем каталога.
    merged["evolutionLevel"] = card["evolutionLevel"]
    merged["starLevel"] = card["starLevel"]
    if card["level"]:
        merged["level"] = card["level"]

    return normalize_card(merged)


def card_icon_candidates(raw_card) -> list[str]:
    """
    Возвращает несколько вариантов официальной иконки карты.

    Некоторые evolution/hero URL в API могут временно отдавать 404.
    Поэтому пробуем специальную иконку первой, затем обычную medium,
    а также варианты из общего каталога /cards.
    """
    raw = normalize_card(raw_card)
    catalog = cards_catalog_cache.get(normalize_card_name(raw["name"])) or {}
    kind = card_special_kind(raw)

    if kind == "hero":
        preferred_keys = ("heroMedium", "evolutionMedium", "medium")
    elif kind == "evo":
        preferred_keys = ("evolutionMedium", "medium", "heroMedium")
    else:
        preferred_keys = ("medium", "evolutionMedium", "heroMedium")

    sources = []

    raw_icons = raw.get("iconUrls") or {}
    if isinstance(raw_icons, dict):
        sources.append(raw_icons)

    catalog_icons = catalog.get("iconUrls") or {}
    if isinstance(catalog_icons, dict):
        sources.append(catalog_icons)

    candidates: list[str] = []

    for key in preferred_keys:
        for icons in sources:
            url = icons.get(key)
            if isinstance(url, str) and url and url not in candidates:
                candidates.append(url)

    # Последний fallback: любой URL из iconUrls, который ещё не пробовали.
    for icons in sources:
        for url in icons.values():
            if isinstance(url, str) and url and url not in candidates:
                candidates.append(url)

    return candidates


def card_icon_url(raw_card) -> str | None:
    candidates = card_icon_candidates(raw_card)
    return candidates[0] if candidates else None


def get_cached_local_binary(path: Path) -> bytes | None:
    key = str(path)
    cached = local_binary_cache.get(key)
    if cached:
        return cached
    data = read_local_binary(path)
    if data:
        local_binary_cache[key] = data
    return data


def card_elixir(raw_card):
    card = merge_catalog_card(raw_card)
    value = card.get("elixirCost")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def average_elixir(cards: list) -> float | None:
    values = [card_elixir(card) for card in cards or []]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 1)


def build_deck_link(cards: list, locale: str = "en") -> str | None:
    """
    Официальный deck-share link Clash Royale.
    Открывает экран импорта колоды в самой игре.
    """
    ids = []
    for raw in cards or []:
        card = merge_catalog_card(raw)
        card_id = card.get("id")
        if card_id is None:
            continue
        try:
            ids.append(str(int(card_id)))
        except (TypeError, ValueError):
            continue

    if len(ids) != 8:
        return None

    return f"https://link.clashroyale.com/deck/{locale}?deck=" + ";".join(ids)


SEASON_ARENA_THEMES = [
    ("Royal Arena", ((11, 86, 176), (22, 143, 224), (162, 217, 255))),
    ("Frozen Peak", ((44, 126, 196), (110, 194, 245), (225, 247, 255))),
    ("Jungle Arena", ((26, 112, 84), (50, 157, 104), (171, 231, 182))),
    ("Electro Valley", ((65, 63, 167), (104, 97, 219), (211, 208, 255))),
    ("Spooky Town", ((73, 69, 115), (121, 116, 171), (231, 223, 255))),
    ("Rascal's Hideout", ((117, 74, 66), (184, 117, 99), (255, 224, 191))),
    ("Serenity Peak", ((55, 111, 143), (83, 158, 198), (207, 240, 255))),
    ("Executioner's Kitchen", ((112, 71, 54), (179, 99, 71), (255, 212, 179))),
    ("Builder's Workshop", ((54, 88, 133), (88, 125, 178), (213, 228, 255))),
    ("Arena of Valor", ((83, 57, 139), (132, 82, 207), (235, 214, 255))),
    ("Clash Fest Arena", ((130, 52, 128), (217, 95, 180), (255, 218, 236))),
    ("Legend Arena", ((70, 92, 140), (112, 147, 205), (223, 236, 255))),
]


MODE_THEME_OVERRIDES = {
    "ranked": ((10, 86, 175), (37, 125, 214), (212, 236, 255)),
    "path of legend": ((31, 80, 170), (103, 73, 206), (236, 224, 255)),
    "ladder": ((24, 105, 173), (67, 150, 221), (220, 243, 255)),
    "meta": ((28, 88, 162), (93, 64, 182), (231, 222, 255)),
    "challenge": ((183, 94, 27), (230, 140, 45), (255, 235, 191)),
    "classic": ((69, 119, 179), (61, 175, 215), (228, 248, 255)),
    "grand": ((148, 79, 24), (219, 120, 41), (255, 230, 189)),
    "trial": ((120, 58, 150), (192, 83, 198), (255, 223, 244)),
    "2v2": ((39, 120, 113), (54, 176, 156), (215, 255, 244)),
}


def parse_season_month(season_id: str | None) -> int:
    if season_id and "-" in season_id:
        try:
            return int(str(season_id).split("-")[1])
        except (TypeError, ValueError, IndexError):
            pass
    return datetime.now(timezone.utc).month


def infer_visual_theme(mode_label: str = "", source_label: str = "", season_id: str | None = None) -> dict:
    mode_text = f"{mode_label} {source_label}".lower()
    month = parse_season_month(season_id)
    arena_name, arena_colors = SEASON_ARENA_THEMES[(month - 1) % len(SEASON_ARENA_THEMES)]

    colors = arena_colors
    selected_mode = "season"

    for key, override in MODE_THEME_OVERRIDES.items():
        if key in mode_text:
            colors = override
            selected_mode = key
            break

    return {
        "arena_name": arena_name,
        "mode_key": selected_mode,
        "top": colors[0],
        "mid": colors[1],
        "accent": colors[2],
    }


def deck_mode_badge(mode_label: str = "", source_label: str = "") -> str:
    text = (mode_label or source_label or "").strip()
    return text if text else "Battle Deck"


def escape(value) -> str:
    return html.escape(str(value or ""))


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

        cards = [
            normalize_card(card)
            for card in (chosen.get("cards") or [])
            if isinstance(card, dict) and card.get("name")
        ]
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
                            {
                                "games": 0,
                                "wins": 0,
                                "losses": 0,
                                "draws": 0,
                                "cards": row["cards"],
                            },
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
                "cards": stat["cards"],
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
# ИКОНКИ КАРТ + RENDER КОЛОДЫ
# =========================

async def ensure_cards_catalog() -> bool:
    global cards_catalog_cache

    if cards_catalog_cache:
        return True

    try:
        data = await cr_get("/cards")
        items = data.get("items", []) if isinstance(data, dict) else []

        catalog = {}
        for raw in items:
            card = normalize_card(raw)
            if card["name"]:
                catalog[normalize_card_name(card["name"])] = card

        cards_catalog_cache = catalog
        logging.info("Cards catalog loaded: %s", len(cards_catalog_cache))
        return bool(cards_catalog_cache)

    except Exception:
        logging.exception("Cards catalog load failed")
        return False


async def get_card_image_bytes(raw_card) -> bytes | None:
    await ensure_cards_catalog()

    card = merge_catalog_card(raw_card)
    card_name = card.get("name") or "Unknown card"
    normalized_name = normalize_card_name(card_name)

    # Для вручную добавленных локальных ассетов используем их сразу.
    # Это гарантирует, что Minion Giant отображается даже если Supercell CDN
    # отдаёт устаревший/битый URL.
    local_path = LOCAL_CARD_ASSETS.get(normalized_name)
    if local_path:
        local_data = get_cached_local_binary(local_path)
        if local_data:
            if card_name not in local_asset_logged:
                logging.info("Using bundled local card asset for %s", card_name)
                local_asset_logged.add(card_name)
            return local_data

    candidates = card_icon_candidates(raw_card)

    if not candidates:
        local_path = LOCAL_CARD_ASSETS.get(normalized_name)
        if local_path:
            local_data = get_cached_local_binary(local_path)
            if local_data:
                if card_name not in local_asset_logged:
                    logging.info("Using bundled local card asset for %s", card_name)
                    local_asset_logged.add(card_name)
                return local_data

        if card_name not in card_image_placeholder_logged:
            logging.info(
                "No official card icon URL available for %s; placeholder will be used",
                card_name,
            )
            card_image_placeholder_logged.add(card_name)
        return None

    now = time.time()

    # First use positive cache.
    for url in candidates:
        cached = card_image_cache.get(url)
        if cached:
            return cached

    async with image_cache_lock:
        # Another coroutine may have filled the cache while we waited.
        for url in candidates:
            cached = card_image_cache.get(url)
            if cached:
                return cached

        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            for index, url in enumerate(candidates, 1):
                retry_after = card_image_bad_urls.get(url)

                if retry_after is not None:
                    if retry_after > now:
                        continue
                    card_image_bad_urls.pop(url, None)

                try:
                    response = await client.get(url)

                    if response.status_code == 404:
                        card_image_bad_urls[url] = time.time() + CARD_IMAGE_BAD_URL_TTL
                        logging.info(
                            "Official card asset temporarily unavailable for %s "
                            "(candidate %s/%s); fallback will be used",
                            card_name,
                            index,
                            len(candidates),
                        )
                        continue

                    response.raise_for_status()

                    content_type = response.headers.get("content-type", "")
                    if "image" not in content_type.lower():
                        card_image_bad_urls[url] = time.time() + CARD_IMAGE_BAD_URL_TTL
                        logging.info(
                            "Official card asset returned non-image content for %s; "
                            "trying fallback",
                            card_name,
                        )
                        continue

                    data = response.content
                    if not data:
                        card_image_bad_urls[url] = time.time() + CARD_IMAGE_BAD_URL_TTL
                        logging.info(
                            "Official card asset returned an empty body for %s; "
                            "trying fallback",
                            card_name,
                        )
                        continue

                    card_image_cache[url] = data
                    card_image_placeholder_logged.discard(card_name)
                    local_asset_logged.discard(card_name)

                    if index > 1:
                        logging.info(
                            "Card icon fallback succeeded for %s "
                            "(candidate %s/%s)",
                            card_name,
                            index,
                            len(candidates),
                        )

                    return data

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code

                    if 400 <= status < 500:
                        card_image_bad_urls[url] = time.time() + CARD_IMAGE_BAD_URL_TTL

                    logging.info(
                        "Card asset HTTP %s for %s; trying fallback",
                        status,
                        card_name,
                    )

                except httpx.RequestError as exc:
                    logging.info(
                        "Temporary card asset network error for %s: %s",
                        card_name,
                        type(exc).__name__,
                    )

                except Exception as exc:
                    logging.info(
                        "Card asset fallback skipped for %s: %s",
                        card_name,
                        type(exc).__name__,
                    )

    # Local bundled fallback for cards you manually provided.
    local_path = LOCAL_CARD_ASSETS.get(normalized_name)
    if local_path:
        local_data = get_cached_local_binary(local_path)
        if local_data:
            if card_name not in local_asset_logged:
                logging.info("Using bundled local card asset for %s", card_name)
                local_asset_logged.add(card_name)
            return local_data

    if card_name not in card_image_placeholder_logged:
        logging.info(
            "No currently reachable official asset for %s; "
            "using placeholder and retrying after cooldown",
            card_name,
        )
        card_image_placeholder_logged.add(card_name)

    return None


def get_font(size: int, bold: bool = False):
    names = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "DejaVuSans-Bold.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "DejaVuSans.ttf"]
    )

    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue

    return ImageFont.load_default()


def fit_text(draw: ImageDraw.ImageDraw, text: str, max_width: int, size=24, bold=False):
    text = str(text or "")
    font = get_font(size, bold=bold)

    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text, font

    trimmed = text
    while len(trimmed) > 3:
        trimmed = trimmed[:-1]
        candidate = trimmed.rstrip() + "…"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            return candidate, font

    return "…", font


def rounded_badge(draw, xy, label, fill, text_fill=(255, 255, 255), font=None):
    font = font or get_font(18, bold=True)
    draw.rounded_rectangle(xy, radius=12, fill=fill)
    x1, y1, x2, y2 = xy
    box = draw.textbbox((0, 0), label, font=font)
    tw = box[2] - box[0]
    th = box[3] - box[1]
    draw.text(
        (x1 + (x2 - x1 - tw) / 2, y1 + (y2 - y1 - th) / 2 - 2),
        label,
        font=font,
        fill=text_fill,
    )


async def build_deck_image(
    cards: list,
    title: str,
    subtitle: str = "",
    mode_label: str = "",
    source_label: str = "",
    season_id: str | None = None,
    tower_troop: str = "",
) -> bytes:
    """
    V10 renderer:
    - колода теперь в более тёмной, почти чёрной стилистике;
    - визуальная подача ближе к присланному примеру с 8 картами на black background;
    - карточки крупнее и чище, без лишних контейнеров;
    - усилены EVO / HERO / CHAMP плашки;
    - фон всё ещё сезонный, но теперь встроен как тёмные акценты, а не как яркий фон.
    """
    await ensure_cards_catalog()

    normalized = [merge_catalog_card(card) for card in (cards or [])][:8]
    theme = infer_visual_theme(
        mode_label=mode_label,
        source_label=source_label,
        season_id=season_id,
    )

    width = 1080
    height = 1460
    bg = Image.new("RGBA", (width, height), (15, 17, 22, 255))
    draw = ImageDraw.Draw(bg)

    def mix(c1, c2, t: float):
        return (
            int(c1[0] * (1 - t) + c2[0] * t),
            int(c1[1] * (1 - t) + c2[1] * t),
            int(c1[2] * (1 - t) + c2[2] * t),
        )

    # Dark black background with seasonal accent glow
    top_glow = mix((18, 18, 22), theme["top"], 0.28)
    mid_glow = mix((18, 18, 22), theme["mid"], 0.22)
    edge_glow = mix((18, 18, 22), theme["accent"], 0.18)

    for y in range(height):
        t = y / max(1, height - 1)
        if t < 0.30:
            col = mix(top_glow, (15, 17, 22), t / 0.30)
        elif t < 0.70:
            col = mix((15, 17, 22), mid_glow, (t - 0.30) / 0.40)
        else:
            col = mix(mid_glow, (12, 13, 18), (t - 0.70) / 0.30)
        draw.line((0, y, width, y), fill=(*col, 255))

    # Seasonal arena glows
    cx, cy = width // 2, 210
    for r, alpha in [(450, 26), (350, 22), (260, 18)]:
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(*edge_glow, alpha), width=2)

    for i in range(9):
        x = 40 + i * 120
        yy = 54 + (i % 2) * 16
        draw.ellipse((x - 26, yy - 26, x + 26, yy + 26), fill=(255, 255, 255, 18))

    # Main deck surface panel
    panel_x1, panel_y1, panel_x2, panel_y2 = 18, 12, width - 18, 1098
    draw.rounded_rectangle((panel_x1 + 6, panel_y1 + 10, panel_x2 + 6, panel_y2 + 12), radius=28, fill=(0, 0, 0, 110))
    draw.rounded_rectangle((panel_x1, panel_y1, panel_x2, panel_y2), radius=28, fill=(17, 19, 26, 228), outline=(88, 104, 136, 255), width=2)
    draw.rounded_rectangle((panel_x1 + 2, panel_y1 + 2, panel_x2 - 2, panel_y1 + 80), radius=28, fill=(255, 255, 255, 10))

    # Header compact, closer to game deck preview
    title_text, title_font = fit_text(draw, title, width - 80, size=42, bold=True)
    draw.text((38, 28), title_text, font=title_font, fill=(255, 255, 255, 255))

    if subtitle.strip():
        sub_text, sub_font = fit_text(draw, subtitle.strip(), width - 220, size=22, bold=False)
        draw.text((38, 72), sub_text, font=sub_font, fill=(212, 221, 238, 255))

    rounded_badge(
        draw,
        (38, 110, 174, 148),
        deck_mode_badge(mode_label, source_label),
        fill=(46, 67, 118, 232),
        text_fill=(255, 255, 255),
        font=get_font(14, bold=True),
    )
    rounded_badge(
        draw,
        (190, 110, 456, 148),
        f"Arena · {theme['arena_name']}",
        fill=(59, 78, 122, 232),
        text_fill=(242, 246, 255),
        font=get_font(14, bold=True),
    )

    # Layout of 8 large cards on dark background
    cols = 4
    card_w = 226
    card_h = 226
    gap_x = 20
    gap_y = 34
    start_x = 54
    start_y = 176

    def style_for_card(card):
        rarity = str(card.get("rarity", "")).lower()
        special = card_special_kind(card)

        if special == "hero":
            return {
                "gem": (244, 185, 56),
                "gem_inner": (255, 227, 133),
                "badge_fill": (213, 152, 44),
                "badge_top": (255, 214, 108),
                "badge_text": "HERO",
                "glow": (255, 204, 84),
            }
        if special == "evo":
            return {
                "gem": (200, 73, 255),
                "gem_inner": (243, 205, 255),
                "badge_fill": (129, 60, 188),
                "badge_top": (210, 141, 255),
                "badge_text": "EVO",
                "glow": (230, 127, 255),
            }
        if "champion" in rarity:
            return {
                "gem": (226, 193, 93),
                "gem_inner": (255, 238, 176),
                "badge_fill": (135, 98, 30),
                "badge_top": (230, 194, 93),
                "badge_text": "CHAMP",
                "glow": (255, 220, 120),
            }
        return {
            "gem": (176, 180, 195),
            "gem_inner": (228, 230, 239),
            "badge_fill": None,
            "badge_top": None,
            "badge_text": "",
            "glow": (130, 136, 160),
        }

    def shadow_box(x1, y1, x2, y2, radius=22, alpha=90):
        draw.rounded_rectangle(
            (x1 + 5, y1 + 7, x2 + 5, y2 + 9),
            radius=radius,
            fill=(0, 0, 0, alpha),
        )

    def draw_diamond(cx, top_y, style):
        outer = [(cx, top_y), (cx + 18, top_y + 18), (cx, top_y + 36), (cx - 18, top_y + 18)]
        inner = [(cx, top_y + 5), (cx + 10, top_y + 18), (cx, top_y + 31), (cx - 10, top_y + 18)]
        draw.polygon(outer, fill=style["gem"], outline=(255, 248, 223, 255))
        draw.polygon(inner, fill=style["gem_inner"])

    def draw_badge(x1, y1, label, fill, top_fill, width_box):
        draw.rounded_rectangle((x1 + 2, y1 + 3, x1 + width_box + 2, y1 + 31), radius=11, fill=(0, 0, 0, 80))
        draw.rounded_rectangle((x1, y1, x1 + width_box, y1 + 28), radius=11, fill=fill)
        draw.rounded_rectangle((x1 + 1, y1 + 1, x1 + width_box - 1, y1 + 12), radius=11, fill=top_fill)
        font = get_font(12, bold=True)
        box = draw.textbbox((0, 0), label, font=font)
        tw = box[2] - box[0]
        th = box[3] - box[1]
        draw.text((x1 + (width_box - tw) / 2, y1 + (28 - th) / 2 - 1), label, font=font, fill=(255, 255, 255, 255))

    def draw_elixir_drop(x, y, value, scale=1.0):
        local_data = get_cached_local_binary(LOCAL_ELIXIR_DROP_PATH)

        if local_data:
            try:
                icon = Image.open(BytesIO(local_data)).convert("RGBA")
                target_w = max(18, int(48 * scale))
                target_h = max(18, int(44 * scale))
                icon = ImageOps.contain(icon, (target_w, target_h))
                bg.paste(icon, (x, y), icon)

                if value != "":
                    label = str(value)
                    font = get_font(max(18, int(26 * scale)), bold=True)
                    box = draw.textbbox((0, 0), label, font=font)
                    tw = box[2] - box[0]
                    th = box[3] - box[1]
                    tx = x + (icon.width - tw) / 2
                    ty = y + icon.height * 0.33 - th / 2
                    draw.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))
                return
            except Exception:
                logging.exception("Failed to render bundled elixir icon")

        # Vector fallback if the local asset is missing.
        w = int(46 * scale)
        body_h = int(40 * scale)
        tip_h = int(20 * scale)
        cx = x + w // 2
        fill = (231, 53, 219, 255)
        outline = (255, 225, 255, 255)
        highlight = (255, 194, 255, 255)

        points = [
            (cx, y),
            (x + w - 6, y + tip_h),
            (x + w - 2, y + tip_h + 12),
            (x + w - 5, y + tip_h + body_h - 2),
            (cx, y + tip_h + body_h + 6),
            (x + 5, y + tip_h + body_h - 2),
            (x + 2, y + tip_h + 12),
            (x + 6, y + tip_h),
        ]
        draw.polygon(points, fill=fill, outline=outline)
        draw.ellipse((x + 3, y + tip_h, x + w - 3, y + tip_h + body_h), fill=fill, outline=outline, width=max(2, int(2 * scale)))
        draw.ellipse((x + 11, y + tip_h + 5, x + 20, y + tip_h + 14), fill=highlight)

        if value != "":
            label = str(value)
            font = get_font(max(18, int(26 * scale)), bold=True)
            box = draw.textbbox((0, 0), label, font=font)
            tw = box[2] - box[0]
            th = box[3] - box[1]
            draw.text((cx - tw / 2, y + tip_h + body_h / 2 - th / 2 - 1), label, font=font, fill=(255, 255, 255, 255))

    for idx, card in enumerate(normalized):
        col = idx % cols
        row = idx // cols
        x = start_x + col * (card_w + gap_x)
        y = start_y + row * (card_h + gap_y)

        style = style_for_card(card)

        # subtle colored glow only for special cards
        draw.rounded_rectangle((x - 2, y - 2, x + card_w + 2, y + card_h + 2), radius=26, outline=(*style["glow"], 80), width=3)
        shadow_box(x, y, x + card_w, y + card_h, radius=24, alpha=88)

        icon_bytes = await get_card_image_bytes(card)
        if icon_bytes:
            try:
                icon = Image.open(BytesIO(icon_bytes)).convert("RGBA")
                icon = ImageOps.contain(icon, (card_w, card_h))
                px = x + (card_w - icon.width) // 2
                py = y + (card_h - icon.height) // 2
                bg.paste(icon, (px, py), icon)
            except Exception:
                logging.exception("Failed to render card art %s", card["name"])
                draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=22, fill=(50, 58, 74, 255))
        else:
            draw.rounded_rectangle((x, y, x + card_w, y + card_h), radius=22, fill=(50, 58, 74, 255))

        draw_diamond(x + card_w // 2, y - 2, style)

        elixir = card_elixir(card)
        if elixir is not None:
            draw_elixir_drop(x - 8, y + 2, elixir, scale=0.88)

        if style["badge_fill"] and style["badge_text"]:
            bw = 70 if style["badge_text"] != "CHAMP" else 96
            draw_badge(x + card_w - bw - 6, y + 8, style["badge_text"], style["badge_fill"], style["badge_top"], bw)

    # divider under cards
    divider_y = 688
    draw.line((52, divider_y, width - 52, divider_y), fill=(80, 88, 108, 255), width=2)

    # Average elixir block
    avg = average_elixir(normalized)
    avg_y = 920
    draw.rounded_rectangle((44 + 3, avg_y + 4, 204 + 3, avg_y + 78 + 4), radius=18, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((44, avg_y, 204, avg_y + 78), radius=18, fill=(47, 59, 93, 255), outline=(112, 124, 170, 255), width=2)
    draw_elixir_drop(54, avg_y + 8, "", scale=0.92)
    avg_text = f"{avg:.1f}" if avg is not None else "—"
    draw.text((118, avg_y + 16), avg_text, font=get_font(40, bold=True), fill=(255, 255, 255, 255))

    # Tower troop block improved
    tower_label = tower_troop.strip() if tower_troop else "Tower Troop"
    tt_x1, tt_y1, tt_x2, tt_y2 = 770, avg_y, 1030, avg_y + 78
    draw.rounded_rectangle((tt_x1 + 3, tt_y1 + 4, tt_x2 + 3, tt_y2 + 4), radius=18, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((tt_x1, tt_y1, tt_x2, tt_y2), radius=18, fill=(46, 81, 132, 255), outline=(122, 170, 232, 255), width=2)
    sx, sy = tt_x1 + 18, tt_y1 + 14
    shield = [(sx + 22, sy), (sx + 44, sy + 8), (sx + 44, sy + 31), (sx + 22, sy + 46), (sx, sy + 31), (sx, sy + 8)]
    draw.polygon(shield, fill=(255, 221, 95, 255), outline=(255, 246, 205, 255))
    draw.polygon([(sx + 22, sy + 6), (sx + 34, sy + 12), (sx + 34, sy + 27), (sx + 22, sy + 35), (sx + 10, sy + 27), (sx + 10, sy + 12)], fill=(104, 177, 244, 255))
    tt_text, tt_font = fit_text(draw, tower_label, 165, size=18, bold=True)
    draw.text((tt_x1 + 76, tt_y1 + 15), tt_text, font=tt_font, fill=(255, 255, 255, 255))
    draw.text((tt_x1 + 76, tt_y1 + 43), "seasonal arena theme", font=get_font(13), fill=(229, 239, 255, 255))

    # Compact info/footer panel
    footer_x1, footer_y1, footer_x2, footer_y2 = 18, 1120, width - 18, height - 18
    draw.rounded_rectangle((footer_x1 + 6, footer_y1 + 8, footer_x2 + 6, footer_y2 + 10), radius=28, fill=(0, 0, 0, 100))
    draw.rounded_rectangle((footer_x1, footer_y1, footer_x2, footer_y2), radius=28, fill=(20, 22, 30, 235), outline=(88, 104, 136, 255), width=2)

    draw.text((42, 1148), "Battle Deck", font=get_font(30, bold=True), fill=(255, 255, 255, 255))

    # decorative slots
    slot_y = 1161
    for i in range(3):
        x1 = 278 + i * 112
        draw.rounded_rectangle((x1, slot_y, x1 + 52, slot_y + 16), radius=8, outline=(124, 136, 170, 255), width=2)

    mode_text = f"Mode: {deck_mode_badge(mode_label, source_label)}"
    arena_text = f"Arena theme: {theme['arena_name']}"
    if season_id:
        arena_text += f" · Season {season_id}"
    source_text = f"Source: {source_label or 'Live data'}"

    lines = [mode_text, arena_text, source_text]
    y_cursor = 1200
    for line in lines:
        draw.text((42, y_cursor), line, font=get_font(20), fill=(228, 236, 251, 255))
        y_cursor += 34

    # season accent strip
    strip_color = mix(theme["accent"], (255, 255, 255), 0.2)
    draw.rounded_rectangle((42, 1316, width - 42, 1328), radius=6, fill=(*strip_color, 255))

    # tower silhouettes and import prompt
    base_y = 1342
    draw.rounded_rectangle((74, base_y + 44, 172, base_y + 104), radius=8, fill=(113, 79, 46, 255))
    draw.rounded_rectangle((87, base_y, 159, base_y + 60), radius=8, fill=(203, 162, 79, 255), outline=(255, 234, 164, 140), width=2)
    draw.rectangle((104, base_y - 14, 144, base_y + 10), fill=(86, 113, 183, 255))

    draw.rounded_rectangle((230, base_y + 52, 310, base_y + 104), radius=8, fill=(113, 79, 46, 255))
    draw.rounded_rectangle((240, base_y + 12, 301, base_y + 64), radius=8, fill=(197, 158, 79, 255), outline=(255, 234, 164, 140), width=2)

    info_x1, info_y1, info_x2, info_y2 = 738, 1350, 1028, 1424
    draw.rounded_rectangle((info_x1 + 3, info_y1 + 4, info_x2 + 3, info_y2 + 4), radius=18, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((info_x1, info_y1, info_x2, info_y2), radius=18, fill=(34, 54, 92, 255), outline=(110, 145, 205, 255), width=2)
    draw.text((760, 1366), "Deck Import", font=get_font(22, bold=True), fill=(255, 255, 255, 255))
    draw.text((760, 1392), "Use the button below", font=get_font(15), fill=(226, 236, 250, 255))
    draw.text((760, 1412), "to open this deck in Clash Royale", font=get_font(15), fill=(226, 236, 250, 255))

    buffer = BytesIO()
    bg.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def deck_action_rows(
    deck_link: str | None,
    back_callback: str,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
):
    rows = []

    if deck_link:
        rows.append([
            InlineKeyboardButton(
                text="🎮 Импортировать в Clash Royale",
                url=deck_link,
            )
        ])

    if extra_rows:
        rows.extend(extra_rows)

    rows.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
        InlineKeyboardButton(text="🏠 Меню", callback_data="home"),
    ])

    return rows


async def replace_with_text(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
):
    """
    Безопасно возвращает интерфейс к обычному текстовому сообщению.
    Если текущее сообщение является фото, edit_text невозможен:
    удаляем его и отправляем новое сообщение.
    """
    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest:
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )


async def replace_with_deck_photo(
    callback: CallbackQuery,
    cards: list,
    title: str,
    caption: str,
    reply_markup=None,
    subtitle: str = "",
    mode_label: str = "",
    source_label: str = "",
    season_id: str | None = None,
    tower_troop: str = "",
):
    try:
        image_bytes = await build_deck_image(
            cards=cards,
            title=title,
            subtitle=subtitle,
            mode_label=mode_label,
            source_label=source_label,
            season_id=season_id,
            tower_troop=tower_troop,
        )
        photo = BufferedInputFile(image_bytes, filename="deck.png")

        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=reply_markup,
        )

    except Exception:
        logging.exception("Deck preview render failed")
        await replace_with_text(
            callback,
            caption + "\n\n⚠️ Не удалось собрать визуальный превью колоды.",
            reply_markup=reply_markup,
        )


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
    await callback.answer()
    await replace_with_text(
        callback,
        "🏠 <b>Clash Decks</b>\n\nВыбери раздел:",
        reply_markup=main_menu(),
    )


# =========================
# МЕТА
# =========================

def meta_keyboard():
    rows = []

    for idx, item in enumerate(meta_decks_cache[:10]):
        wr = (
            f"{item['win_rate']}%"
            if item["win_rate"] is not None
            else "—"
        )
        rows.append([
            InlineKeyboardButton(
                text=f"🃏 #{idx + 1} · WR {wr} · {item['games']} боёв",
                callback_data=f"meta_deck:{idx}",
            )
        ])

    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "meta")
async def meta(callback: CallbackQuery):
    await callback.answer("Обновляю данные…")
    await ensure_live_data()

    if not meta_decks_cache:
        await replace_with_text(
            callback,
            "🔥 <b>МЕТА</b>\n\n"
            "Live-колоды пока не загрузились. Попробуй ещё раз через минуту.",
            reply_markup=back_button(),
        )
        return

    msg = (
        "🔥 <b>АКТУАЛЬНАЯ МЕТА</b>\n\n"
        "Выборка: последние 1v1 бои топ-30 игроков Ranked.\n"
        "Нажми на колоду — откроется превью с игровыми иконками, "
        "EVO/HERO и уровнями.\n\n"
        f"🕒 Обновлено: {format_last_updated()}"
    )

    await replace_with_text(callback, msg, reply_markup=meta_keyboard())


@dp.callback_query(F.data.startswith("meta_deck:"))
async def meta_deck_detail(callback: CallbackQuery):
    try:
        idx = int(callback.data.split(":", 1)[1])
        item = meta_decks_cache[idx]
    except (ValueError, IndexError):
        await callback.answer("Колода больше не доступна", show_alert=True)
        return

    await callback.answer()

    wr = (
        f"{item['win_rate']}%"
        if item["win_rate"] is not None
        else "—"
    )

    caption = (
        f"🔥 <b>Мета #{idx + 1}</b>\n"
        f"📈 Win rate: <b>{wr}</b>\n"
        f"⚔️ Матчей в выборке: <b>{item['games']}</b>\n"
        f"📅 Сезон: <b>{escape(leaderboard_season_id or 'текущий')}</b>\n"
        f"🕒 {format_last_updated()}"
    )

    deck_link = build_deck_link(item["cards"])
    kb = InlineKeyboardMarkup(
        inline_keyboard=deck_action_rows(
            deck_link=deck_link,
            back_callback="meta",
        )
    )

    await replace_with_deck_photo(
        callback,
        item["cards"],
        title=f"Meta Deck #{idx + 1}",
        subtitle=f"WR {wr} · {item['games']} matches",
        caption=caption,
        reply_markup=kb,
        mode_label="Meta / Ranked",
        source_label=leaderboard_source_label,
        season_id=leaderboard_season_id,
        tower_troop="Tower Troop: —",
    )


# =========================
# ТОП
# =========================

@dp.callback_query(F.data == "top")
async def top(callback: CallbackQuery):
    sorted_decks = sorted(decks, key=lambda x: x.games, reverse=True)

    rows = []
    for deck in sorted_decks[:10]:
        rows.append([
            InlineKeyboardButton(
                text=f"🃏 {deck.name} · {deck.win_rate}% WR",
                callback_data=f"deck:{deck.id}:top",
            )
        ])

    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])

    await callback.answer()
    await replace_with_text(
        callback,
        "🏆 <b>ТОП КОЛОД</b>\n\nНажми на колоду, чтобы открыть её карточки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )



# =========================
# ТОП 100 ИГРОКОВ
# =========================

def player_rating_text(player: dict) -> str:
    elo = player.get("eloRating")
    trophies = player.get("trophies")

    if elo is not None:
        return f"{elo} рейтинга"
    if trophies is not None:
        return f"{trophies} 🏆"
    return "рейтинг —"


def top100_keyboard(offset: int = 0):
    rows = []

    for index, player in enumerate(
        top_players_cache[offset:offset + 10],
        offset,
    ):
        name = str(player.get("name", "Unknown"))
        short_name = name if len(name) <= 18 else name[:17] + "…"
        deck_mark = "🃏" if player.get("recent_deck") else "▫️"

        rows.append([
            InlineKeyboardButton(
                text=f"{deck_mark} #{index + 1} {short_name}",
                callback_data=f"player_deck:{index}:{offset}",
            )
        ])

    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"top100:{max(0, offset - 10)}",
            )
        )
    if offset + 10 < min(100, len(top_players_cache)):
        nav.append(
            InlineKeyboardButton(
                text="➡️ Далее",
                callback_data=f"top100:{offset + 10}",
            )
        )
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "top100")
async def top100(callback: CallbackQuery):
    await callback.answer("Загружаю топ игроков…")
    await ensure_live_data()

    if not top_players_cache:
        await replace_with_text(
            callback,
            "👑 <b>ТОП 100 ИГРОКОВ</b>\n\n"
            "API доступен, но leaderboard пока пуст.",
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

    lines = [
        "👑 <b>ТОП 100 ИГРОКОВ</b>",
        f"Источник: {escape(leaderboard_source_label)}",
        f"🕒 Обновлено: {format_last_updated()}",
        "",
    ]

    for i, player in enumerate(items, offset + 1):
        lines.append(
            f"<b>{i}. {escape(player.get('name', 'Unknown'))}</b> — "
            f"{escape(player_rating_text(player))}"
        )

    lines.extend([
        "",
        "Нажми на игрока ниже, чтобы открыть его колоду в игровом стиле и импортировать её в Clash Royale.",
        f"Страница {offset // 10 + 1}/10",
    ])

    await replace_with_text(
        callback,
        "\n".join(lines),
        reply_markup=top100_keyboard(offset),
    )


@dp.callback_query(F.data.startswith("player_deck:"))
async def player_deck_detail(callback: CallbackQuery):
    try:
        _, index_text, offset_text = callback.data.split(":")
        index = int(index_text)
        offset = int(offset_text)
        player = top_players_cache[index]
    except (ValueError, IndexError):
        await callback.answer("Данные игрока обновились. Открой список заново.", show_alert=True)
        return

    deck = player.get("recent_deck") or []

    if len(deck) != 8:
        await callback.answer(
            "В последних боях игрока не нашлась полная Ranked-колода.",
            show_alert=True,
        )
        return

    await callback.answer()

    name = str(player.get("name", "Unknown"))
    rank = index + 1
    tag = str(player.get("tag", ""))

    caption = (
        f"👑 <b>#{rank} {escape(name)}</b>\n"
        f"📊 {escape(player_rating_text(player))}\n"
        f"🏷 <code>{escape(tag)}</code>\n"
        f"📅 {escape(leaderboard_source_label)}"
    )

    deck_link = build_deck_link(deck)
    kb = InlineKeyboardMarkup(
        inline_keyboard=deck_action_rows(
            deck_link=deck_link,
            back_callback=f"top100:{offset}",
        )
    )

    await replace_with_deck_photo(
        callback,
        deck,
        title=f"#{rank} {name}",
        subtitle=player_rating_text(player),
        caption=caption,
        reply_markup=kb,
        mode_label="Ranked",
        source_label=leaderboard_source_label,
        season_id=leaderboard_season_id,
        tower_troop="Tower Troop: —",
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
    query = message.text.strip().lower()

    results: list[Deck] = []

    for deck in decks:
        names = card_names(deck.cards)
        if query in deck.name.lower() or any(query in name.lower() for name in names):
            results.append(deck)

    if not results:
        await message.answer("😔 Ничего не найдено.", reply_markup=main_menu())
        await state.clear()
        return

    rows = []
    for deck in results[:10]:
        rows.append([
            InlineKeyboardButton(
                text=f"🃏 {deck.name}",
                callback_data=f"deck:{deck.id}:search",
            )
        ])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])

    await message.answer(
        f"🔍 Найдено колод: <b>{len(results)}</b>\n\n"
        "Нажми на колоду, чтобы увидеть карточки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )

    await state.clear()


# =========================
# КОЛОДА
# =========================

@dp.callback_query(F.data.startswith("deck:"))
async def show_deck(callback: CallbackQuery):
    parts = callback.data.split(":")

    try:
        deck_id = int(parts[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный идентификатор колоды", show_alert=True)
        return

    origin = parts[2] if len(parts) >= 3 else "home"
    deck = get_deck(deck_id)

    if not deck:
        await callback.answer("Колода не найдена", show_alert=True)
        return

    user_id = callback.from_user.id
    starred = deck_id in favorites.get(user_id, set())

    await callback.answer()

    caption = (
        f"🃏 <b>{escape(deck.name)}</b>\n"
        f"🎮 Режим: <b>{escape(deck.mode)}</b>\n"
        f"📈 Win rate: <b>{deck.win_rate}%</b>\n"
        f"⚔️ Игр: <b>{deck.games:,}</b>\n"
        f"📡 Источник: <b>{escape(deck.source)}</b>"
    )

    back_callback = "top" if origin == "top" else "home"

    deck_link = build_deck_link(deck.cards)
    kb = InlineKeyboardMarkup(
        inline_keyboard=deck_action_rows(
            deck_link=deck_link,
            back_callback=back_callback,
            extra_rows=[
                [
                    InlineKeyboardButton(
                        text="⭐" if starred else "☆",
                        callback_data=f"fav:{deck.id}",
                    )
                ]
            ],
        )
    )

    await replace_with_deck_photo(
        callback,
        deck.cards,
        title=deck.name,
        subtitle=f"{deck.win_rate}% WR · {deck.games:,} games",
        caption=caption,
        reply_markup=kb,
        mode_label=deck.mode,
        source_label=deck.source,
        season_id=leaderboard_season_id,
        tower_troop="Tower Troop: —",
    )


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

    await message.answer(
        f"✅ <b>Колода добавлена!</b>\n\n"
        f"🃏 {escape(deck.name)}\n"
        f"📈 {deck.win_rate}% WR\n"
        "Открой её через «🏆 Топ колоды», чтобы увидеть превью.",
        reply_markup=main_menu(),
    )


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

# Каталог карт и кэш официальных игровых иконок.
cards_catalog_cache: dict[str, dict] = {}
card_image_cache: dict[str, bytes] = {}
local_binary_cache: dict[str, bytes] = {}

# Временный negative-cache для битых/stale URL.
# Это особенно полезно для только что выпущенных карт: CDN Supercell
# может начать отдавать картинку позже, поэтому 404 не кэшируется навсегда.
card_image_bad_urls: dict[str, float] = {}
CARD_IMAGE_BAD_URL_TTL = 15 * 60  # 15 минут
card_image_placeholder_logged: set[str] = set()
local_asset_logged: set[str] = set()

image_cache_lock = asyncio.Lock()

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

    # Каталог карт загрузится лениво при первом превью.
    # Основные live-данные продолжают обновляться в фоне.
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
        "cards_catalog": len(cards_catalog_cache),
        "cached_card_images": len(card_image_cache),
        "bad_card_image_urls": len(card_image_bad_urls),
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
