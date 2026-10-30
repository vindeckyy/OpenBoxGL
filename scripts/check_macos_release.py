#!/usr/bin/env python3
"""macOS release gate (ADR 0094, plan W5-01).

Static checks, run by `make check`:
  - scripts/macos/OpenBox.entitlements parses, and carries only keys listed in
    ENTITLEMENT_REASONS (ADR 0094's table).
  - scripts/build_macos_app.sh keeps every Developer ID step: the hardened runtime,
    the entitlements file, notarytool submission, stapling, and the Gatekeeper assessment.
  - scripts/install-macos.sh sets NOTARIZED_BUILD=0 exactly once; the notarized release job
    rewrites that line to 1 in the installer it ships, which removes the install caveat.

With --bundle PATH, the built bundle is checked too: its signature verifies, and with
--require-notarized the main executable carries the hardened-runtime flag and Gatekeeper
accepts the bundle as notarized. Without that flag an ad-hoc bundle passes.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTITLEMENTS = ROOT / "scripts" / "macos" / "OpenBox.entitlements"
BUILD_SCRIPT = ROOT / "scripts" / "build_macos_app.sh"
INSTALLER = ROOT / "scripts" / "install-macos.sh"

# Keys ADR 0094 allows, each with its reason. Empty: the dry run needed none.
ENTITLEMENT_REASONS: dict[str, str] = {}

REQUIRED_PIPELINE = (
    "--options runtime",
    "--entitlements",
    "notarytool submit",
    "xcrun stapler staple",
    "spctl --assess",
)


def entitlement_problems(entitlements: dict) -> list[str]:
    return [
        f"entitlement {key} is not in ADR 0094's table"
        for key in sorted(entitlements)
        if key not in ENTITLEMENT_REASONS
    ]


def pipeline_problems(script_text: str) -> list[str]:
    return [
        f"build script lost a notarization step: {step!r}"
        for step in REQUIRED_PIPELINE
        if step not in script_text
    ]


def runtime_flag_set(codesign_details: str) -> bool:
    """True when `codesign -dv` lists the hardened runtime, as in flags=0x10002(adhoc,runtime)."""
    match = re.search(r"flags=0x[0-9a-fA-F]+\(([^)]*)\)", codesign_details)
    return bool(match) and "runtime" in match.group(1).split(",")


def notarized(returncode: int, assessment: str) -> bool:
    """Gatekeeper accepts the bundle and names a notarized Developer ID source."""
    return returncode == 0 and "source=Notarized Developer ID" in assessment


def bundle_problems(bundle: Path, require_notarized: bool) -> list[str]:
    problems = []
    verify = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        problems.append(f"codesign --verify failed: {verify.stderr.strip()[:200]}")
    if not require_notarized:
        return problems
    details = subprocess.run(
        ["codesign", "-dv", "--verbose=4", str(bundle / "Contents" / "MacOS" / "OpenBox")],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    if not runtime_flag_set(details):
        problems.append("the main executable lacks the hardened-runtime flag")
    assessment = subprocess.run(
        ["spctl", "--assess", "--type", "execute", "--verbose=2", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    if not notarized(assessment.returncode, assessment.stdout + assessment.stderr):
        problems.append("Gatekeeper does not accept the bundle as notarized")
    return problems


def main(argv: list[str]) -> int:
    problems = []
    problems += entitlement_problems(plistlib.loads(ENTITLEMENTS.read_bytes()))
    problems += pipeline_problems(BUILD_SCRIPT.read_text(encoding="utf-8"))
    defaults = re.findall(r"^NOTARIZED_BUILD=(\S+)$", INSTALLER.read_text(encoding="utf-8"), re.MULTILINE)
    if defaults != ["0"]:
        problems.append("install-macos.sh must set NOTARIZED_BUILD=0 exactly once")
    if "--bundle" in argv:
        bundle = Path(argv[argv.index("--bundle") + 1])
        problems += bundle_problems(bundle, require_notarized="--require-notarized" in argv)
    if problems:
        print("FAIL: macOS release gate")
        for problem in problems:
            print("  " + problem)
        return 1
    print("macOS release gate OK: entitlements, notarization pipeline, installer caveat")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
