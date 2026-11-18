"""HTTP handlers for Save Vault (ADR 0107)."""

from __future__ import annotations

from urllib.parse import parse_qs

from api_errors import BadRequest, GameNotFound, VaultNoSaves
from openbox import DATA
from pkg.parity.parity_save_vault import (
    DEFAULT_KEEP,
    create_snapshot,
    delete_snapshot,
    diff_snapshot,
    list_snapshots,
    resolve_game_save_roots,
    restore_snapshot,
    vault_status,
)
from routes.registry import route
from webapp_state import JOB_MANAGER, game_from_payload, game_from_query, load_state_view


def _require_save_management(state, *, read: bool = False):
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    if settings.get("save_management_enabled", True) is False:
        if read:
            return False
        raise BadRequest("Save management is disabled.")
    return True


def _game_from_query_or_404(state, query):
    try:
        return game_from_query(state, query)
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise GameNotFound("Game not found.") from error


def _game_from_payload_or_404(state, payload):
    try:
        return game_from_payload(state, payload)
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise GameNotFound("Game not found.") from error


@route("GET", "/api/v2/saves/vault", spec="handlers.savevault.vault_list")
def vault_list(handler, parsed):
    state = load_state_view()
    if not _require_save_management(state, read=True):
        handler.send_json(200, {"enabled": False, "snapshots": []})
        return
    query = parse_qs(parsed.query or "")
    game_id = ""
    if query.get("id") or query.get("game_id"):
        game = _game_from_query_or_404(state, query)
        game_id = str(game.get("game_id") or "")
    snapshots = list_snapshots(DATA.parent, game_id=game_id)
    status = vault_status(DATA.parent, game_id=game_id, settings=state.get("settings"))
    handler.send_json(200, {"enabled": True, "snapshots": snapshots, "status": status})


@route("GET", "/api/v2/saves/vault/status", spec="handlers.savevault.vault_get_status")
def vault_get_status(handler, parsed):
    state = load_state_view()
    if not _require_save_management(state, read=True):
        handler.send_json(200, {"enabled": False, "snapshots": 0, "bytes": 0, "games": 0})
        return
    query = parse_qs(parsed.query or "")
    game_id = ""
    if query.get("id") or query.get("game_id"):
        game = _game_from_query_or_404(state, query)
        game_id = str(game.get("game_id") or "")
    status = vault_status(DATA.parent, game_id=game_id, settings=state.get("settings"))
    status["enabled"] = True
    handler.send_json(200, status)


@route("POST", "/api/v2/saves/vault/snapshot", spec="handlers.savevault.vault_snapshot")
def vault_snapshot(handler, payload):
    state = load_state_view()
    _require_save_management(state)
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    game = dict(_game_from_payload_or_404(state, payload))
    game_id = str(game.get("game_id") or "")
    trigger = str(payload.get("trigger") or "manual").strip()
    if trigger not in {"manual", "session_close", "auto", "pre-restore"}:
        raise BadRequest("Unsupported Save Vault trigger.")
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    if trigger in ("session_close", "auto") and settings.get("save_vault_enabled", True) is False:
        raise BadRequest("Automatic Save Vault snapshots are disabled.")
    keep = settings.get("save_vault_keep", DEFAULT_KEEP)
    data_dir = DATA.parent
    # Validate paths & empty-save condition up front so NO_SAVES / path escapes fail fast.
    resolve_game_save_roots(game, include_shared=(trigger in ("manual", "pre-restore")))

    def worker(ctx):
        return create_snapshot(
            data_dir,
            game,
            trigger=trigger,
            include_shared=(trigger in ("manual", "pre-restore")),
            keep=keep,
            progress_cb=lambda current, total: ctx.progress(current=current, total=total),
        )

    job = JOB_MANAGER.submit(f"save-vault-snapshot:{game_id or 'game'}", worker)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"], "game_id": game_id})


@route("GET", "/api/v2/saves/vault/diff", spec="handlers.savevault.vault_diff")
def vault_diff(handler, parsed):
    state = load_state_view()
    _require_save_management(state)
    query = parse_qs(parsed.query or "")
    game = _game_from_query_or_404(state, query)
    snapshot_id = str((query.get("snapshot_id") or [""])[0]).strip()
    if not snapshot_id:
        raise BadRequest("snapshot_id is required.")
    handler.send_json(200, diff_snapshot(DATA.parent, game, snapshot_id))


@route("POST", "/api/v2/saves/vault/restore", spec="handlers.savevault.vault_restore")
def vault_restore(handler, payload):
    state = load_state_view()
    _require_save_management(state)
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    game = dict(_game_from_payload_or_404(state, payload))
    game_id = str(game.get("game_id") or "")
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if not snapshot_id:
        raise BadRequest("snapshot_id is required.")
    dry_run = bool(payload.get("dry_run", False))
    skip_safety = bool(payload.get("skip_safety", False))
    include_removals = bool(payload.get("include_removals", False))
    data_dir = DATA.parent

    if dry_run:
        handler.send_json(
            200,
            restore_snapshot(
                data_dir,
                game,
                snapshot_id,
                dry_run=True,
                skip_safety=skip_safety,
                include_removals=include_removals,
            ),
        )
        return

    def worker(ctx):
        return restore_snapshot(
            data_dir,
            game,
            snapshot_id,
            dry_run=False,
            skip_safety=skip_safety,
            include_removals=include_removals,
            progress_cb=lambda current, total: ctx.progress(current=current, total=total),
        )

    job = JOB_MANAGER.submit(f"save-vault-restore:{game_id or 'game'}", worker)
    handler.send_json(202, {"state": "queued", "job_id": job["job_id"], "snapshot_id": snapshot_id})


@route("POST", "/api/v2/saves/vault/delete", spec="handlers.savevault.vault_delete")
def vault_delete(handler, payload):
    state = load_state_view()
    _require_save_management(state)
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    snapshot_id = str(payload.get("snapshot_id") or "").strip()
    if not snapshot_id:
        raise BadRequest("snapshot_id is required.")
    game = None
    if payload.get("id") is not None or payload.get("game_id"):
        game = _game_from_payload_or_404(state, payload)
    handler.send_json(200, delete_snapshot(DATA.parent, game, snapshot_id))


__all__ = [
    "VaultNoSaves",
    "vault_delete",
    "vault_diff",
    "vault_get_status",
    "vault_list",
    "vault_restore",
    "vault_snapshot",
]
