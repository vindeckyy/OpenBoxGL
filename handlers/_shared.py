"""Shared handler utilities extracted to break cross-handler dependencies."""

from pathlib import Path


def clean_extras(items, command):
    """Validate and clean a list of game extras (applications, versions, documents)."""
    if not isinstance(items, list):
        raise ValueError("Game extras must be a list.")
    clean = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        record = {"name": str(item.get("name") or Path(path).stem).strip(), "path": path}
        if command:
            record["command"] = str(item.get("command", "")).strip()
        clean.append(record)
    return clean
