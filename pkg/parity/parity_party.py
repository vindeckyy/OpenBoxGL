"""parity_party.py — Game Night party queue builder and deck storage.

No runtime deps. Pure queue functions plus the persisted deck shape used by
Game Night.  Decks are content-addressed so a shared deck reproduces its order
from the stored seed and game ids alone.  The shared-folder transport reuses
the Household namespace and lock, so a deck never leaves the device unless the
user explicitly shares it.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend_io import atomic_write_text
from pkg.parity.parity_library_sync import SyncFolderError, SyncValidationError, _canonical
from pkg.parity.parity_household import _household_transport_lock, transport_subdir

# ponytail: static couch-platform set. Consoles, handhelds, and arcade cabinets
# are couch-multiplayer by default; computer platforms qualify only through
# explicit per-game controller_support. Upgrade path: derive from a
# user-editable platform taxonomy if one ever exists.
COUCH_PLATFORMS = frozenset({
    # Nintendo
    "NES", "SNES", "Nintendo 64", "GameCube", "Wii", "Wii U", "Switch",
    "Nintendo Switch", "Game Boy", "Game Boy Color", "Game Boy Advance",
    "Nintendo DS", "Nintendo 3DS",
    # Sega
    "Sega Genesis", "Sega Mega Drive", "Sega Master System", "Sega Game Gear",
    "Sega Saturn", "Sega Dreamcast", "Sega CD",
    # Sony
    "PlayStation", "PlayStation 2", "PlayStation 3", "PlayStation 4",
    "PlayStation 5", "PSP", "PlayStation Vita",
    # Microsoft
    "Xbox", "Xbox 360", "Xbox One", "Xbox Series X", "Xbox Series S",
    # Arcade
    "Arcade", "MAME", "Neo Geo", "Neo Geo AES",
    # Atari / NEC
    "Atari 2600", "Atari 5200", "Atari 7800", "Atari Jaguar", "Atari Lynx",
    "Turbografx-16", "TurboGrafx-16", "PC Engine",
})

PARTY_QUEUE_LIMIT = 50


def _max_players(game: dict[str, Any]) -> int:
    try:
        return max(1, int(game.get("max_players") or 1))
    except (TypeError, ValueError):
        return 1


def _path_usable(game: dict[str, Any]) -> bool:
    # Same default as the picker: only a known-missing path without a store
    # install disqualifies; unknown counts as usable.
    if game.get("path_exists") is False and not game.get("store_installed"):
        return False
    return True


def _avg_session_seconds(game: dict[str, Any]) -> float | None:
    try:
        plays = int(game.get("play_count") or 0)
        total = float(game.get("playtime_seconds") or 0)
    except (TypeError, ValueError):
        return None
    if plays > 0 and total > 0:
        return total / plays
    return None


def eligible_party_games(games: list[dict[str, Any]], *, players: int = 2) -> list[dict[str, Any]]:
    """Games suitable for a couch session with `players` participants."""
    eligible = []
    for game in games:
        if not isinstance(game, dict):
            continue
        if game.get("hidden") or game.get("hide_in_bigbox"):
            continue
        if not _path_usable(game):
            continue
        if _max_players(game) < players:
            continue
        platform = str(game.get("platform") or "").strip()
        has_controller = bool(str(game.get("controller_support") or "").strip())
        if not has_controller and platform not in COUCH_PLATFORMS:
            continue
        eligible.append(game)
    return eligible


def queue_exclusion_breakdown(
    games: list[dict[str, Any]],
    *,
    players: int = 2,
    minutes: int = 0,
) -> dict[str, int]:
    """Count why games were excluded from a party queue (same rules as build).

    Keys: total, hidden, unusable_path, too_few_players,
    no_controller_or_platform, over_budget. Used to explain empty queues.
    """
    try:
        players = max(2, min(8, int(players)))
    except (TypeError, ValueError):
        players = 2
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    counts = {
        "total": 0, "hidden": 0, "unusable_path": 0, "too_few_players": 0,
        "no_controller_or_platform": 0, "over_budget": 0,
    }
    budget = minutes * 60 * 3 if minutes > 0 else None
    for game in games or []:
        if not isinstance(game, dict):
            continue
        counts["total"] += 1
        if game.get("hidden") or game.get("hide_in_bigbox"):
            counts["hidden"] += 1
            continue
        if not _path_usable(game):
            counts["unusable_path"] += 1
            continue
        if _max_players(game) < players:
            counts["too_few_players"] += 1
            continue
        platform = str(game.get("platform") or "").strip()
        if not str(game.get("controller_support") or "").strip() and platform not in COUCH_PLATFORMS:
            counts["no_controller_or_platform"] += 1
            continue
        if budget is not None:
            avg = _avg_session_seconds(game)
            if avg is not None and avg > budget:
                counts["over_budget"] += 1
    return counts


def empty_queue_reason(breakdown: dict[str, int], players: int = 2) -> str:
    """Human-readable explanation for an empty party queue."""
    total = breakdown.get("total", 0)
    if not total:
        return "Your library is empty — import games to build a party queue."
    ranked = sorted(
        (("too_few_players", f"No games support {players} players — lower the player count or add Max players metadata."),
         ("no_controller_or_platform", "No couch-ready games found — add controller support or console platforms."),
         ("unusable_path", "Every candidate has missing game files — fix paths or reinstall."),
         ("over_budget", "Every candidate runs longer than the session budget — raise the minutes."),
         ("hidden", "Every game in the library is hidden.")),
        key=lambda item: breakdown.get(item[0], 0),
        reverse=True,
    )
    for key, message in ranked:
        if breakdown.get(key, 0):
            return message
    return "No eligible games for this setup."


def _rating_of(game: dict[str, Any]) -> float:
    try:
        return float(game.get("rating") or 0)
    except (TypeError, ValueError):
        return 0.0


def _game_id_of(game: dict[str, Any]) -> str:
    return str(game.get("game_id") or game.get("id") or "")


def _select_ids(candidates: list[dict[str, Any]], limit: int, *, deterministic: bool) -> list[str]:
    """Rate-descending selection; the tiebreak is random or the stable game id."""
    rng = None if deterministic else random.Random()
    scored = []
    for game in candidates:
        game_id = _game_id_of(game)
        tie = game_id if deterministic else rng.random()
        scored.append((-_rating_of(game), tie, game_id))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [game_id for _, _, game_id in scored[:limit] if game_id]


def build_party_queue(
    games: list[dict[str, Any]],
    *,
    players: int = 2,
    minutes: int = 0,
    limit: int = PARTY_QUEUE_LIMIT,
    seed: str | None = None,
    preset: str | None = None,
) -> list[str]:
    """Build a party queue of game ids, best first.

    Filters to couch-eligible games, excludes titles whose typical session
    runs longer than 3x the session budget (same factor as the picker), sorts
    rating desc with a random tiebreak, and caps at `limit`.  An optional
    ``preset`` applies one of :data:`THEME_PRESETS`.  With a ``seed`` the
    result is a deterministic shuffle of the same rating-ranked selection, so
    a shared deck reproduces its order from the seed and game ids alone.
    """
    try:
        players = int(players)
    except (TypeError, ValueError):
        players = 2
    players = max(2, min(8, players))
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    try:
        limit = int(limit or PARTY_QUEUE_LIMIT)
    except (TypeError, ValueError):
        limit = PARTY_QUEUE_LIMIT
    limit = max(1, min(PARTY_QUEUE_LIMIT, limit))

    candidates = eligible_party_games(games, players=players)
    if minutes > 0:
        budget = minutes * 60 * 3
        kept = []
        for game in candidates:
            avg = _avg_session_seconds(game)
            if avg is None or avg <= budget:
                kept.append(game)
        candidates = kept
    candidates = apply_theme_preset(candidates, preset)

    selected = _select_ids(candidates, limit, deterministic=seed is not None)
    if seed is None:
        return selected
    return seeded_shuffle(selected, seed)


# ---------------------------------------------------------------------------
# Theme presets
# ---------------------------------------------------------------------------

THEME_PRESETS: dict[str, dict[str, Any]] = {
    "nineties_racers": {
        "label_key": "party.preset_90s_racers",
        "min_year": 1990,
        "max_year": 1999,
        "keywords": (
            "racing", "race", "racer", "kart", "grand prix", "driving",
            "motocross", "rally", "stock car", "formula",
        ),
    },
    "coop_only": {
        "label_key": "party.preset_coop",
        "min_players": 2,
        "coop_keywords": (
            "co-op", "coop", "co-operative", "cooperative", "team", "together",
        ),
    },
    "eight_plus": {
        "label_key": "party.preset_eight_plus",
        "min_players": 8,
    },
}

PRESET_EMPTY_REASON_KEY = "party.preset_empty"


def theme_preset_ids() -> list[str]:
    """Return the shipped preset ids in a stable order."""
    return list(THEME_PRESETS)


def _year_of(game: dict[str, Any]) -> int | None:
    raw = str(game.get("year") or "").strip()
    match = re.search(r"(19|20)\d{2}", raw)
    if match is None:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _coop_evidence(game: dict[str, Any]) -> bool:
    if str(game.get("controller_support") or "").strip():
        return True
    haystack = " ".join(str(game.get(field) or "") for field in ("genre", "tags", "description")).casefold()
    return any(keyword in haystack for keyword in THEME_PRESETS["coop_only"]["coop_keywords"])


def apply_theme_preset(games: list[dict[str, Any]], preset: str | None) -> list[dict[str, Any]]:
    """Filter a candidate list by a shipped theme preset (unknown presets raise)."""
    if preset in (None, ""):
        return list(games or [])
    key = str(preset).strip()
    rules = THEME_PRESETS.get(key)
    if rules is None:
        raise SyncValidationError(f"Unknown party preset: {preset!r}")
    result = []
    for game in games or []:
        if not isinstance(game, dict):
            continue
        if "min_players" in rules and _max_players(game) < rules["min_players"]:
            continue
        if "min_year" in rules:
            year = _year_of(game)
            if year is None or not rules["min_year"] <= year <= rules["max_year"]:
                continue
            haystack = f"{game.get('name') or ''} {game.get('genre') or ''} {game.get('tags') or ''}".casefold()
            if not any(keyword in haystack for keyword in rules["keywords"]):
                continue
        if rules.get("coop_keywords") and not _coop_evidence(game):
            continue
        result.append(game)
    return result


def preset_empty_reason(preset: str | None) -> str | None:
    """Return the honest empty-queue reason when a preset filtered everything."""
    if preset in (None, ""):
        return None
    return PRESET_EMPTY_REASON_KEY if str(preset) in THEME_PRESETS else None


# ---------------------------------------------------------------------------
# Deterministic seeded order
# ---------------------------------------------------------------------------

DECK_FORMAT = 1
DECK_DIRECTORY = "party-decks"
PARTY_DECKS_KEY = "party_decks"
MAX_DECKS = 50
MAX_SHARED_DECKS = 200
MAX_DECK_NAME = 80
MAX_DECK_BYTES = 128 * 1024
SEED_RE = re.compile(r"^[0-9a-f]{8,64}$")
SEED_SALT = "openbox-party-v1"
_REQUIRED_DECK_FIELDS = frozenset({
    "format", "deck_id", "name", "players", "minutes", "preset", "seed",
    "game_ids", "created_at", "signature",
})
_OPTIONAL_DECK_FIELDS = frozenset({"origin", "shared"})


def new_deck_seed() -> str:
    """Return a 32-hex seed that both devices can store and replay."""
    return uuid.uuid4().hex


def seeded_shuffle(game_ids: list[str], seed: str) -> list[str]:
    """Shuffle deterministically so the same seed and ids reproduce one order."""
    cleaned = clean_seed(seed)
    items = [str(item) for item in (game_ids or []) if str(item)]
    rng = random.Random(f"{SEED_SALT}:{cleaned}")
    rng.shuffle(items)
    return items


def clean_seed(seed: Any) -> str:
    value = str(seed or "").strip().casefold()
    if not SEED_RE.fullmatch(value):
        raise SyncValidationError("Party deck seed is invalid.")
    return value


def deck_signature(deck: dict[str, Any]) -> str:
    """Content hash over the shareable deck fields (order included, flags not)."""
    body = {
        field: deck.get(field)
        for field in ("format", "deck_id", "name", "players", "minutes", "preset", "seed", "game_ids", "created_at")
    }
    return hashlib.sha256(_canonical(body)).hexdigest()


def _clean_deck_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        raise SyncValidationError("Party deck name is required.")
    if len(text) > MAX_DECK_NAME or any(ord(char) < 32 for char in text):
        raise SyncValidationError("Party deck name is invalid.")
    return text


def validate_deck(deck: Any) -> dict[str, Any]:
    """Validate and return a detached, self-consistent deck copy."""
    if not isinstance(deck, dict):
        raise SyncValidationError("Party deck must be an object.")
    if set(deck) - (_REQUIRED_DECK_FIELDS | _OPTIONAL_DECK_FIELDS):
        raise SyncValidationError("Party deck shape is invalid.")
    if not _REQUIRED_DECK_FIELDS <= set(deck):
        raise SyncValidationError("Party deck has missing fields.")
    if deck.get("format") != DECK_FORMAT:
        raise SyncValidationError("Unsupported party deck format.")
    deck_id = str(deck.get("deck_id") or "")
    if not re.fullmatch(r"[0-9a-f]{16,64}", deck_id):
        raise SyncValidationError("Party deck ID is invalid.")
    try:
        players = int(deck.get("players"))
        minutes = int(deck.get("minutes") or 0)
    except (TypeError, ValueError) as error:
        raise SyncValidationError("Party deck players/minutes are invalid.") from error
    if not 2 <= players <= 8 or minutes < 0 or minutes > 24 * 60:
        raise SyncValidationError("Party deck players/minutes are out of range.")
    preset = str(deck.get("preset") or "")
    if preset and preset not in THEME_PRESETS:
        raise SyncValidationError("Party deck preset is unsupported.")
    seed = clean_seed(deck.get("seed"))
    raw_ids = deck.get("game_ids")
    if not isinstance(raw_ids, list) or len(raw_ids) > PARTY_QUEUE_LIMIT:
        raise SyncValidationError("Party deck game list is invalid.")
    game_ids = []
    for item in raw_ids:
        value = str(item or "").strip()
        if not value or len(value) > 128 or any(ord(char) < 32 for char in value) or value in game_ids:
            raise SyncValidationError("Party deck game list is invalid.")
        game_ids.append(value)
    created_at = str(deck.get("created_at") or "")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise SyncValidationError("Party deck created_at is invalid.") from error
    if parsed.tzinfo is None:
        raise SyncValidationError("Party deck created_at is invalid.")
    checked = {
        "format": DECK_FORMAT,
        "deck_id": deck_id,
        "name": _clean_deck_name(deck.get("name")),
        "players": players,
        "minutes": minutes,
        "preset": preset,
        "seed": seed,
        "game_ids": game_ids,
        "created_at": created_at,
    }
    signature = str(deck.get("signature") or "")
    expected = deck_signature(checked)
    if signature and signature != expected:
        raise SyncValidationError("Party deck signature does not match its contents.")
    checked["signature"] = expected
    if deck.get("origin") in ("imported", "local"):
        checked["origin"] = deck["origin"]
    if deck.get("shared") is True:
        checked["shared"] = True
    if len(_canonical(checked)) > MAX_DECK_BYTES:
        raise SyncValidationError("Party deck is too large.")
    return checked


def list_decks(state: Any) -> list[dict[str, Any]]:
    """Return valid saved decks newest first; corrupt rows are skipped honestly."""
    bucket = state.get(PARTY_DECKS_KEY) if isinstance(state, dict) else None
    raw = bucket.get("decks") if isinstance(bucket, dict) else None
    if not isinstance(raw, list):
        return []
    decks = []
    for item in raw[:MAX_DECKS * 2]:
        try:
            decks.append(validate_deck(item))
        except SyncValidationError:
            continue
    decks.sort(key=lambda deck: (deck["created_at"], deck["deck_id"]), reverse=True)
    return decks[:MAX_DECKS]


def find_deck(state: Any, deck_id: str) -> dict[str, Any] | None:
    wanted = str(deck_id or "").strip()
    for deck in list_decks(state):
        if deck["deck_id"] == wanted or deck["name"].casefold() == wanted.casefold():
            return deck
    return None


def save_deck(state: dict[str, Any], deck: Any) -> dict[str, Any]:
    """Upsert one validated deck into ``state["party_decks"]`` and return it."""
    checked = validate_deck(deck)
    decks = list_decks(state)
    replaced = False
    for index, item in enumerate(decks):
        if item["deck_id"] == checked["deck_id"]:
            decks[index] = checked
            replaced = True
            break
        if item["name"].casefold() == checked["name"].casefold():
            decks[index] = checked
            replaced = True
            break
    if not replaced:
        if len(decks) >= MAX_DECKS:
            raise SyncValidationError("Party deck limit reached.")
        decks.append(checked)
    state[PARTY_DECKS_KEY] = {"format": DECK_FORMAT, "decks": decks}
    return checked


def delete_deck(state: dict[str, Any], deck_id: str) -> bool:
    wanted = str(deck_id or "").strip()
    decks = [deck for deck in list_decks(state) if deck["deck_id"] != wanted]
    if len(decks) == len(list_decks(state)):
        return False
    state[PARTY_DECKS_KEY] = {"format": DECK_FORMAT, "decks": decks}
    return True


def build_deck(
    games: list[dict[str, Any]],
    *,
    name: str,
    players: int = 2,
    minutes: int = 0,
    preset: str | None = None,
    seed: str | None = None,
    game_ids: list[str] | None = None,
    limit: int = PARTY_QUEUE_LIMIT,
) -> dict[str, Any]:
    """Build a named deck: deterministic base ranking plus a replayable seed."""
    try:
        players = max(2, min(8, int(players)))
    except (TypeError, ValueError):
        players = 2
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes < 0:
        minutes = 0
    candidates = eligible_party_games(games, players=players)
    if minutes > 0:
        budget = minutes * 60 * 3
        candidates = [
            game for game in candidates
            if (avg := _avg_session_seconds(game)) is None or avg <= budget
        ]
    candidates = apply_theme_preset(candidates, preset)
    if game_ids is None:
        selected = _select_ids(candidates, max(1, min(PARTY_QUEUE_LIMIT, int(limit))), deterministic=True)
    else:
        known = {_game_id_of(game) for game in candidates}
        selected = [str(item) for item in game_ids if str(item)]
        if any(item not in known for item in selected):
            raise SyncValidationError("Party deck game list is not eligible for this setup.")
    deck = {
        "format": DECK_FORMAT,
        "deck_id": uuid.uuid4().hex[:16],
        "name": name,
        "players": players,
        "minutes": minutes,
        "preset": str(preset or ""),
        "seed": clean_seed(seed) if seed else new_deck_seed(),
        "game_ids": selected,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    deck["signature"] = deck_signature(deck)
    return validate_deck(deck)


def deck_queue(deck: dict[str, Any]) -> list[str]:
    """Return the deck's playback order; the seed alone reproduces it."""
    checked = validate_deck(deck)
    return seeded_shuffle(checked["game_ids"], checked["seed"])


