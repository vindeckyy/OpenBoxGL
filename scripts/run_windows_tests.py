#!/usr/bin/env python3
"""Run the standalone test suite on Windows with per-file reporting.

Every ``tests/test_*.py`` runs in its own subprocess (the suite is written as
standalone scripts) and is reported as ``pass``/``fail``/``timeout``. Results
are written as JSON for CI and local triage.

Windows CI uses this instead of ``make check``: ruff/coverage live in the
dev-only POSIX virtualenv and the AppImage/Flatpak stages are Linux-only.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TIMEOUT_SECONDS = 120


def _results_path() -> Path:
    override = os.environ.get("OPENBOX_TEST_RESULTS")
    if override:
        path = Path(override)
    else:
        path = Path(tempfile.gettempdir()) / "openbox-windows-test-results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    # Isolate state: modules bind the data dir at import time, so a shared temp
    # dir keeps the suite out of a developer's real library.
    env.setdefault("OPENBOX_DATA_DIR", tempfile.mkdtemp(prefix="openbox-windows-data."))
    return env


def run_file(path: Path, env: dict) -> dict:
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "returncode": None,
            "seconds": round(time.monotonic() - start, 1),
            "stderr_tail": "",
            "stdout_tail": "",
        }
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - start, 1),
        # Long enough for a full unittest traceback: the Windows job prints
        # this inline, and a 12-line tail cut the frames that named the
        # failing assertion.
        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-40:]),
        "stdout_tail": "\n".join(proc.stdout.strip().splitlines()[-12:]),
    }


def main() -> int:
    only = sys.argv[1:] or None
    names = sorted(path.name for path in TESTS.glob("test_*.py"))
    if only:
        names = [name for name in names if name in only]
    if not names:
        print("no test files matched", file=sys.stderr)
        return 1

    env = _env()
    results = {}
    for name in names:
        results[name] = run_file(TESTS / name, env)
        print(f"{results[name]['status']:8} {name} ({results[name]['seconds']}s)", flush=True)
        if results[name]["status"] != "pass":
            # The JSON only survives locally, so CI needs the tail inline.
            for line in results[name]["stderr_tail"].splitlines():
                print(f"    {line}", flush=True)
            for line in results[name]["stdout_tail"].splitlines():
                print(f"    {line}", flush=True)

    output = _results_path()
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")

    passed = sum(1 for result in results.values() if result["status"] == "pass")
    failed = sorted(name for name, result in results.items() if result["status"] != "pass")
    print(f"\n{passed}/{len(results)} passed; details in {output}")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
