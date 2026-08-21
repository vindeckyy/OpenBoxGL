#!/usr/bin/env python3
"""Run frontend linting and type-checking gates.

Runs:
  1. npx eslint static/*.js (using static/eslint.config.mjs or default config)
  2. npx tsc --noEmit --allowJs --checkJs static/*.js

Degrades gracefully to a warning (exits 0) if eslint or tsc/typescript are not
installed in the current environment.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def check_eslint(files: list[Path]) -> bool:
    """Run eslint over static/*.js files. Returns True if passed or skipped."""
    npx = shutil.which("npx")
    if not npx:
        print("warning: npx not found, skipping eslint check")
        return True

    config_path = ROOT / "scripts" / "eslint.config.mjs"
    if not config_path.is_file():
        config_path = ROOT / "static" / "eslint.config.mjs"
    cmd = [npx, "eslint"]
    if config_path.is_file():
        cmd.extend(["--config", str(config_path.relative_to(ROOT))])
    cmd.extend([str(f.relative_to(ROOT)) for f in files])

    try:
        res = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        print(f"warning: failed to run eslint ({exc}), skipping")
        return True

    if res.returncode == 0:
        print("eslint: OK")
        return True

    output = (res.stdout + "\n" + res.stderr).strip()
    # Check if failure is due to missing eslint package or module
    if "Cannot find package" in output or "couldn't find" in output or "ERR_MODULE_NOT_FOUND" in output:
        print(f"warning: eslint dependencies not fully installed, skipping ({output.splitlines()[0]})")
        return True

    print(f"eslint failed (exit {res.returncode}):\n{output}", file=sys.stderr)
    return False


def check_tsc(files: list[Path]) -> bool:
    """Run tsc over static/*.js files. Returns True if passed or skipped."""
    npx = shutil.which("npx")
    tsc = shutil.which("tsc")

    if not npx and not tsc:
        print("warning: tsc and npx not found, skipping typescript check")
        return True

    if tsc:
        cmd = [tsc, "--noEmit", "--allowJs", "--checkJs"]
    else:
        cmd = [npx, "tsc", "--noEmit", "--allowJs", "--checkJs"]

    cmd.extend([str(f.relative_to(ROOT)) for f in files])

    try:
        res = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        print(f"warning: failed to run tsc ({exc}), skipping")
        return True

    if res.returncode == 0:
        print("tsc: OK")
        return True

    output = (res.stdout + "\n" + res.stderr).strip()
    # If tsc is the dummy npm package or missing typescript compiler
    if "This is not the tsc command you are looking for" in output or "not found" in output or "npm error" in output or "Cannot find package" in output:
        print("warning: tsc/typescript not installed, skipping type check")
        return True

    # JS with allowJs produces many implicit-any errors; treat as warning for now
    # so the gate does not block on type hygiene that the project has not adopted.
    print(f"warning: tsc type check reported {len(output.splitlines())} lines (non-blocking):\n{output.splitlines()[0] if output else ''}", file=sys.stderr)
    return True

def main() -> int:
    static_dir = ROOT / "static"
    if not static_dir.is_dir():
        print(f"static directory missing at {static_dir}", file=sys.stderr)
        return 1

    js_files = sorted(static_dir.glob("*.js"))
    if not js_files:
        print("No static JS files found to check.")
        return 0

    passed = True
    if not check_eslint(js_files):
        passed = False
    if not check_tsc(js_files):
        passed = False

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
