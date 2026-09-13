"""Pure, testable selection of API-provided single-deck battles."""
import re
import unicodedata
from datetime import datetime, timezone

def normalized_name(name):
    value = unicodedata.normalize("NFKC", str(name or "")).casefold().strip()
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())

def normalized_tag(tag):
    return str(tag or "").strip().lstrip("#").upper()

def deck_provenance(player):
    mode = {"pathoflegend": "Ranked", "pvp": "Ladder"}.get(player.get("recent_deck_mode"), "1v1")
    stamp = player.get("recent_deck_at", "")
    label = f"Battlelog · {stamp} · {mode}"
    if player.get("recent_deck_stale"):
        label += " · сохранённые данные"
    return label

def valid_deck(cards):
    return (isinstance(cards, list) and len(cards) == 8
            and all(isinstance(c, dict) and type(c.get("id")) is int
                    and 0 < c["id"] < 159000000 for c in cards)
            and len({c["id"] for c in cards}) == 8)

def extract_player_decks(battlelog, player_tag):
    wanted = normalized_tag(player_tag)
    extracted = []
    if not wanted or not isinstance(battlelog, list):
        return extracted
    for battle in battlelog:
        if not isinstance(battle, dict):
            continue
        team, opponent = battle.get("team") or [], battle.get("opponent") or []
        if len(team) != 1 or len(opponent) != 1 or not all(isinstance(p, dict) for p in team + opponent):
            continue
        mode = battle.get("gameMode") or ""
        mode = mode.get("name", "") if isinstance(mode, dict) else str(mode)
        kind = (str(battle.get("type", "")) + " " + mode).lower()
        if re.search(r"duel|boat|2v2|triple|touchdown|mega.?deck", kind):
            continue
        participants = team + opponent
        matches = [i for i, p in enumerate(participants) if normalized_tag(p.get("tag")) == wanted]
        if len(matches) != 1:
            continue
        chosen, enemy = participants[matches[0]], participants[1 - matches[0]]
        cards = chosen.get("cards")
        if not valid_deck(cards):
            continue
        try:
            at = str(battle.get("battleTime", ""))
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                continue
            mine, theirs = chosen["crowns"], enemy["crowns"]
            if type(mine) is not int or type(theirs) is not int:
                continue
        except (ValueError, KeyError, TypeError):
            continue
        ranked = bool(re.search(r"ranked|path.?of.?legend", kind))
        ladder = bool(re.search(r"\bpvp\b|ladder", kind))
        extracted.append({"cards": cards, "result": "win" if mine > theirs else "loss" if mine < theirs else "draw",
                          "battle_type": "pathoflegend" if ranked else "pvp" if ladder else "1v1",
                          "battle_time": at, "battle_at": parsed.astimezone(timezone.utc).isoformat(),
                          "battle_key": "|".join([at, kind, *sorted(normalized_tag(p.get("tag")) for p in participants)]),
                          "_priority": 0 if ranked else 1 if ladder else 2, "_timestamp": parsed.timestamp()})
    extracted.sort(key=lambda row: (row["_priority"], -row["_timestamp"]))
    return extracted
