import logging
import os

import main as original
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo,
)
from extra_features import router as extra_router
from bot_hardening import apply_hardening
from bot_hardening_runtime import apply_runtime_hardening, validate_production_environment


MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip()
_base_main_menu = original.main_menu


# Fail closed before registering production lifecycle handlers. main remains untouched;
# this wrapper is the Render entrypoint for the hardened branch.
validate_production_environment(original, MINI_APP_URL)


def main_menu() -> InlineKeyboardMarkup:
    """Adds the Mini App button without changing the legacy menu contract."""
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


original.main_menu = main_menu
original.dp.include_router(extra_router)

# Apply security/webhook/persistence fixes first, then the bounded image/render layer.
apply_hardening(original)
apply_runtime_hardening(original)


@original.app.on_event("startup")
async def configure_mini_app_menu_button() -> None:
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


app = original.app