def _decks_dir(root: Path, *, create: bool) -> Path:
    return transport_subdir(root, DECK_DIRECTORY, create=create)


def write_shared_deck(folder: str | Path, deck: Any) -> Path:
    """Publish one content-addressed deck into the shared Household folder."""
    checked = validate_deck(deck)
    shared = {key: value for key, value in checked.items() if key != "shared"}
    shared["shared"] = True
    with _household_transport_lock(folder, create=True) as root:
        decks = _decks_dir(root, create=True)
        target = decks / f"{deck_signature(checked)}.json"
        if target.exists():
            try:
                if json.loads(target.read_bytes().decode("utf-8")).get("signature") == checked["signature"]:
                    return target
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            raise SyncValidationError("Shared party deck differs from the existing file.")
        encoded = _canonical(shared).decode("utf-8") + "\n"
        try:
            atomic_write_text(target, encoded, mode=0o600)
            target.chmod(0o600)
        except OSError as error:
            raise SyncFolderError("Unable to publish the party deck.") from error
        return target


def read_shared_decks(folder: str | Path) -> list[dict[str, Any]]:
    """Read validated shared decks; the file name must match the content hash."""
    try:
        with _household_transport_lock(folder) as root:
            decks = _decks_dir(root, create=False)
            if not decks.exists() or decks.is_symlink() or not decks.is_dir():
                return []
            paths = sorted(path for path in decks.glob("*.json") if not path.is_symlink())
            if len(paths) > MAX_SHARED_DECKS:
                raise SyncValidationError("Too many shared party decks.")
            shared = []
            for path in paths:
                if not re.fullmatch(r"[0-9a-f]{64}", path.stem) or not path.is_file():
                    continue
                try:
                    if path.stat().st_size > MAX_DECK_BYTES:
                        continue
                    raw = path.read_bytes()
                    if len(raw) > MAX_DECK_BYTES:
                        continue
                    deck = validate_deck(json.loads(raw.decode("utf-8")))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, SyncValidationError):
                    continue
                if deck["signature"] != path.stem:
                    continue
                deck["origin"] = "imported"
                deck["shared"] = True
                shared.append(deck)
    except SyncFolderError:
        raise
    shared.sort(key=lambda deck: (deck["name"].casefold(), deck["deck_id"]))
    return shared


def import_decks(state: dict[str, Any], decks: list[dict[str, Any]]) -> int:
    """Merge shared decks into local state; existing ids and names are left alone."""
    existing = list_decks(state)
    local_ids = {deck["deck_id"] for deck in existing}
    local_names = {deck["name"].casefold() for deck in existing}
    applied = 0
    for raw in decks or []:
        try:
            deck = validate_deck(raw)
        except SyncValidationError:
            continue
        if deck["deck_id"] in local_ids or deck["name"].casefold() in local_names:
            continue
        deck["origin"] = "imported"
        save_deck(state, deck)
        local_ids.add(deck["deck_id"])
        local_names.add(deck["name"].casefold())
        applied += 1
    return applied
