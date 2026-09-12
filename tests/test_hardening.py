import asyncio
import os
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import bot_hardening
from bot_hardening import _deck_token, _player_tag
from bot_hardening_runtime import _safe_image_url, validate_production_environment


def valid_original():
    return SimpleNamespace(
        WEBHOOK_SECRET="0123456789abcdef0123456789abcdef",
        PUBLIC_URL="https://bot.example.com",
        CR_API_KEY="test-clash-key",
    )


def test_stable_deck_token_ignores_order():
    a = [{"id": 2}, {"id": 1}, {"name": "Knight"}]
    b = [{"name": "Knight"}, {"id": 1}, {"id": 2}]
    assert _deck_token(a) == _deck_token(b)


def test_player_tag_is_normalized_and_bounded():
    assert _player_tag({"tag": "#abc123"}) == "ABC123"
    assert len(_player_tag({"tag": "#" + "A" * 100})) == 20


def test_environment_fails_closed_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_production_environment(valid_original(), "https://mini.example.com")


def test_environment_accepts_secure_configuration(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/db")
    validate_production_environment(valid_original(), "https://mini.example.com")


def test_image_url_allowlist_blocks_redirect_ssrf_targets():
    assert _safe_image_url("https://api-assets.clashroyale.com/cards/300/example.png")
    assert _safe_image_url("https://clashroyale.com/example.png")
    assert not _safe_image_url("http://api-assets.clashroyale.com/example.png")
    assert not _safe_image_url("https://clashroyale.com.evil.example/example.png")
    assert not _safe_image_url("https://127.0.0.1/internal")
    assert not _safe_image_url("https://user:pass@api-assets.clashroyale.com/example.png")


@dataclass
class FakeDeck:
    id: int
    name: str
    cards: list
    mode: str
    win_rate: float
    games: int
    source: str = "manual"


def test_persistent_favorites_coerce_legacy_set_assignment():
    favorites = bot_hardening.PersistentFavorites()
    favorites[123] = set()
    assert isinstance(favorites[123], bot_hardening.PersistentFavoriteSet)
    assert favorites[123].owner_id == 123


def test_strict_python_deck_link_rejects_duplicates_and_bad_locale():
    original = SimpleNamespace()
    original.build_deck_link = lambda *_args, **_kwargs: "legacy"
    original.merge_catalog_card = lambda card: card
    original.CR_API_KEY = "key"
    original.CR_API_BASE = "https://api.example.com"

    class DummyMiddlewareTarget:
        def outer_middleware(self, _middleware):
            pass

    class DummyDispatcher:
        update = DummyMiddlewareTarget()

        def callback_query(self, *_args, **_kwargs):
            return lambda fn: fn

    class DummyRouter:
        on_startup = []
        on_shutdown = []
        routes = []

    class DummyApp:
        router = DummyRouter()

        def add_event_handler(self, *_args):
            pass

        def middleware(self, *_args):
            return lambda fn: fn

        def post(self, *_args):
            return lambda fn: fn

    original.dp = DummyDispatcher()
    original.app = DummyApp()
    original.on_startup = object()
    original.on_shutdown = object()
    original.WEBHOOK_SECRET = "0123456789abcdef"
    original.meta_decks_cache = []
    original.top_players_cache = []
    original.meta_keyboard = lambda: None
    original.top100_keyboard = lambda _offset=0: None

    bot_hardening.apply_hardening(original)

    cards = [{"id": 26000000 + i} for i in range(8)]
    assert original.build_deck_link(cards, "ru") == (
        "https://link.clashroyale.com/deck/ru?deck="
        "26000000;26000001;26000002;26000003;26000004;26000005;26000006;26000007"
    )
    duplicate = cards[:-1] + [{"id": 26000000}]
    assert original.build_deck_link(duplicate) is None
    assert original.build_deck_link(cards, "../../bad").startswith("https://link.clashroyale.com/deck/en?deck=")


def test_postgres_persistence_round_trip():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is not configured")

    async def scenario():
        first = SimpleNamespace(
            favorites={},
            decks=[],
            Deck=FakeDeck,
            next_deck_id=1,
        )
        await bot_hardening._init_persistence(first)
        assert bot_hardening._db_pool is not None
        await bot_hardening._db_pool.execute("TRUNCATE bot_user_favorites, bot_manual_decks")

        # Mirror the actual legacy handler: it explicitly assigns set() for a
        # first-time user before calling add(). The wrapper must coerce it.
        first.favorites[123456] = set()
        first.favorites[123456].add(77)
        first.decks.append(
            FakeDeck(
                id=501,
                name="CI persisted deck",
                cards=[{"id": i} for i in range(8)],
                mode="manual",
                win_rate=0.0,
                games=0,
            )
        )
        # No arbitrary sleep: close must flush tracked writes deterministically.
        await bot_hardening._close_persistence()

        second = SimpleNamespace(
            favorites={},
            decks=[],
            Deck=FakeDeck,
            next_deck_id=1,
        )
        await bot_hardening._init_persistence(second)
        assert 77 in second.favorites[123456]
        assert any(deck.id == 501 and deck.name == "CI persisted deck" for deck in second.decks)
        assert second.next_deck_id >= 502
        await bot_hardening._db_pool.execute("TRUNCATE bot_user_favorites, bot_manual_decks")
        await bot_hardening._close_persistence()

    asyncio.run(scenario())
