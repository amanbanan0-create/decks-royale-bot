"""Safe Telegram UX extras adapted from the uploaded standalone bot.

This module is intentionally a Router extension only:
- it NEVER starts polling;
- it NEVER deletes or replaces the Telegram webhook;
- it reuses the existing live Clash API/client/cache logic from main.py;
- it does not require Redis;
- it never introduces simulated rankings or seeded meta data.
"""

from __future__ import annotations

import logging
import os

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

import main as original

log = logging.getLogger("clash-decks-extra")
router = Router(name="clash-decks-extra")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()

_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "lab_unavailable": "Mini App is not configured on the server yet.",
        "lab_title": "⚔️ <b>Clash Decks</b>",
        "lab_body": "Live meta, challenges, Top 100, deck analysis and player profiles — inside the Mini App.",
        "open_app": "⚔️ Open Clash Decks",
        "decks_title": "🃏 <b>Live decks</b>",
        "decks_body": "Choose live meta/challenges here or open the full deck lab.",
        "meta": "🔥 Live meta",
        "top_decks": "🏆 Top decks",
        "challenges": "🎯 Challenges",
        "top_fail": "Couldn't load the ranking right now. Try again in a moment.",
        "top_empty": "Path of Legends ranking is empty right now. Try again later.",
        "top_title": "👑 <b>Path of Legends — Top 10</b>",
        "open_top": "👑 Open Top 100",
    },
    "ru": {
        "lab_unavailable": "Mini App пока не настроен на сервере.",
        "lab_title": "⚔️ <b>Clash Decks</b>",
        "lab_body": "Живая мета, испытания, Топ-100, анализ колод и профили игроков — внутри Mini App.",
        "open_app": "⚔️ Открыть Clash Decks",
        "decks_title": "🃏 <b>Актуальные колоды</b>",
        "decks_body": "Выбери живую мету/испытания здесь или открой полный Deck Lab.",
        "meta": "🔥 Живая мета",
        "top_decks": "🏆 Топ колоды",
        "challenges": "🎯 Испытания",
        "top_fail": "Не удалось получить рейтинг прямо сейчас. Попробуй чуть позже.",
        "top_empty": "Рейтинг Path of Legends сейчас пуст. Попробуй чуть позже.",
        "top_title": "👑 <b>Path of Legends — Топ 10</b>",
        "open_top": "👑 Открыть Топ 100",
    },
    "es": {
        "lab_unavailable": "La Mini App todavía no está configurada en el servidor.",
        "lab_title": "⚔️ <b>Clash Decks</b>",
        "lab_body": "Meta en vivo, desafíos, Top 100, análisis de mazos y perfiles — dentro de la Mini App.",
        "open_app": "⚔️ Abrir Clash Decks",
        "decks_title": "🃏 <b>Mazos en vivo</b>",
        "decks_body": "Elige meta/desafíos aquí o abre el laboratorio completo.",
        "meta": "🔥 Meta en vivo",
        "top_decks": "🏆 Mejores mazos",
        "challenges": "🎯 Desafíos",
        "top_fail": "No se pudo cargar la clasificación. Inténtalo de nuevo en un momento.",
        "top_empty": "La clasificación de Path of Legends está vacía ahora mismo.",
        "top_title": "👑 <b>Path of Legends — Top 10</b>",
        "open_top": "👑 Abrir Top 100",
    },
}


def _lang(message: Message) -> str:
    code = message.from_user.language_code if message.from_user else None
    lang = (code or "en").split("-")[0].lower()
    return lang if lang in _TEXT else "en"


def _t(message: Message, key: str) -> str:
    return _TEXT[_lang(message)][key]


def _app_button(message: Message, text_key: str = "open_app") -> InlineKeyboardButton | None:
    if not MINI_APP_URL:
        return None
    return InlineKeyboardButton(text=_t(message, text_key), web_app=WebAppInfo(url=MINI_APP_URL))


@router.message(Command("lab"))
async def open_lab(message: Message) -> None:
    """Open the same authenticated Mini App used by the main menu."""
    button = _app_button(message)
    if button is None:
        await message.answer(_t(message, "lab_unavailable"))
        return

    await message.answer(
        f"{_t(message, 'lab_title')}\n\n{_t(message, 'lab_body')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )


@router.message(Command("decks"))
async def decks_shortcut(message: Message) -> None:
    """Compact live-deck launcher using the production bot callbacks."""
    rows = [
        [
            InlineKeyboardButton(text=_t(message, "meta"), callback_data="meta"),
            InlineKeyboardButton(text=_t(message, "top_decks"), callback_data="top"),
        ],
        [InlineKeyboardButton(text=_t(message, "challenges"), callback_data="challenges")],
    ]
    button = _app_button(message)
    if button is not None:
        rows.append([button])

    await message.answer(
        f"{_t(message, 'decks_title')}\n\n{_t(message, 'decks_body')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _send_top10(message: Message) -> None:
    """Top 10 from the same live season-aware ranking path as the production bot."""
    try:
        players = await original.fetch_top_100_players()
    except Exception:
        log.exception("Failed to fetch top players")
        await message.answer(_t(message, "top_fail"))
        return

    if not players:
        await message.answer(_t(message, "top_empty"))
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [_t(message, "top_title"), ""]
    for index, player in enumerate(players[:10], start=1):
        rank = original.safe_int(player.get("rank"), index)
        prefix = medals.get(rank, f"<b>{rank}.</b>")
        name = original.escape(player.get("name") or "Unknown")
        score = player.get("eloRating") or player.get("trophies") or "—"
        clan_raw = player.get("clan")
        clan_name = clan_raw.get("name") if isinstance(clan_raw, dict) else ""
        clan = f" · {original.escape(clan_name)}" if clan_name else ""
        lines.append(f"{prefix} {name} — <b>{original.escape(score)}</b>{clan}")

    markup = None
    button = _app_button(message, "open_top")
    if button is not None:
        markup = InlineKeyboardMarkup(inline_keyboard=[[button]])
    await message.answer("\n".join(lines), reply_markup=markup)


@router.message(Command("top10"))
async def top10(message: Message) -> None:
    await _send_top10(message)


@router.message(Command("top"))
async def top_alias(message: Message) -> None:
    """User-friendly alias imported from the standalone bot UX."""
    await _send_top10(message)
