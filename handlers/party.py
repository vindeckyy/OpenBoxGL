"""PartyHandlers — Game Night party queue and deck builder (Big Box party mode)."""
from __future__ import annotations

from api_errors import BadRequest
from openbox import load_state
from pkg.parity.parity_library_sync import SyncFolderError, SyncValidationError
from pkg.parity.parity_party import (
    THEME_PRESETS,
    build_deck,
    build_party_queue,
    clean_seed,
    deck_queue,
    delete_deck,
    empty_queue_reason,
    find_deck,
    import_decks,
    list_decks,
    preset_empty_reason,
    queue_exclusion_breakdown,
    read_shared_decks,
    save_deck,
    write_shared_deck,
)
from routes.registry import route
from webapp_state import transact_state


def _queue_from_settings(settings: dict) -> tuple[list[str], int, int]:
    """Return (queue, players, index) cleaned defensively for serving."""
    from handlers.settings import _clean_party

    try:
        queue, players, index = _clean_party(settings)
    except ValueError:
        return [], 2, 0
    return queue, players, index


def _party_request(payload):
    if not isinstance(payload, dict):
        raise BadRequest("body must be an object", code="PARTY_INVALID_REQUEST")
    return payload


def _party_int(payload, key, *, default, minimum, maximum):
    raw = payload.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise BadRequest(f"{key} must be an integer", code="PARTY_INVALID_REQUEST") from error
    if not minimum <= value <= maximum:
        raise BadRequest(f"{key} must be between {minimum} and {maximum}", code="PARTY_INVALID_FIELD")
    return value


def _party_preset(payload):
    preset = str(payload.get("preset") or "").strip()
    if preset and preset not in THEME_PRESETS:
        raise BadRequest("party preset is unsupported", code="PARTY_INVALID_PRESET")
    return preset


def _party_folder(state) -> str:
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    folder = settings.get("cloud_folder") if isinstance(settings, dict) else None
    if not isinstance(folder, str) or not folder.strip():
        raise BadRequest("Configure a mounted cloud sync folder first.", code="PARTY_SHARE_FOLDER_REQUIRED")
    return folder.strip()


