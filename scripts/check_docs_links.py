#!/usr/bin/env python3
"""Verify relative markdown links in README.md and docs/ resolve on disk.

Scans inline links and images (``[text](target)``, ``![alt](target)``) plus
reference definitions (``[id]: target``). External URLs, anchors, and
``mailto:`` targets are ignored. Absolute targets (``/foo``) resolve from the
repository root; everything else resolves relative to the containing file.
Fragments and query strings are stripped before the filesystem check.

This gate keeps ADR 0041's complete-index intent honest: a moved or renamed
doc must not leave dangling links behind. It does not touch the network.

Run directly: python3 scripts/check_docs_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
INLINE_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
REFERENCE_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+(\S+)", re.MULTILINE)
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "data:")


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    return [path for path in files if path.is_file()]


def targets_in(text: str) -> list[str]:
    return [*INLINE_LINK_RE.findall(text), *REFERENCE_RE.findall(text)]


def _normalize(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return target


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def broken_for(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    failures = []
    for raw in targets_in(text):
        target = _normalize(raw)
        lowered = target.lower()
        if not target or lowered.startswith(SKIP_SCHEMES):
            continue
        split = urlsplit(target)
        relative = unquote(split.path)
        if not relative:
            # Pure anchor or query on the current document.
            continue
        if relative.startswith("/"):
            candidate = ROOT / relative.lstrip("/")
        else:
            candidate = (path.parent / relative).resolve()
        if not candidate.exists():
            failures.append(f"{_display(path)}: broken link -> {target}")
    return failures


def main() -> int:
    failures: list[str] = []
    files = markdown_files()
    for path in files:
        failures.extend(broken_for(path))
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(f"docs links: {len(failures)} broken of {len(files)} markdown files scanned", file=sys.stderr)
        return 1
    print(f"docs links: {len(files)} markdown files scanned, all relative links resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
