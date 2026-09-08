import asyncio
import logging
import os
import html
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


def card_icon_url(raw_card) -> str | None:
    card = merge_catalog_card(raw_card)
    icons = card.get("iconUrls") or {}
    kind = card_special_kind(card)

    if kind == "hero":
        return (
            icons.get("heroMedium")
            or icons.get("evolutionMedium")
            or icons.get("medium")
        )

    if kind == "evo":
        return icons.get("evolutionMedium") or icons.get("medium")

    return icons.get("medium") or icons.get("evolutionMedium")


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

    url = card_icon_url(raw_card)
    if not url:
        return None

    cached = card_image_cache.get(url)
    if cached:
        return cached

    async with image_cache_lock:
        cached = card_image_cache.get(url)
        if cached:
            return cached

        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.content

            card_image_cache[url] = data
            return data

        except Exception:
            logging.exception("Card icon download failed: %s", url)
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
    V8 renderer:
    - оформление карт ближе к игровому виду;
    - без уровня и без звёздного уровня;
    - эликсир и рамки похожи на скриншоты пользователя;
    - задний фон зависит от режима/испытания и сезона.
    """
    await ensure_cards_catalog()

    normalized = [merge_catalog_card(card) for card in (cards or [])][:8]
    theme = infer_visual_theme(
        mode_label=mode_label,
        source_label=source_label,
        season_id=season_id,
    )

    width = 1080
    height = 1500
    bg = Image.new("RGB", (width, height), theme["top"])
    draw = ImageDraw.Draw(bg)

    # Seasonal/mode background gradient
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(theme["top"][0] * (1 - t) + theme["mid"][0] * t)
        g = int(theme["top"][1] * (1 - t) + theme["mid"][1] * t)
        b = int(theme["top"][2] * (1 - t) + theme["mid"][2] * t)
        draw.line((0, y, width, y), fill=(r, g, b))

    # Decorative arena circles
    for i in range(7):
        ox = 90 + i * 140
        oy = 120 + (i % 2) * 25
        draw.ellipse(
            (ox - 48, oy - 48, ox + 48, oy + 48),
            fill=(255, 255, 255, 0),
            outline=(min(255, theme["accent"][0] + 10), min(255, theme["accent"][1] + 10), min(255, theme["accent"][2] + 10)),
            width=2,
        )
    for i in range(5):
        x = 70 + i * 220
        y = 1115 + (i % 2) * 18
        draw.rounded_rectangle(
            (x, y, x + 110, y + 26),
            radius=13,
            outline=(255, 255, 255),
            width=2,
        )

    # Main frame
    draw.rounded_rectangle(
        (20, 20, width - 20, height - 20),
        radius=28,
        outline=(240, 246, 255),
        width=3,
    )

    title_text, title_font = fit_text(draw, title, width - 80, size=46, bold=True)
    draw.text((34, 28), title_text, font=title_font, fill=(255, 255, 255))

    subtitle_text = subtitle.strip()
    if subtitle_text:
        subtitle_text, sub_font = fit_text(draw, subtitle_text, width - 80, size=24, bold=False)
        draw.text((34, 80), subtitle_text, font=sub_font, fill=(232, 240, 255))

    # Mode / arena badges
    rounded_badge(
        draw,
        (34, 118, 280, 164),
        deck_mode_badge(mode_label, source_label),
        fill=(25, 61, 127),
        text_fill=(255, 255, 255),
        font=get_font(18, bold=True),
    )
    rounded_badge(
        draw,
        (302, 118, 620, 164),
        f"Arena · {theme['arena_name']}",
        fill=(47, 94, 161),
        text_fill=(239, 247, 255),
        font=get_font(18, bold=True),
    )

    cols, rows = 4, 2
    card_w = 235
    card_h = 355
    gap_x = 16
    gap_y = 18
    start_x = 34
    start_y = 190

    def frame_style(card):
        rarity = str(card.get("rarity", "")).lower()
        special = card_special_kind(card)

        if special == "hero":
            return {
                "outer": (246, 185, 57),
                "inner": (255, 219, 105),
                "panel": (43, 60, 124),
                "gem_fill": (255, 199, 62),
                "gem_inner": (255, 236, 143),
                "glow": (255, 219, 120),
            }
        if special == "evo":
            return {
                "outer": (223, 90, 255),
                "inner": (255, 161, 244),
                "panel": (43, 59, 124),
                "gem_fill": (180, 59, 255),
                "gem_inner": (246, 193, 255),
                "glow": (244, 173, 255),
            }
        if "champion" in rarity:
            return {
                "outer": (224, 176, 66),
                "inner": (248, 215, 115),
                "panel": (53, 72, 132),
                "gem_fill": (229, 194, 94),
                "gem_inner": (255, 239, 176),
                "glow": (255, 227, 144),
            }
        if "legendary" in rarity:
            return {
                "outer": (225, 190, 84),
                "inner": (246, 221, 137),
                "panel": (43, 60, 124),
                "gem_fill": (219, 184, 89),
                "gem_inner": (255, 238, 173),
                "glow": (255, 235, 172),
            }
        if "epic" in rarity:
            return {
                "outer": (145, 73, 209),
                "inner": (200, 134, 255),
                "panel": (43, 60, 124),
                "gem_fill": (152, 76, 227),
                "gem_inner": (235, 195, 255),
                "glow": (230, 190, 255),
            }
        return {
            "outer": (138, 164, 212),
            "inner": (203, 219, 248),
            "panel": (43, 60, 124),
            "gem_fill": (226, 193, 89),
            "gem_inner": (255, 238, 183),
            "glow": (215, 229, 255),
        }

    def draw_card_base(x, y, w, h, style):
        # Soft glow
        draw.rounded_rectangle((x - 3, y - 3, x + w + 3, y + h + 3), radius=28, fill=style["glow"])
        # Main frame
        draw.rounded_rectangle((x, y, x + w, y + h), radius=26, fill=style["outer"])
        draw.rounded_rectangle((x + 6, y + 6, x + w - 6, y + h - 6), radius=22, fill=style["inner"])
        draw.rounded_rectangle((x + 13, y + 16, x + w - 13, y + h - 11), radius=18, fill=style["panel"])

        # Crown / diamond top
        cx = x + w // 2
        top_y = y - 8
        diamond = [(cx, top_y), (cx + 22, top_y + 22), (cx, top_y + 44), (cx - 22, top_y + 22)]
        draw.polygon(diamond, fill=style["gem_fill"], outline=(255, 250, 220))
        inner = [(cx, top_y + 6), (cx + 12, top_y + 22), (cx, top_y + 38), (cx - 12, top_y + 22)]
        draw.polygon(inner, fill=style["gem_inner"])

    def draw_elixir_drop(x, y, value):
        fill = (237, 44, 215)
        outline = (255, 215, 255)
        draw.ellipse((x, y + 8, x + 54, y + 58), fill=fill, outline=outline, width=3)
        draw.polygon([(x + 27, y), (x + 40, y + 20), (x + 14, y + 20)], fill=fill, outline=outline)
        font = get_font(30, bold=True)
        label = str(value)
        box = draw.textbbox((0, 0), label, font=font)
        tw = box[2] - box[0]
        th = box[3] - box[1]
        draw.text((x + (54 - tw) / 2, y + 18 + (40 - th) / 2 - 2), label, font=font, fill=(255, 255, 255))

    for idx in range(8):
        col = idx % cols
        row = idx // cols
        x = start_x + col * (card_w + gap_x)
        y = start_y + row * (card_h + gap_y)

        if idx >= len(normalized):
            continue

        card = normalized[idx]
        style = frame_style(card)
        draw_card_base(x, y, card_w, card_h, style)

        # Art area
        art_x1, art_y1 = x + 15, y + 18
        art_x2, art_y2 = x + card_w - 15, y + 260

        icon_bytes = await get_card_image_bytes(card)
        if icon_bytes:
            try:
                icon = Image.open(BytesIO(icon_bytes)).convert("RGBA")
                icon = ImageOps.contain(icon, (art_x2 - art_x1, art_y2 - art_y1))
                px = art_x1 + ((art_x2 - art_x1) - icon.width) // 2
                py = art_y1 + ((art_y2 - art_y1) - icon.height) // 2
                bg.paste(icon, (px, py), icon)
            except Exception:
                logging.exception("Failed to render card art %s", card["name"])

        # Elixir cost
        elixir = card_elixir(card)
        if elixir is not None:
            draw_elixir_drop(x + 14, y + 8, elixir)

        # Name plate
        plate_y1 = y + 268
        plate_y2 = y + 340
        draw.rounded_rectangle((x + 12, plate_y1, x + card_w - 12, plate_y2), radius=16, fill=(22, 34, 75))

        name_text, name_font = fit_text(draw, card["name"], card_w - 28, size=26, bold=True)
        # Slight game-like shadow
        draw.text((x + 19, plate_y1 + 15), name_text, font=name_font, fill=(9, 16, 42))
        draw.text((x + 17, plate_y1 + 13), name_text, font=name_font, fill=(255, 255, 255))

        # Small badge plate
        badge_parts = []
        special = card_special_kind(card)
        rarity = str(card.get("rarity", "")).lower()
        if special == "evo":
            badge_parts.append("EVO")
        elif special == "hero":
            badge_parts.append("HERO")
        if "champion" in rarity:
            badge_parts.append("CHAMP")

        if badge_parts:
            badge_text = " · ".join(badge_parts)
            bw = min(card_w - 24, max(72, len(badge_text) * 12 + 20))
            rounded_badge(
                draw,
                (x + card_w - bw - 12, y + 18, x + card_w - 12, y + 52),
                badge_text,
                fill=(118, 78, 19) if "CHAMP" in badge_text and "EVO" not in badge_text and "HERO" not in badge_text else (123, 55, 178) if "EVO" in badge_text else (189, 127, 33),
                text_fill=(255, 255, 255),
                font=get_font(14, bold=True),
            )

    # Average elixir panel like the in-game block
    avg = average_elixir(normalized)
    panel_y = 952
    draw.rounded_rectangle((34, panel_y, 225, panel_y + 95), radius=24, fill=(37, 72, 148))
    draw.rounded_rectangle((34, panel_y, 225, panel_y + 95), radius=24, outline=(148, 202, 255), width=3)
    draw_elixir_drop(48, panel_y + 18, "")
    avg_text = f"{avg:.1f}" if avg is not None else "—"
    avg_font = get_font(46, bold=True)
    box = draw.textbbox((0, 0), avg_text, font=avg_font)
    tw = box[2] - box[0]
    draw.text((140 - tw / 2, panel_y + 24), avg_text, font=avg_font, fill=(255, 255, 255))

    # Optional tower troop block / fallback label
    tower_label = tower_troop.strip() if tower_troop else "Tower Troop: dynamic slot"
    draw.rounded_rectangle((745, panel_y, 1018, panel_y + 95), radius=24, fill=(57, 146, 220))
    draw.rounded_rectangle((745, panel_y, 1018, panel_y + 95), radius=24, outline=(185, 225, 255), width=3)
    tt_text, tt_font = fit_text(draw, tower_label, 250, size=18, bold=True)
    draw.text((765, panel_y + 18), tt_text, font=tt_font, fill=(255, 255, 255))
    draw.text((765, panel_y + 50), "season-aware deck theme", font=get_font(15), fill=(231, 243, 255))

    # Bottom info block
    info_y = 1092
    draw.line((34, 1076, width - 34, 1076), fill=(215, 233, 255), width=3)
    draw.text((44, info_y + 16), "Battle Deck", font=get_font(31, bold=True), fill=(255, 255, 255))
    draw.text((44, info_y + 60), f"Mode: {deck_mode_badge(mode_label, source_label)}", font=get_font(21, bold=False), fill=(232, 242, 255))
    arena_line = f"Arena theme: {theme['arena_name']}"
    if season_id:
        arena_line += f" · Season {season_id}"
    draw.text((44, info_y + 93), arena_line, font=get_font(21, bold=False), fill=(232, 242, 255))
    draw.text((44, info_y + 126), "Background auto-changes by mode/challenge and season.", font=get_font(19), fill=(212, 228, 255))

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
