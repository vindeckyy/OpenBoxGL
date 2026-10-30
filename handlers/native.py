"""Native host IPC handlers.

These routes are the contract between the web UI and the native host. Python
owns policy and validation; the C host owns native chrome and dialogs. Every
route must keep working (returning a capability-absent result) when no host is
connected, so the web UI stays usable in a plain browser.
"""

from __future__ import annotations

import os
import sys

from api_errors import BadRequest
import plugins
from pkg.platform_compat import battery_status, platform_features
from routes.registry import route


def _handle_unauthorized(handler):
    if hasattr(handler, "handle_unauthorized"):
        handler.handle_unauthorized()
    else:
        handler.send_json(403, {"error": "Unauthorized"})


def _version():
    from updates import VERSION
    return VERSION


def _data_dir():
    from openbox import APP_DIR
    return str(APP_DIR)


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
        _handle_unauthorized(handler)
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
        # Read by Settings > About: "where is my stuff" and "why is this a browser tab".
        "version": _version(),
        "platform": sys.platform,
        "data_dir": _data_dir(),
        # One contract for every platform-specific control (ADR 0086). A
        # missing object means every key is false: hidden is the safe default.
        "features": platform_features(),
    })


@route("GET", "/api/native/battery")
def battery(handler, parsed):
    """Report battery presence, percentage, and charging state."""
    if not handler.authorized():
        _handle_unauthorized(handler)
        return
    handler.send_json(200, battery_status())


@route("POST", "/api/native/dialog")
def dialog(handler, payload):
    """Folder/file/save picker. No host -> cancelled, page falls back."""
    if not handler.authorized():
        _handle_unauthorized(handler)
        return
    kind = str(payload.get("kind") or "folder")
    if kind not in ("folder", "file", "save"):
        raise BadRequest("Native dialog kind must be folder, file, or save.")
    start_path = str(payload.get("start_path") or "").strip()
    if start_path and not os.path.isabs(start_path):
        raise BadRequest("Native dialog start_path must be absolute.")
    handler.send_json(200, {"path": None, "cancelled": True})


@route("POST", "/api/native/open-external")
def open_external(handler, payload):
    """Open a path or URL with the default handler. No host -> not ok."""
    if not handler.authorized():
        _handle_unauthorized(handler)
        return
    target = str(payload.get("path") or payload.get("url") or "").strip()
    if not target:
        raise BadRequest("Native open-external needs a path or url.")
    handler.send_json(200, {"ok": False, "error": None})


@route("POST", "/api/native/reveal")
def reveal(handler, payload):
    """Reveal a file in the file manager. No host -> not ok."""
    if not handler.authorized():
        _handle_unauthorized(handler)
        return
    path = str(payload.get("path") or "").strip()
    if not path:
        raise BadRequest("Native reveal needs a path.")
    handler.send_json(200, {"ok": False, "error": None})


@route("POST", "/api/native/window")
def window(handler, payload):
    """Window chrome actions. No host -> not ok."""
    if not handler.authorized():
        _handle_unauthorized(handler)
        return
    action = str(payload.get("action") or "")
    if action not in ("minimize", "toggle-maximize", "close", "set-fullscreen", "unset-fullscreen"):
        raise BadRequest("Native window action is not supported.")
    handler.send_json(200, {"ok": False})


API_VERSION = 2
# The newest plugin API this build serves. A v1 plugin still runs unchanged (ADR 0092).
PLUGIN_API_VERSION = plugins.PLUGIN_API_VERSION_SUPPORTED
BACKUP_FORMAT_VERSION = 2
SYNC_FORMAT_VERSION = 3


@route("GET", "/api/v2/version")
def version_info(handler, parsed):
    """Advertise every contract version so a client can refuse a shape it cannot read (ADR 0087)."""
    import platform
    import sys

    from state_store import STATE_SCHEMA_VERSION
    from updates import VERSION

    if sys.platform.startswith("win"):
        host_platform = "windows"
    elif sys.platform == "darwin":
        host_platform = "macos"
    else:
        host_platform = "linux"
    handler.send_json(200, {
        "version": VERSION,
        "api_version": API_VERSION,
        "state_schema": STATE_SCHEMA_VERSION,
        "plugin_api_version": PLUGIN_API_VERSION,
        "backup_format": BACKUP_FORMAT_VERSION,
        "sync_format": SYNC_FORMAT_VERSION,
        "platform": host_platform,
        "python": platform.python_version(),
    })
