"""Safe extras adapted from the uploaded standalone bot.

This module is intentionally a Router extension only:
- it NEVER starts polling;
- it NEVER deletes or replaces the Telegram webhook;
- it reuses the existing Clash API/client/cache logic from main.py;
- it does not require Redis.
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


@router.message(Command("lab"))
async def open_lab(message: Message) -> None:
    """Open the same Mini App used by the main menu."""
    if not MINI_APP_URL:
        await message.answer("Mini App пока не настроен на сервере.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚔️ Открыть Clash Decks Lab",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ]
        ]
    )
    await message.answer(
        "⚔️ <b>Clash Decks Lab</b>\n\nМета, билдер, контры, профиль и кланы — внутри Mini App.",
        reply_markup=keyboard,
    )


@router.message(Command("top10"))
async def top10(message: Message) -> None:
    """Top 10 using the working season-aware ranking logic from main.py."""
    try:
        players = await original.fetch_top_100_players()
    except Exception:
        log.exception("Failed to fetch top players")
        await message.answer("Не удалось получить рейтинг прямо сейчас. Попробуй чуть позже.")
        return

    if not players:
        await message.answer("Рейтинг Path of Legends сейчас пуст. Попробуй чуть позже.")
        return

    lines = ["👑 <b>Path of Legends — Top 10</b>", ""]
    for index, player in enumerate(players[:10], start=1):
        rank = player.get("rank") or index
        name = original.escape(player.get("name") or "Unknown")
        score = player.get("eloRating") or player.get("trophies") or "—"
        clan_raw = player.get("clan")
        clan_name = clan_raw.get("name") if isinstance(clan_raw, dict) else ""
        clan = f" · {original.escape(clan_name)}" if clan_name else ""
        lines.append(f"<b>{rank}.</b> {name} — {original.escape(score)}{clan}")

    await message.answer("\n".join(lines))
