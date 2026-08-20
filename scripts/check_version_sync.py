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


def _doc_path(name: str) -> Path:
    # Support both flat (docs at root) and docs/ layout after reorg
    direct = ROOT / name
    if direct.is_file():
        return direct
    docs_path = ROOT / "docs" / name
    if docs_path.is_file():
        return docs_path
    return direct


def runtime_version():
    # Importing updates.py drags in no heavyweight dependencies; parse
    # the literal instead so this script works without importing the app.
    source = (ROOT / "updates.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if not match:
        print("updates.py: VERSION literal not found")
        return None
    return match.group(1)


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

    if f"Latest stable: v{version}" not in readme:
        print(f"README.md: latest stable link does not mention v{version}")
        failures.append("README latest stable")

    readme_version = re.search(r"^VERSION=([0-9.]+)", readme, re.MULTILINE)
    if not readme_version or readme_version.group(1) != version:
        print(f"README.md: installer VERSION is {readme_version.group(1) if readme_version else 'missing'}, expected {version}")
        failures.append("README installer VERSION")

    metainfo = (ROOT / "openbox.metainfo.xml").read_text(encoding="utf-8")
    latest = re.search(r'<release version="([0-9.]+)"', metainfo)
    if not latest or latest.group(1) != version:
        print(f"openbox.metainfo.xml: latest release is {latest.group(1) if latest else 'missing'}, expected {version}")
        failures.append("metainfo latest release")

    parity = _doc_path("PARITY.md").read_text(encoding="utf-8")
    parity_line = parity.splitlines()[2] if len(parity.splitlines()) > 2 else ""
    if f"**v{version}**" not in parity_line:
        print(f"PARITY.md: lead paragraph does not mention v{version}")
        failures.append("PARITY lead")

    bug_template = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    if f"v{version}" not in bug_template:
        print(f"bug_report.yml: does not mention v{version}")
        failures.append("bug report template")

    changelog = _doc_path("CHANGELOG.md").read_text(encoding="utf-8")
    if version not in changelog:
        print(f"CHANGELOG.md: no entry mentions {version}")
        failures.append("CHANGELOG")


    release_notes_path = _doc_path("RELEASE_NOTES.md")
    if release_notes_path.is_file():
        release_notes = release_notes_path.read_text(encoding="utf-8")
        if f"...v{version}" not in release_notes and f"v{version}" not in release_notes:
            print(f"RELEASE_NOTES.md: compare link does not mention v{version}")
            failures.append("RELEASE_NOTES")

    sbom_script_path = ROOT / "scripts" / "gen_sbom.py"
    if sbom_script_path.is_file():
        sbom_script = sbom_script_path.read_text(encoding="utf-8")
        sbom_default = re.search(r'DEFAULT_VERSION\s*=\s*"([0-9.]+)"', sbom_script)
        if not sbom_default or sbom_default.group(1) != version:
            print(f"gen_sbom.py: fallback DEFAULT_VERSION is {sbom_default.group(1) if sbom_default else 'missing'}, expected {version}")
            failures.append("gen_sbom DEFAULT_VERSION")
    if failures:
        print(f"version sync failed: {', '.join(failures)}")
        return 1
    print("version sync ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
