import asyncio
import os
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import bot_hardening
from bot_hardening import _deck_token, _player_tag
from bot_hardening_runtime import validate_production_environment


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


@dataclass
class FakeDeck:
    id: int
    name: str
    cards: list
    mode: str
    win_rate: float
    games: int
    source: str = "manual"


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
        await asyncio.sleep(0.15)
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
