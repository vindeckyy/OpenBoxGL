#!/usr/bin/env python3
"""Bump the OpenBox version across the published spots, preview by default.

The version is declared once in ``updates.py``. This helper keeps the
mechanical spots in sync:

  - ``updates.py`` — the ``VERSION`` literal
  - ``README.md`` — release badge, "Latest stable" line, installer ``VERSION=``
  - ``openbox.metainfo.xml`` — the latest ``<release version="...">`` entry

It also refuses to run unless ``docs/CHANGELOG.md`` has a matching
``## [version]`` section or a non-empty ``## [Unreleased]`` block, because a
release without notes is not a release. Remaining version surfaces enforced by
``scripts/check_version_sync.py`` (PARITY.md, SUPPORT.md, SECURITY.md,
flathub-checklist.md, the bug report template, RELEASE_NOTES.md, gen_sbom.py)
are reported after ``--apply`` so nothing is silently missed.

Usage:

  python3 scripts/bump_version.py 1.13.0            # dry run (default)
  python3 scripts/bump_version.py 1.13.0 --apply    # write the files
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _sub(text: str, pattern: str, replacement: str, count: int = 0) -> tuple[str, int]:
    return re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE)


def plan_updates_py(text: str, version: str) -> tuple[str, int]:
    return _sub(text, r'^(VERSION\s*=\s*")[^"]+(")', rf"\g<1>{version}\g<2>", count=1)


def plan_readme(text: str, version: str) -> tuple[str, int]:
    total = 0
    text, n = _sub(text, r"img\.shields\.io/badge/Release-v[0-9.]+", f"img.shields.io/badge/Release-v{version}")
    total += n
    text, n = _sub(text, r"Latest stable: v[0-9.]+", f"Latest stable: v{version}")
    total += n
    text, n = _sub(text, r"^VERSION=[0-9.]+", f"VERSION={version}", count=1)
    total += n
    return text, total


def plan_metainfo(text: str, version: str) -> tuple[str, int]:
    return _sub(text, r'(<release version=")[0-9.]+(")', rf"\g<1>{version}\g<2>", count=1)


def _section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^## \[{re.escape(heading)}\][^\n]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1) if match else ""


def changelog_ready(changelog: str, version: str) -> tuple[bool, str]:
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        return True, f"CHANGELOG.md has a dated [{version}] section"
    unreleased = _section(changelog, "Unreleased")
    if any(line.strip().startswith("- ") for line in unreleased.splitlines()):
        return True, "CHANGELOG.md [Unreleased] has entries; promote the section before tagging"
    return False, f"CHANGELOG.md has no [{version}] section and [Unreleased] is empty"


def build_plan(version: str) -> tuple[list[tuple], bool, str]:
    edits = []
    for name, planner in (
        ("updates.py", plan_updates_py),
        ("README.md", plan_readme),
        ("openbox.metainfo.xml", plan_metainfo),
    ):
        path = ROOT / name
        old_text = path.read_text(encoding="utf-8")
        new_text, substitutions = planner(old_text, version)
        edits.append((path, old_text, new_text, substitutions))
    changelog = (ROOT / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    ready, reason = changelog_ready(changelog, version)
    return edits, ready, reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="new semantic version, e.g. 1.13.0")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes (default is a dry run)",
    )
    args = parser.parse_args(argv)

    if not VERSION_RE.match(args.version):
        print(f"bump_version: invalid version {args.version!r}; expected MAJOR.MINOR.PATCH", file=sys.stderr)
        return 2

    edits, ready, reason = build_plan(args.version)
    mode = "apply" if args.apply else "dry-run"
    print(f"bump_version {args.version} ({mode})")
    for path, old_text, new_text, substitutions in edits:
        rel = path.relative_to(ROOT)
        state = "change" if old_text != new_text else "unchanged"
        print(f"  {rel}: {state} ({substitutions} substitution(s))")
    print(f"  changelog: {'ok' if ready else 'BLOCKED'} — {reason}")
    if not ready:
        print("bump_version: refusing to bump without changelog entries", file=sys.stderr)
        return 1

    if not args.apply:
        print("dry run: rerun with --apply to write")
        return 0

    written = 0
    for path, old_text, new_text, _ in edits:
        if old_text != new_text:
            path.write_text(new_text, encoding="utf-8")
            written += 1
    print(f"bump_version: wrote {written} file(s) for {args.version}")

    from scripts.check_version_sync import check

    remaining = check(args.version)
    if remaining:
        print("bump_version: still manual (check_version_sync will fail until updated):")
        for item in remaining:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
