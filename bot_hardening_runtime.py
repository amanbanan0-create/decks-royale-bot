"""Additional bounded-runtime hardening for the aiogram/FastAPI wrapper.

This module intentionally contains no Telegram polling and never deletes the webhook.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi.responses import JSONResponse

_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_IMAGE_LOCK_LIMIT = 256
_PNG_CACHE_LIMIT = 96
_PNG_CACHE_TTL = 10 * 60.0

_image_client: httpx.AsyncClient | None = None
_image_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
_render_semaphore = asyncio.Semaphore(2)
_png_cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()


def _https_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.netloc)
    except Exception:
        return False


def _safe_image_url(value: str) -> bool:
    """Allow only HTTPS Clash Royale asset hosts; never follow arbitrary redirects."""
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https"
            and bool(host)
            and (host == "clashroyale.com" or host.endswith(".clashroyale.com"))
            and parsed.username is None
            and parsed.password is None
        )
    except Exception:
        return False


def validate_production_environment(original: Any, mini_app_url: str) -> None:
    """Reject insecure production configuration before Uvicorn starts serving traffic."""
    errors: list[str] = []
    if not getattr(original, "WEBHOOK_SECRET", "") or len(str(original.WEBHOOK_SECRET)) < 16:
        errors.append("WEBHOOK_SECRET must contain at least 16 characters")
    if not _https_url(str(getattr(original, "PUBLIC_URL", ""))):
        errors.append("RENDER_EXTERNAL_URL/PUBLIC_URL must be an HTTPS URL")
    if not _https_url(mini_app_url):
        errors.append("MINI_APP_URL must be an HTTPS URL")
    if not getattr(original, "CR_API_KEY", ""):
        errors.append("CLASH_ROYALE_API_KEY is required for live production data")
    if not os.getenv("DATABASE_URL", "").strip():
        errors.append("DATABASE_URL is required for persistent bot favorites/manual decks")

    if errors:
        raise RuntimeError("Invalid Clash Decks bot production configuration: " + "; ".join(errors))


async def _get_image_client() -> httpx.AsyncClient:
    global _image_client
    if _image_client is None or _image_client.is_closed:
        _image_client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            follow_redirects=False,
            headers={"User-Agent": "ClashDecksBot/2.1"},
        )
    return _image_client


async def _close_image_client() -> None:
    global _image_client
    if _image_client is not None and not _image_client.is_closed:
        await _image_client.aclose()
    _image_client = None


def _lock_for(key: str) -> asyncio.Lock:
    lock = _image_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _image_locks[key] = lock
    else:
        _image_locks.move_to_end(key)
    # Keep the lock registry strictly bounded. Removing an in-use lock from the
    # registry is safe: holders retain their reference; at worst singleflight is
    # temporarily lost for that URL, while memory remains bounded.
    while len(_image_locks) > _IMAGE_LOCK_LIMIT:
        _image_locks.popitem(last=False)
    return lock


def _render_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        raw = json.dumps([args, kwargs], sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = repr((args, kwargs))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def apply_runtime_hardening(original: Any) -> None:
    """Patch image fetching, diagnostics and deck rendering with bounded resources."""

    async def hardened_card_image_bytes(raw_card: Any) -> bytes | None:
        await original.ensure_cards_catalog()
        card = original.merge_catalog_card(raw_card)
        card_name = card.get("name") or "Unknown card"
        normalized_name = original.normalize_card_name(card_name)
        candidates = [url for url in original.card_icon_candidates(raw_card) if _safe_image_url(url)]
        now = time.time()

        for url in candidates:
            cached = original.card_image_cache.get(url)
            if cached:
                return cached

        client = await _get_image_client()
        for url in candidates:
            failed_at = original.card_image_bad_urls.get(url)
            if failed_at and now - failed_at < original.CARD_IMAGE_BAD_URL_TTL:
                continue

            async with _lock_for(url):
                cached = original.card_image_cache.get(url)
                if cached:
                    return cached
                try:
                    async with client.stream("GET", url) as response:
                        content_type = response.headers.get("content-type", "").lower()
                        if response.status_code != 200:
                            raise RuntimeError(f"image HTTP {response.status_code}")
                        if content_type and not content_type.startswith("image/"):
                            raise RuntimeError("non-image content type")

                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > _IMAGE_MAX_BYTES:
                                    raise RuntimeError("image payload size rejected")
                            except ValueError:
                                pass

                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > _IMAGE_MAX_BYTES:
                                raise RuntimeError("image payload size rejected")
                            data.extend(chunk)

                    if not data:
                        raise RuntimeError("empty image payload")
                    result = bytes(data)
                    original.card_image_cache[url] = result
                    original.card_image_bad_urls.pop(url, None)
                    return result
                except Exception as exc:
                    original.card_image_bad_urls[url] = time.time()
                    logging.info("Card image fetch failed for %s (%s): %s", card_name, url, type(exc).__name__)

        local_path = original.LOCAL_CARD_ASSETS.get(normalized_name)
        if local_path:
            data = original.get_cached_local_binary(local_path)
            if data:
                return data

        if card_name not in original.card_image_placeholder_logged:
            logging.info("No reachable official card image for %s; using placeholder", card_name)
            original.card_image_placeholder_logged.add(card_name)
        return None

    base_build = original.build_deck_image

    async def cached_bounded_build(*args: Any, **kwargs: Any) -> bytes:
        key = _render_key(args, kwargs)
        now = time.time()
        hit = _png_cache.get(key)
        if hit and now - hit[0] < _PNG_CACHE_TTL:
            _png_cache.move_to_end(key)
            return hit[1]

        async with _render_semaphore:
            hit = _png_cache.get(key)
            if hit and time.time() - hit[0] < _PNG_CACHE_TTL:
                _png_cache.move_to_end(key)
                return hit[1]
            data = await base_build(*args, **kwargs)
            _png_cache[key] = (time.time(), data)
            _png_cache.move_to_end(key)
            while len(_png_cache) > _PNG_CACHE_LIMIT:
                _png_cache.popitem(last=False)
            return data

    original.get_card_image_bytes = hardened_card_image_bytes
    original.build_deck_image = cached_bounded_build

    # Remove misleading "Ranked" visual labels when Top-100 falls back to Trophy Road.
    base_replace = original.replace_with_deck_photo

    async def honest_replace_with_deck_photo(*args: Any, **kwargs: Any):
        mode_label = kwargs.get("mode_label")
        if mode_label == "Meta / Ranked":
            kwargs["mode_label"] = "Meta"
        elif mode_label == "Top 100 / Ranked":
            kwargs["mode_label"] = "Top 100"
        return await base_replace(*args, **kwargs)

    original.replace_with_deck_photo = honest_replace_with_deck_photo

    diagnostics_secret = os.getenv("DIAGNOSTICS_SECRET", str(getattr(original, "WEBHOOK_SECRET", ""))).strip()

    # This middleware is registered after the legacy hardening middleware, so it runs
    # first and returns a real response instead of raising from inside middleware.
    @original.app.middleware("http")
    async def safe_diagnostics_guard(request, call_next):
        if request.url.path in {"/api-status", "/api-test", "/webhook-status"}:
            supplied = request.headers.get("x-diagnostics-secret", "")
            if not diagnostics_secret or not hmac.compare_digest(supplied, diagnostics_secret):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    async def close_runtime_resources() -> None:
        await _close_image_client()

    original.app.router.on_shutdown.append(close_runtime_resources)
