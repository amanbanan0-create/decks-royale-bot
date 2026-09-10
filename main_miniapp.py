import logging
import os

import main as original
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo,
)


MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
_base_main_menu = original.main_menu


def main_menu() -> InlineKeyboardMarkup:
    """Добавляет кнопку Mini App, не меняя остальные функции старого бота."""
    base = _base_main_menu()

    if not MINI_APP_URL:
        return base

    rows = list(base.inline_keyboard)
    already_added = any(
        button.web_app and button.web_app.url == MINI_APP_URL
        for row in rows
        for button in row
    )

    if not already_added:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text="⚔️ Открыть Clash Decks Lab",
                    web_app=WebAppInfo(url=MINI_APP_URL),
                )
            ],
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


# Все уже зарегистрированные aiogram-обработчики обращаются к main_menu
# через глобальное имя модуля main, поэтому достаточно заменить его здесь.
original.main_menu = main_menu


@original.app.on_event("startup")
async def configure_mini_app_menu_button() -> None:
    """Добавляет постоянную кнопку Mini App в меню Telegram."""
    if not MINI_APP_URL:
        logging.warning("MINI_APP_URL is not configured; Mini App button is disabled")
        return

    try:
        await original.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="⚔️ Deck Lab",
                web_app=WebAppInfo(url=MINI_APP_URL),
            )
        )
        logging.info("Telegram Mini App menu button set: %s", MINI_APP_URL)
    except Exception:
        logging.exception("Failed to set Telegram Mini App menu button")


# Render/Uvicorn импортирует именно этот объект.
app = original.app
