#!/usr/bin/env python3
"""Run frontend linting and type-checking gates.

Runs:
  1. eslint static/*.js (using scripts/eslint.config.mjs)
  2. tsc -p scripts/tsconfig.json (allowJs; checkJs enabled when the tree is ready)

Exits non-zero when eslint or tsc fail. In CI, npm and typescript are required.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _in_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def check_eslint(files: list[Path]) -> bool:
    """Run eslint over static/*.js files."""
    eslint = SCRIPTS / "node_modules" / ".bin" / "eslint"
    if eslint.is_file():
        cmd = [str(eslint)]
    else:
        npx = shutil.which("npx")
        if not npx:
            print("eslint: npx not found", file=sys.stderr)
            return not _in_ci()
        cmd = [npx, "eslint"]

    config_path = SCRIPTS / "eslint.config.mjs"
    if config_path.is_file():
        cmd.extend(["--config", str(config_path.relative_to(ROOT))])
    cmd.extend([str(f.relative_to(ROOT)) for f in files])

    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if res.returncode == 0:
        print("eslint: OK")
        return True

    output = (res.stdout + "\n" + res.stderr).strip()
    print(f"eslint failed (exit {res.returncode}):\n{output}", file=sys.stderr)
    return False


def check_tsc() -> bool:
    """Run project tsc over static/*.js. Blocking when typescript is installed."""
    tsc = SCRIPTS / "node_modules" / ".bin" / "tsc"
    tsconfig = SCRIPTS / "tsconfig.json"
    if tsc.is_file() and tsconfig.is_file():
        cmd = [str(tsc), "-p", str(tsconfig.relative_to(ROOT))]
    else:
        npx = shutil.which("npx")
        if not npx:
            print("tsc: npx not found", file=sys.stderr)
            return not _in_ci()
        cmd = [npx, "tsc", "-p", str(tsconfig.relative_to(ROOT))]

    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if res.returncode == 0:
        print("tsc: OK")
        return True

    output = (res.stdout + "\n" + res.stderr).strip()
    if "This is not the tsc command you are looking for" in output or "Cannot find package" in output:
        print("tsc: typescript not installed", file=sys.stderr)
        return not _in_ci()

    print(f"tsc failed (exit {res.returncode}):\n{output}", file=sys.stderr)
    return False


def main() -> int:
    static_dir = ROOT / "static"
    if not static_dir.is_dir():
        print(f"static directory missing at {static_dir}", file=sys.stderr)
        return 1

    js_files = sorted(static_dir.glob("*.js"))
    if not js_files:
        print("No static JS files found to check.")
        return 0

    passed = check_eslint(js_files) and check_tsc()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
