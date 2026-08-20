"""Native host IPC handlers.

These routes are the contract between the web UI and the native host. Python
owns policy and validation; the C host owns native chrome and dialogs. Every
route must keep working (returning a capability-absent result) when no host is
connected, so the web UI stays usable in a plain browser.
"""

from __future__ import annotations

import os
from routes.registry import route


@route("GET", "/api/native/capabilities")
def capabilities(handler, parsed):
    """Report host capabilities; never blocks, and is the page's first call.

    The native host (native_host.c) sets OPENBOX_NATIVE_HOST before spawning
    this server, so its presence means native chrome and dialogs are available
    and the frontend should route through the bridge. gamepad stays "webkit"
    regardless: the host's onGamepad is a no-op stub, so the Web Gamepad API
    must keep serving.
    """
    if not handler.authorized():
        handler.send_json(403, {"error": "Unauthorized"})
        return
    host_attached = bool(os.environ.get("OPENBOX_NATIVE_HOST"))
    handler.send_json(200, {
        "webview": host_attached,
        "dialogs": host_attached,
        "tray": host_attached,
        "single_instance": host_attached,
        "gamepad": "webkit",
        "fullscreen": True,
        "clipboard": True,
    })


@route("POST", "/api/native/dialog")
def dialog(handler, payload):
    """Folder/file/save picker. No host -> cancelled, page falls back."""
    kind = str(payload.get("kind") or "folder")
    if kind not in ("folder", "file", "save"):
        from api_errors import BadRequest
        raise BadRequest("Native dialog kind must be folder, file, or save.")
    handler.send_json(200, {"path": None, "cancelled": True})


@route("POST", "/api/native/open-external")
def open_external(handler, payload):
    """Open a path or URL with the default handler. No host -> not ok."""
    target = str(payload.get("path") or payload.get("url") or "").strip()
    if not target:
        from api_errors import BadRequest
        raise BadRequest("Native open-external needs a path or url.")
    handler.send_json(200, {"ok": False, "error": None})


@route("POST", "/api/native/reveal")
def reveal(handler, payload):
    """Reveal a file in the file manager. No host -> not ok."""
    path = str(payload.get("path") or "").strip()
    if not path:
        from api_errors import BadRequest
        raise BadRequest("Native reveal needs a path.")
    handler.send_json(200, {"ok": False, "error": None})


@route("POST", "/api/native/window")
def window(handler, payload):
    """Window chrome actions. No host -> not ok."""
    action = str(payload.get("action") or "")
    if action not in ("minimize", "toggle-maximize", "close", "set-fullscreen", "unset-fullscreen"):
        from api_errors import BadRequest
        raise BadRequest("Native window action is not supported.")
    handler.send_json(200, {"ok": False})
