"""Production hardening applied by main_miniapp without forking the large legacy module.

The module intentionally patches the existing aiogram/FastAPI objects in-place so Render keeps
using one bot, one Dispatcher and one Telegram webhook owner.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Any, Coroutine

import httpx
from aiogram import BaseMiddleware, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject, Update
from fastapi import HTTPException, Request


_api_client: httpx.AsyncClient | None = None
_db_pool: Any | None = None
_persistence_tasks: set[asyncio.Task[Any]] = set()
_update_lock = asyncio.Lock()
_update_inflight: set[int] = set()
_update_processed: dict[int, float] = {}
_UPDATE_TTL = 3600.0
_UPDATE_MAX = 4096


def _deck_token(cards: list[Any]) -> str:
    names: list[str] = []
    for card in cards or []:
        if isinstance(card, dict):
            value = str(card.get("id") or card.get("name") or "")
        else:
            value = str(card)
        names.append(value.strip().lower())
    return hashlib.sha256("|".join(sorted(names)).encode()).hexdigest()[:16]


def _player_tag(player: dict[str, Any]) -> str:
    return str(player.get("tag", "")).strip().lstrip("#").upper()[:20]


def _spawn_persistence(coro: Coroutine[Any, Any, Any]) -> None:
    """Track fire-and-forget DB writes so shutdown can flush them safely."""
    task = asyncio.create_task(coro)
    _persistence_tasks.add(task)
    task.add_done_callback(_persistence_tasks.discard)


async def _client() -> httpx.AsyncClient:
    global _api_client
    if _api_client is None or _api_client.is_closed:
        _api_client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
            follow_redirects=False,
        )
    return _api_client


async def _close_client() -> None:
    global _api_client
    if _api_client is not None and not _api_client.is_closed:
        await _api_client.aclose()
    _api_client = None


class NavigationStateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]):
        state = data.get("state")
        clear = False
        if isinstance(event, Message):
            clear = bool(event.text and event.text.split(maxsplit=1)[0].split("@", 1)[0] == "/start")
        elif isinstance(event, CallbackQuery):
            clear = event.data == "home"
        if clear and isinstance(state, FSMContext):
            await state.clear()
        return await handler(event, data)


class PersistentFavoriteSet(set[int]):
    def __init__(self, owner_id: int, values=()):
        super().__init__(values)
        self.owner_id = owner_id

    def add(self, value: int) -> None:
        super().add(value)
        if _db_pool is not None:
            _spawn_persistence(_persist_favorite(self.owner_id, int(value), True))

    def remove(self, value: int) -> None:
        super().remove(value)
        if _db_pool is not None:
            _spawn_persistence(_persist_favorite(self.owner_id, int(value), False))

    def discard(self, value: int) -> None:
        existed = value in self
        super().discard(value)
        if existed and _db_pool is not None:
            _spawn_persistence(_persist_favorite(self.owner_id, int(value), False))


class PersistentFavorites(dict[int, PersistentFavoriteSet]):
    def __setitem__(self, key: int, value) -> None:
        owner_id = int(key)
        if isinstance(value, PersistentFavoriteSet) and value.owner_id == owner_id:
            persistent = value
        else:
            persistent = PersistentFavoriteSet(owner_id, value or ())
        dict.__setitem__(self, owner_id, persistent)

    def __missing__(self, key: int):
        value = PersistentFavoriteSet(int(key))
        dict.__setitem__(self, int(key), value)
        return value


class PersistentDeckList(list[Any]):
    def append(self, deck: Any) -> None:
        super().append(deck)
        if _db_pool is not None and getattr(deck, "id", None) is not None:
            _spawn_persistence(_persist_deck(deck))


async def _persist_favorite(user_id: int, deck_id: int, present: bool) -> None:
    if _db_pool is None:
        return
    try:
        if present:
            await _db_pool.execute(
                "INSERT INTO bot_user_favorites (telegram_id, deck_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                user_id,
                deck_id,
            )
        else:
            await _db_pool.execute(
                "DELETE FROM bot_user_favorites WHERE telegram_id=$1 AND deck_id=$2",
                user_id,
                deck_id,
            )
    except Exception:
        logging.exception("Failed to persist bot favorite")


async def _persist_deck(deck: Any) -> None:
    if _db_pool is None:
        return
    try:
        await _db_pool.execute(
            """
            INSERT INTO bot_manual_decks (id,name,cards,mode,win_rate,games,source)
            VALUES ($1,$2,$3::jsonb,$4,$5,$6,$7)
            ON CONFLICT (id) DO UPDATE SET
              name=EXCLUDED.name,cards=EXCLUDED.cards,mode=EXCLUDED.mode,
              win_rate=EXCLUDED.win_rate,games=EXCLUDED.games,source=EXCLUDED.source
            """,
            int(deck.id),
            str(deck.name),
            json.dumps(list(deck.cards)),
            str(deck.mode),
            float(deck.win_rate),
            int(deck.games),
            str(getattr(deck, "source", "manual")),
        )
    except Exception:
        logging.exception("Failed to persist bot deck")


async def _init_persistence(original: Any) -> None:
    global _db_pool
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        logging.warning("DATABASE_URL is not configured for Python bot; user favorites/manual decks are process-local")
        return
    try:
        import asyncpg

        _db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3, command_timeout=10)
        async with _db_pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_user_favorites (
                  telegram_id BIGINT NOT NULL,
                  deck_id INTEGER NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  PRIMARY KEY (telegram_id, deck_id)
                );
                CREATE TABLE IF NOT EXISTS bot_manual_decks (
                  id INTEGER PRIMARY KEY,
                  name TEXT NOT NULL,
                  cards JSONB NOT NULL,
                  mode TEXT NOT NULL,
                  win_rate DOUBLE PRECISION NOT NULL,
                  games INTEGER NOT NULL,
                  source TEXT NOT NULL DEFAULT 'manual',
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            favorite_rows = await conn.fetch("SELECT telegram_id, deck_id FROM bot_user_favorites")
            deck_rows = await conn.fetch("SELECT id,name,cards,mode,win_rate,games,source FROM bot_manual_decks ORDER BY id")

        favorites = PersistentFavorites()
        for row in favorite_rows:
            user_id = int(row["telegram_id"])
            if user_id not in favorites:
                dict.__setitem__(favorites, user_id, PersistentFavoriteSet(user_id))
            set.add(favorites[user_id], int(row["deck_id"]))
        original.favorites = favorites

        persisted = PersistentDeckList(original.decks)
        known_ids = {int(getattr(deck, "id", -1)) for deck in persisted}
        for row in deck_rows:
            if int(row["id"]) in known_ids:
                continue
            cards = row["cards"] if isinstance(row["cards"], list) else json.loads(row["cards"])
            list.append(
                persisted,
                original.Deck(
                    id=int(row["id"]),
                    name=str(row["name"]),
                    cards=list(cards),
                    mode=str(row["mode"]),
                    win_rate=float(row["win_rate"]),
                    games=int(row["games"]),
                    source=str(row["source"]),
                ),
            )
        original.decks = persisted
        original.next_deck_id = max([int(getattr(deck, "id", 0)) for deck in persisted] + [0]) + 1
        logging.info("Python bot persistent state loaded: favorites=%s manual_decks=%s", len(favorites), len(deck_rows))
    except Exception:
        _db_pool = None
        logging.exception("Python bot persistence is unavailable")


async def _close_persistence() -> None:
    global _db_pool
    # Flush writes spawned by synchronous set/list compatibility wrappers before
    # closing the asyncpg pool. This removes the shutdown race present in the
    # legacy process-local implementation.
    pending = list(_persistence_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    _persistence_tasks.clear()
    if _db_pool is not None:
        await _db_pool.close()
    _db_pool = None


def apply_hardening(original: Any) -> None:
    """Apply runtime fixes before Uvicorn starts the imported FastAPI app."""

    legacy_build_deck_link = original.build_deck_link

    def hardened_build_deck_link(cards: list, locale: str = "en") -> str | None:
        if not isinstance(cards, list) or len(cards) != 8:
            return None
        ids: list[int] = []
        for raw in cards:
            card = original.merge_catalog_card(raw)
            try:
                card_id = int(card.get("id"))
            except (TypeError, ValueError):
                return None
            if card_id <= 0:
                return None
            ids.append(card_id)
        if len(set(ids)) != 8:
            return None
        safe_locale = locale if re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", locale or "") else "en"
        return f"https://link.clashroyale.com/deck/{safe_locale}?deck=" + ";".join(str(card_id) for card_id in ids)

    # Keep a reference only for compatibility diagnostics; all production calls
    # use the strict replacement below.
    original.legacy_build_deck_link = legacy_build_deck_link
    original.build_deck_link = hardened_build_deck_link

    async def hardened_cr_get(path: str, params: dict | None = None):
        if not original.CR_API_KEY:
            raise RuntimeError("CLASH_ROYALE_API_KEY не задан.")
        if not path.startswith("/"):
            raise RuntimeError("Некорректный путь Clash Royale API.")
        client = await _client()
        headers = {"Authorization": f"Bearer {original.CR_API_KEY}", "Accept": "application/json"}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.get(f"{original.CR_API_BASE}{path}", headers=headers, params=params)
                if response.status_code == 429:
                    raw = response.headers.get("retry-after")
                    try:
                        delay = min(5.0, max(0.5, float(raw))) if raw else 1.0 + attempt
                    except ValueError:
                        delay = 1.0 + attempt
                    last_error = RuntimeError(f"API 429 для {path}")
                    if attempt < 2:
                        await asyncio.sleep(delay)
                        continue
                if response.status_code in {502, 503, 504} and attempt < 2:
                    last_error = RuntimeError(f"API {response.status_code} для {path}")
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(f"API {response.status_code} для {path}: {response.text[:500]}")
                try:
                    return response.json()
                except ValueError as exc:
                    raise RuntimeError(f"API вернул не-JSON для {path}: {response.text[:300]}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise RuntimeError(f"Сетевая ошибка API для {path}: {exc}") from exc
        raise last_error or RuntimeError(f"Не удалось получить API {path}")

    original.cr_get = hardened_cr_get
    original.dp.update.outer_middleware(NavigationStateMiddleware())

    async def hardened_startup() -> None:
        await _init_persistence(original)
        webhook_url = f"{original.PUBLIC_URL.rstrip('/')}/telegram/webhook"
        await original.bot.set_webhook(
            webhook_url,
            secret_token=original.WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        original.refresh_task = asyncio.create_task(original.data_refresh_loop())
        logging.info("Telegram webhook set without dropping pending updates: %s", webhook_url)

    async def hardened_shutdown() -> None:
        if original.refresh_task:
            original.refresh_task.cancel()
            try:
                await original.refresh_task
            except asyncio.CancelledError:
                pass
        await _close_persistence()
        await _close_client()
        await original.bot.session.close()

    try:
        original.app.router.on_startup.remove(original.on_startup)
    except ValueError:
        pass
    try:
        original.app.router.on_shutdown.remove(original.on_shutdown)
    except ValueError:
        pass
    original.app.add_event_handler("startup", hardened_startup)
    original.app.add_event_handler("shutdown", hardened_shutdown)

    diagnostics_secret = os.getenv("DIAGNOSTICS_SECRET", original.WEBHOOK_SECRET).strip()

    @original.app.middleware("http")
    async def protect_operator_routes(request: Request, call_next):
        if request.url.path in {"/api-status", "/api-test", "/webhook-status"}:
            supplied = request.headers.get("x-diagnostics-secret", "")
            if not diagnostics_secret or not hmac.compare_digest(supplied, diagnostics_secret):
                raise HTTPException(status_code=401, detail="Unauthorized")
        return await call_next(request)

    # Replace the legacy webhook route with constant-time secret validation and update-id de-duplication.
    original.app.router.routes[:] = [
        route
        for route in original.app.router.routes
        if not (getattr(route, "path", None) == "/telegram/webhook" and "POST" in getattr(route, "methods", set()))
    ]

    @original.app.post("/telegram/webhook")
    async def hardened_webhook(request: Request):
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not original.WEBHOOK_SECRET or not hmac.compare_digest(supplied, original.WEBHOOK_SECRET):
            raise HTTPException(status_code=403, detail="Invalid secret")
        try:
            update = Update.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc

        update_id = int(update.update_id)
        now = time.time()
        async with _update_lock:
            for uid, stamp in list(_update_processed.items()):
                if now - stamp > _UPDATE_TTL:
                    _update_processed.pop(uid, None)
            if update_id in _update_inflight or update_id in _update_processed:
                return {"ok": True, "duplicate": True}
            _update_inflight.add(update_id)

        try:
            await original.dp.feed_update(original.bot, update)
        except Exception:
            logging.exception("Failed to process Telegram update %s", update_id)
            raise HTTPException(status_code=500, detail="Update processing failed")
        finally:
            async with _update_lock:
                _update_inflight.discard(update_id)

        async with _update_lock:
            _update_processed[update_id] = time.time()
            while len(_update_processed) > _UPDATE_MAX:
                _update_processed.pop(next(iter(_update_processed)), None)
        return {"ok": True}

    # Stable meta callbacks: refreshed cache order can no longer make an old button open a different deck.
    def stable_meta_keyboard():
        rows = []
        for index, item in enumerate(original.meta_decks_cache[:10]):
            wr = f"{item['win_rate']}%" if item.get("win_rate") is not None else "—"
            rows.append([
                InlineKeyboardButton(
                    text=f"🃏 #{index + 1} · WR {wr} · {item['games']} боёв",
                    callback_data=f"meta_key:{_deck_token(item.get('cards') or [])}",
                )
            ])
        rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    original.meta_keyboard = stable_meta_keyboard

    @original.dp.callback_query(F.data.startswith("meta_key:"))
    async def stable_meta_detail(callback: CallbackQuery):
        token = (callback.data or "").split(":", 1)[1]
        item = next((row for row in original.meta_decks_cache if _deck_token(row.get("cards") or []) == token), None)
        if item is None:
            await callback.answer("Данные колоды обновились. Открой список заново.", show_alert=True)
            return
        index = original.meta_decks_cache.index(item)
        await callback.answer()
        wr = f"{item['win_rate']}%" if item.get("win_rate") is not None else "—"
        caption = (
            f"🔥 <b>Мета #{index + 1}</b>\n"
            f"📈 Win rate: <b>{wr}</b>\n"
            f"⚔️ Матчей в выборке: <b>{item['games']}</b>\n"
            f"📅 Сезон: <b>{original.escape(original.leaderboard_season_id or 'текущий')}</b>\n"
            f"🕒 {original.format_last_updated()}"
        )
        deck_link = original.build_deck_link(item["cards"])
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=original.deck_action_rows(deck_link=deck_link, back_callback="meta")
        )
        await original.replace_with_deck_photo(
            callback,
            item["cards"],
            title=f"Meta Deck #{index + 1}",
            subtitle=f"WR {wr} · {item['games']} matches",
            caption=caption,
            reply_markup=keyboard,
            mode_label="Meta / Ranked",
            source_label=original.leaderboard_source_label,
            season_id=original.leaderboard_season_id,
            tower_troop="Tower Troop: —",
        )

    def stable_top100_keyboard(offset: int = 0):
        rows = []
        for index, player in enumerate(original.top_players_cache[offset:offset + 10], offset):
            name = str(player.get("name", "Unknown"))
            short_name = name if len(name) <= 18 else name[:17] + "…"
            mark = "🃏" if player.get("recent_deck") else "▫️"
            tag = _player_tag(player)
            rows.append([
                InlineKeyboardButton(
                    text=f"{mark} #{index + 1} {short_name}",
                    callback_data=f"player_tag:{tag}",
                )
            ])
        nav = []
        if offset > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"top100:{max(0, offset - 10)}"))
        if offset + 10 < min(100, len(original.top_players_cache)):
            nav.append(InlineKeyboardButton(text="➡️ Далее", callback_data=f"top100:{offset + 10}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="home")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    original.top100_keyboard = stable_top100_keyboard

    @original.dp.callback_query(F.data.startswith("player_tag:"))
    async def stable_player_detail(callback: CallbackQuery):
        tag = (callback.data or "").split(":", 1)[1].strip().lstrip("#").upper()
        player = next((row for row in original.top_players_cache if _player_tag(row) == tag), None)
        if player is None:
            await callback.answer("Данные игрока обновились. Открой список заново.", show_alert=True)
            return
        deck = player.get("recent_deck") or []
        if len(deck) != 8:
            await callback.answer("В последних боях игрока не нашлась полная Ranked-колода.", show_alert=True)
            return
        index = original.top_players_cache.index(player)
        offset = (index // 10) * 10
        await callback.answer()
        name = str(player.get("name", "Unknown"))
        caption = (
            f"👑 <b>#{index + 1} {original.escape(name)}</b>\n"
            f"📊 {original.escape(original.player_rating_text(player))}\n"
            f"🏷 <code>{original.escape(str(player.get('tag', '')))}</code>\n"
            f"📅 {original.escape(original.leaderboard_source_label)}"
        )
        link = original.build_deck_link(deck)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=original.deck_action_rows(deck_link=link, back_callback=f"top100:{offset}")
        )
        await original.replace_with_deck_photo(
            callback,
            deck,
            title=f"#{index + 1} {name}",
            subtitle=original.player_rating_text(player),
            caption=caption,
            reply_markup=keyboard,
            mode_label="Top 100 / Ranked",
            source_label=original.leaderboard_source_label,
            season_id=original.leaderboard_season_id,
            tower_troop="Tower Troop: —",
        )
