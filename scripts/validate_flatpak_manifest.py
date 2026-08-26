#!/usr/bin/env python3
"""Validate io.openbox.GameLauncher.yml when flatpak-builder --dry-run is unavailable."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "io.openbox.GameLauncher.yml"

REQUIRED_SNIPPETS = (
    "app-id: io.openbox.GameLauncher",
    "runtime: org.gnome.Platform",
    "runtime-version: '46'",
    "sdk: org.gnome.Sdk",
    "command: openbox",
)


def main() -> int:
    if not MANIFEST.is_file():
        print(f"missing manifest: {MANIFEST}", file=sys.stderr)
        return 1
    text = MANIFEST.read_text(encoding="utf-8")
    missing = [snippet for snippet in REQUIRED_SNIPPETS if snippet not in text]
    if missing:
        print("flatpak manifest missing required fields:", ", ".join(missing), file=sys.stderr)
        return 1
    for pattern, label in (
        (r"^finish-args:", "finish-args"),
        (r"^modules:", "modules"),
    ):
        if not re.search(pattern, text, re.MULTILINE):
            print(f"flatpak manifest missing {label}", file=sys.stderr)
            return 1
    print("flatpak manifest validation: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
