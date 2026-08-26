"""Package a redacted diagnostic report for pasting into a GitHub issue.

Local-only and opt-in: nothing leaves the machine unless the user copies it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from openbox_logging import read_diagnostic_log

_HOME_PATH_RE = re.compile(r"/home/[^/]+")
_REQUEST_ID_RE = re.compile(r"\[([0-9a-f]{8})\]")


def tokenize_home_paths(value):
    """Replace home directories and /home/<user> segments with ~."""
    home = os.path.expanduser("~")
    if isinstance(value, str):
        if home:
            value = value.replace(home, "~")
        return _HOME_PATH_RE.sub("~", value)
    if isinstance(value, dict):
        return {key: tokenize_home_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [tokenize_home_paths(item) for item in value]
    return value


def system_facts() -> dict:
    """Best-effort host facts, each guarded so a missing module never blocks."""
    facts = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "user_agent_os": "",
        "data_dir": "",
        "version": "",
    }
    try:
        import updates  # local import keeps this importable pre-install

        facts["version"] = getattr(updates, "VERSION", "")
    except Exception:  # pragma: no cover - import failure is the point
        pass
    return facts


def install_channel() -> str:
    if os.environ.get("FLATPAK_ID"):
        return "flatpak"
    if os.environ.get("APPIMAGE"):
        return "appimage"
    return "source"


def desktop_session() -> dict:
    return {
        "type": os.environ.get("XDG_SESSION_TYPE", ""),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "") or os.environ.get("DESKTOP_SESSION", ""),
    }


def native_host_facts() -> dict:
    attached = bool(os.environ.get("OPENBOX_NATIVE_HOST"))
    return {
        "attached": attached,
        "webkit": attached,
    }


def renderer_flags() -> dict:
    attached = bool(os.environ.get("OPENBOX_NATIVE_HOST"))
    return {
        "native_host": attached,
        "browser_fallback": not attached,
    }


def disk_space_facts(data_dir) -> dict:
    try:
        usage = shutil.disk_usage(Path(data_dir))
    except OSError:
        return {"total_bytes": 0, "free_bytes": 0}
    return {"total_bytes": usage.total, "free_bytes": usage.free}


def package_integrity_facts() -> dict:
    appimage = os.environ.get("APPIMAGE", "").strip()
    if not appimage:
        return {"status": "skipped", "reason": "source"}
    manifest = Path(appimage).resolve().parent / "sbom-manifest.json"
    if not manifest.is_file():
        return {"status": "skipped", "reason": "manifest_missing"}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "failed", "reason": "manifest_unreadable"}
    return {
        "status": "ok",
        "file_count": payload.get("file_count", 0),
    }


def _library_facts(root: Path) -> tuple[int, int, int]:
    library_path = root / "library.json"
    try:
        with library_path.open(encoding="utf-8") as source:
            raw = source.read()
        payload = json.loads(raw)
        games = payload.get("games", [])
        library_count = len(games) if isinstance(games, list) else 0
        schema_version = int(payload.get("schema_version", 0))
        library_bytes = len(raw.encode("utf-8", errors="replace"))
        return library_count, schema_version, library_bytes
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0, 0, 0


def recent_request_ids_from_log(data_dir, *, limit=10) -> list[str]:
    log = read_diagnostic_log(data_dir, limit=250_000)
    seen = []
    for match in reversed(list(_REQUEST_ID_RE.finditer(log))):
        request_id = match.group(1)
        if request_id not in seen:
            seen.append(request_id)
        if len(seen) >= limit:
            break
    return list(reversed(seen))


def build_preview(data_dir, *, recent_job_ids=None, recent_request_ids=None):
    """Return a tokenized diagnostic preview payload without the full log."""
    root = Path(data_dir)
    facts = system_facts()
    facts["data_dir"] = str(root)
    library_count, schema_version, library_bytes = _library_facts(root)
    facts["library_bytes"] = library_bytes
    request_ids = list(recent_request_ids or recent_request_ids_from_log(root))
    preview = {
        "report": "openbox-diagnostic",
        "version": facts.pop("version"),
        "install_channel": install_channel(),
        "architecture": platform.machine(),
        "distro": platform.platform(),
        "desktop_session": desktop_session(),
        "native_host": native_host_facts(),
        "renderer_flags": renderer_flags(),
        "library_count": library_count,
        "schema_version": schema_version,
        "disk_space": disk_space_facts(root),
        "package_integrity": package_integrity_facts(),
        "recent_job_ids": list(recent_job_ids or []),
        "recent_request_ids": request_ids,
        "system": facts,
        "python_executable": sys.executable,
        "diagnostic_log": "",
    }
    return tokenize_home_paths(preview)


def build_report(data_dir, *, include_log=True, log_limit=250_000):
    """Return the diagnostic report text; the log is redacted on read."""
    preview = build_preview(data_dir)
    if include_log:
        log = read_diagnostic_log(data_dir, limit=log_limit)
        preview["diagnostic_log"] = log[-log_limit:]
        preview = tokenize_home_paths(preview)
    return json.dumps(preview, indent=2)
