#!/usr/bin/env python3
"""Verify the version string in updates.py matches every published spot.

The version is declared once, in updates.py. README badge, metainfo latest
release, PARITY.md lead paragraph, and the bug report template must all agree
or the release would ship with stale version claims. CI runs this on every
push and release.

Run directly: python3 scripts/check_version_sync.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def runtime_version():
    # Importing updates.py drags in no heavyweight dependencies; parse
    # the literal instead so this script works without importing the app.
    source = (ROOT / "updates.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        print("updates.py: VERSION literal not found")
        return None
    return match.group(1)


def check_line(version, file_name, line, expect, context):
    if expect not in line:
        print(f"{file_name}:{context}: expected {expect!r}, got {line.strip()!r}")
        return False
    return True


def main() -> int:
    version = runtime_version()
    if not version:
        return 1
    print(f"declared version: {version}")
    failures = []

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    badge = re.search(r"img\.shields\.io/badge/Release-v([0-9.]+)", readme)
    if not badge or badge.group(1) != version:
        print(f"README.md: release badge is {badge.group(1) if badge else 'missing'}, expected {version}")
        failures.append("README badge")

    metainfo = (ROOT / "openbox.metainfo.xml").read_text(encoding="utf-8")
    latest = re.search(r'<release version="([0-9.]+)"', metainfo)
    if not latest or latest.group(1) != version:
        print(f"openbox.metainfo.xml: latest release is {latest.group(1) if latest else 'missing'}, expected {version}")
        failures.append("metainfo latest release")

    parity = (ROOT / "PARITY.md").read_text(encoding="utf-8")
    parity_line = parity.splitlines()[2] if len(parity.splitlines()) > 2 else ""
    if f"**v{version}**" not in parity_line:
        print(f"PARITY.md: lead paragraph does not mention v{version}")
        failures.append("PARITY lead")

    bug_template = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    if f"v{version}" not in bug_template:
        print(f"bug_report.yml: does not mention v{version}")
        failures.append("bug report template")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if version not in changelog:
        print(f"CHANGELOG.md: no entry mentions {version}")
        failures.append("CHANGELOG")

    if failures:
        print(f"version sync failed: {', '.join(failures)}")
        return 1
    print("version sync ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
