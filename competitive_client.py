"""Consume the same persisted Top-1000 dataset as the Mini App, never a Top-30 substitute."""
from urllib.parse import urlsplit


def api_cards(keys, catalog, played=None):
    forms = {c.get("key"): c for c in (played or [])}
    cards = []
    for key in keys:
        card = catalog.get(key, {})
        numeric = forms.get(key, {}).get("scId") or card.get("scId")
        if numeric is None and isinstance(key, str) and key.startswith("sc_"):
            try:
                numeric = int(key[3:])
            except ValueError:
                pass
        if not isinstance(numeric, int) or isinstance(numeric, bool) or not 0 < numeric < 159000000:
            return []
        form = forms.get(key, {}).get("form", "normal")
        cards.append({"id": numeric, "name": card.get("name") or key,
                      "rarity": card.get("rarity", "common"), "elixirCost": card.get("elixir", 0),
                      "evolutionLevel": 1 if form == "evolution" else 2 if form == "hero" else 0,
                      "iconUrls": {k: v for k, v in {"medium": card.get("iconUrl"),
                          "evolutionMedium": card.get("evolutionIconUrl"), "heroMedium": card.get("heroIconUrl")}.items() if v}})
    return cards if len(cards) == 8 and len({c["id"] for c in cards}) == 8 else []


def parse_competitive_payload(payload, catalog):
    if payload.get("source") not in ("world_top_1000", "unavailable"):
        return []
    result = []
    for deck in payload.get("decks", []):
        if deck.get("source") != "world_top_1000" or deck.get("provenance", {}).get("sourceType") != "world_top_1000":
            continue
        cards = api_cards(deck.get("cards", []), catalog, deck.get("playedCards"))
        stats = deck.get("stats", {})
        games, wins, losses = stats.get("games"), stats.get("wins"), stats.get("losses")
        if not cards or any(not isinstance(n, int) or isinstance(n, bool) for n in (games, wins, losses)):
            continue
        if games <= 0 or min(wins, losses) < 0 or wins + losses > games:
            continue
        result.append({"cards": cards, "games": games, "wins": wins, "losses": losses,
                       "draws": games - wins - losses, "win_rate": round(wins / games * 100, 1),
                       "usage": stats.get("usageRate"), "best_world_rank": stats.get("bestWorldRank"),
                       "source": "Top 1000 World", "as_of": deck.get("asOf"),
                       "stale": bool(deck.get("provenance", {}).get("stale")), "sample_size": deck.get("provenance", {}).get("sampleSize")})
    return result


async def fetch_competitive(base_url):
    import httpx
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MINI_APP_URL must be a trusted HTTPS application URL")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        responses = []
        for path in ("/api/cards", "/api/leaderboard", "/api/decks?limit=20"):
            response = await client.get(base_url.rstrip("/") + path)
            response.raise_for_status()
            if len(response.content) > 4_000_000:
                raise ValueError("competitive response too large")
            responses.append(response.json())
    catalog = {c["key"]: c for c in responses[0].get("cards", [])}
    players = []
    for row in responses[1].get("players", [])[:100]:
        details = {**catalog, **{c["key"]: c for c in row.get("deckDetails", [])}}
        cards = api_cards(row.get("deckCards", []), details, row.get("deckPlayedCards"))
        players.append({**row, "tag": "#" + row["tag"].lstrip("#"), "eloRating": row.get("elo"),
                        "recent_deck": cards, "recent_deck_at": row.get("deckAsOf"),
                        "recent_deck_mode": row.get("deckMode"), "recent_deck_source": "battlelog",
                        "recent_deck_stale": bool(row.get("deckStale") or responses[1].get("source") == "snapshot")})
    return players, parse_competitive_payload(responses[2], catalog), responses[2].get("updatedAt"), responses[2].get("season")
