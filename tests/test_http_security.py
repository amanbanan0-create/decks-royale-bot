import asyncio

import httpx

import main_miniapp


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
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
