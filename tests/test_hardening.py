import asyncio
import os
from types import SimpleNamespace

import pytest

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