class PartyHandlers:
    @route("POST", "/api/v2/party/queue")
    def _api_post_api_v2_party_queue(self, payload):
        # Dispatch passes the already-parsed JSON body (web_app._do_POST reads
        # rfile exactly once); never call self.body() here — re-reading the
        # exhausted stream blocks until the socket times out.
        body = _party_request(payload)
        players = _party_int(body, "players", default=2, minimum=2, maximum=8)
        minutes = _party_int(body, "minutes", default=0, minimum=0, maximum=24 * 60)
        preset = _party_preset(body)
        seed = str(body.get("seed") or "").strip() or None
        if seed is not None:
            try:
                seed = clean_seed(seed)
            except SyncValidationError as error:
                raise BadRequest(str(error), code="PARTY_INVALID_FIELD") from error

        state = load_state()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        try:
            queue = build_party_queue(games, players=players, minutes=minutes, seed=seed, preset=preset)
        except SyncValidationError as error:
            raise BadRequest(str(error), code="PARTY_INVALID_PRESET") from error

        def mutate(live):
            settings = live.setdefault("settings", {})
            settings["party_queue"] = queue
            settings["party_players"] = players
            settings["party_index"] = 0

        transact_state(mutate)
        breakdown = queue_exclusion_breakdown(games, players=players, minutes=minutes)
        self.send_json(200, {
            "queue": queue,
            "count": len(queue),
            "players": players,
            "minutes": minutes,
            "preset": preset or None,
            "seed": seed,
            "empty_reason": (preset_empty_reason(preset) or empty_queue_reason(breakdown, players)) if not queue else None,
            "excluded": breakdown,
        })
        return

    @route("GET", "/api/v2/party/queue")
    def _api_get_api_v2_party_queue(self, parsed):
        state = load_state()
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        queue, _players, index = _queue_from_settings(settings if isinstance(settings, dict) else {})
        self.send_json(200, {"queue": queue, "index": index if queue else 0})
        return

    @route("POST", "/api/v2/party/next")
    def _api_post_api_v2_party_next(self, payload):
        state = load_state()
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        queue, _players, index = _queue_from_settings(settings if isinstance(settings, dict) else {})
        if not queue:
            raise BadRequest("party queue is empty — build one first")
        index = (index + 1) % len(queue)
        game_id = queue[index]

        games = state.get("games", []) if isinstance(state, dict) else []
        name = game_id
        for game in games:
            if not isinstance(game, dict):
                continue
            if str(game.get("game_id") or "") == game_id or str(game.get("id") or "") == game_id:
                name = str(game.get("name") or game_id)
                break

        def mutate(live):
            live.setdefault("settings", {})["party_index"] = index

        transact_state(mutate)
        self.send_json(200, {"game_id": game_id, "name": name, "index": index})
        return

    @route("GET", "/api/v2/party/decks")
    def _api_get_api_v2_party_decks(self, parsed):
        state = load_state()
        decks = list_decks(state)
        self.send_json(200, {
            "format": 1,
            "decks": decks,
            "count": len(decks),
            "presets": [
                {"id": preset_id, "label_key": rules["label_key"]}
                for preset_id, rules in THEME_PRESETS.items()
            ],
        })
        return

    @route("POST", "/api/v2/party/decks")
    def _api_post_api_v2_party_decks(self, payload):
        body = _party_request(payload)
        name = str(body.get("name") or "").strip()
        if not name:
            raise BadRequest("name is required", code="PARTY_INVALID_FIELD")
        players = _party_int(body, "players", default=2, minimum=2, maximum=8)
        minutes = _party_int(body, "minutes", default=0, minimum=0, maximum=24 * 60)
        preset = _party_preset(body)
        seed = str(body.get("seed") or "").strip() or None
        game_ids = body.get("game_ids")
        if game_ids is not None and not isinstance(game_ids, list):
            raise BadRequest("game_ids must be a list", code="PARTY_INVALID_FIELD")
        state = load_state()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        try:
            deck = build_deck(
                games, name=name, players=players, minutes=minutes,
                preset=preset, seed=seed, game_ids=game_ids,
            )
        except SyncValidationError as error:
            raise BadRequest(str(error), code="PARTY_DECK_INVALID") from error

        def mutate(live):
            save_deck(live, deck)

        transact_state(mutate)
        self.send_json(200, {"deck": deck, "count": len(list_decks(load_state()))})
        return

    @route("POST", "/api/v2/party/decks/load")
    def _api_post_api_v2_party_decks_load(self, payload):
        body = _party_request(payload)
        deck_id = str(body.get("deck_id") or "").strip()
        if not deck_id:
            raise BadRequest("deck_id is required", code="PARTY_INVALID_FIELD")
        state = load_state()
        deck = find_deck(state, deck_id)
        if deck is None:
            raise BadRequest("party deck not found", code="PARTY_DECK_NOT_FOUND")
        queue = deck_queue(deck)

        def mutate(live):
            settings = live.setdefault("settings", {})
            settings["party_queue"] = queue
            settings["party_players"] = deck["players"]
            settings["party_index"] = 0

        transact_state(mutate)
        self.send_json(200, {"deck": deck, "queue": queue, "index": 0, "count": len(queue)})
        return

    @route("POST", "/api/v2/party/decks/delete")
    def _api_post_api_v2_party_decks_delete(self, payload):
        body = _party_request(payload)
        deck_id = str(body.get("deck_id") or "").strip()
        if not deck_id:
            raise BadRequest("deck_id is required", code="PARTY_INVALID_FIELD")

        def mutate(live):
            return delete_deck(live, deck_id)

        deleted = transact_state(mutate)[1]
        self.send_json(200, {"deleted": bool(deleted), "count": len(list_decks(load_state()))})
        return

    @route("POST", "/api/v2/party/decks/share")
    def _api_post_api_v2_party_decks_share(self, payload):
        body = _party_request(payload)
        deck_id = str(body.get("deck_id") or "").strip()
        if not deck_id:
            raise BadRequest("deck_id is required", code="PARTY_INVALID_FIELD")
        state = load_state()
        deck = find_deck(state, deck_id)
        if deck is None:
            raise BadRequest("party deck not found", code="PARTY_DECK_NOT_FOUND")
        folder = _party_folder(state)
        try:
            target = write_shared_deck(folder, deck)
        except (SyncFolderError, OSError, ValueError) as error:
            raise BadRequest("Party deck share folder is invalid or unavailable.", code="PARTY_SHARE_FAILED") from error

        def mutate(live):
            stored = find_deck(live, deck_id)
            if stored is not None:
                stored["shared"] = True
                save_deck(live, stored)

        transact_state(mutate)
        self.send_json(200, {"shared": True, "deck_id": deck["deck_id"], "signature": target.name[:-5]})
        return

    @route("POST", "/api/v2/party/decks/import")
    def _api_post_api_v2_party_decks_import(self, payload):
        _party_request(payload)
        state = load_state()
        folder = _party_folder(state)
        try:
            shared = read_shared_decks(folder)
        except (SyncFolderError, OSError, ValueError) as error:
            raise BadRequest("Party deck share folder is invalid or unavailable.", code="PARTY_SHARE_FAILED") from error

        def mutate(live):
            return import_decks(live, shared)

        applied = transact_state(mutate)[1]
        self.send_json(200, {"imported": applied, "available": len(shared), "count": len(list_decks(load_state()))})
        return
