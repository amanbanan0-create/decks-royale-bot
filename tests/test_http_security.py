import asyncio
import os

import httpx
import pytest

import bot_hardening


_REQUIRED_WRAPPER_ENV = (
    "BOT_TOKEN",
    "WEBHOOK_SECRET",
    "CLASH_ROYALE_API_KEY",
    "RENDER_EXTERNAL_URL",
    "MINI_APP_URL",
    "DATABASE_URL",
)


def _load_hardened_wrapper():
    missing = [name for name in _REQUIRED_WRAPPER_ENV if not os.getenv(name, "").strip()]
    if missing:
        pytest.skip("hardened wrapper test environment is not configured: " + ", ".join(missing))
    import main_miniapp

    return main_miniapp


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    main_miniapp = _load_hardened_wrapper()
    transport = httpx.ASGITransport(app=main_miniapp.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        return await client.request(method, path, **kwargs)


def request(method: str, path: str, **kwargs) -> httpx.Response:
    return asyncio.run(_request(method, path, **kwargs))


def test_operator_diagnostics_return_401_without_secret():
    for path in ("/api-status", "/api-test", "/webhook-status"):
        response = request("GET", path)
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}


def test_telegram_webhook_rejects_wrong_secret_before_parsing_body():
    response = request(
        "POST",
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        content=b"not-json",
    )
    assert response.status_code == 403


def test_telegram_webhook_rejects_invalid_update_with_valid_secret():
    main_miniapp = _load_hardened_wrapper()
    response = request(
        "POST",
        "/telegram/webhook",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": str(main_miniapp.original.WEBHOOK_SECRET),
            "content-type": "application/json",
        },
        json={},
    )
    assert response.status_code == 400


def test_production_startup_fails_closed_without_live_persistence(monkeypatch):
    main_miniapp = _load_hardened_wrapper()
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured-but-unavailable/example")
    monkeypatch.setattr(bot_hardening, "_db_pool", None)
    with pytest.raises(RuntimeError, match="PostgreSQL persistence is unavailable"):
        asyncio.run(main_miniapp.require_persistence_ready())
