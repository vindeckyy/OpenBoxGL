"""One-click Flatpak folder access for a game's emulator (1.16.1).

Launch Readiness reports FLATPAK_FS_DENIED with the exact `flatpak override`
command. This module runs that command for the user, with three guards: the
Flatpak id must be one a bundled definition declares, the folder must be an
existing directory inside the user's home (never the home itself), and the grant
is read-only. Undo removes the same grant.

The caller confirms the command in the UI first; this module does not prompt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from api_errors import BadRequest
from pkg.parity.parity_emulator_defs import _registry

GRANT_TIMEOUT_SECONDS = 30


def known_flatpak_ids() -> set[str]:
    return {str(adapter.get("flatpak_app_id")) for adapter in _registry()["adapters"] if adapter.get("flatpak_app_id")}


def validate_grant(app_id, folder) -> tuple[str, str]:
    """Return the (app id, resolved folder) a grant may touch, or raise BadRequest."""
    app = str(app_id or "").strip()
    if app not in known_flatpak_ids():
        raise BadRequest("That Flatpak app is not one OpenBox has a definition for.")
    text = str(folder or "").strip()
    if not text or not Path(text).is_absolute():
        raise BadRequest("The folder must be an absolute path.")
    resolved = Path(text).resolve()
    home = Path.home().resolve()
    if resolved == home or home not in resolved.parents:
        raise BadRequest("Only a folder inside your home directory can be granted.")
    if not resolved.is_dir():
        raise BadRequest("That folder does not exist.")
    return app, str(resolved)


def grant_command(app: str, folder: str, *, undo: bool = False) -> list[str]:
    if undo:
        return ["flatpak", "override", "--user", f"--nofilesystem={folder}", app]
    return ["flatpak", "override", "--user", f"--filesystem={folder}:ro", app]


def apply_grant(app_id, folder, *, undo: bool = False, run=subprocess.run) -> dict:
    app, path = validate_grant(app_id, folder)
    command = grant_command(app, path, undo=undo)
    try:
        result = run(command, capture_output=True, text=True, timeout=GRANT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BadRequest(f"flatpak did not finish the change: {error}") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BadRequest(f"flatpak refused the change: {detail[:300] or 'no detail given'}")
    return {
        "applied": True,
        "undone": undo,
        "app_id": app,
        "path": path,
        "command": " ".join(command),
    }


__all__ = ["GRANT_TIMEOUT_SECONDS", "apply_grant", "grant_command", "known_flatpak_ids", "validate_grant"]
