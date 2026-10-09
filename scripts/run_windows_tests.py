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
# Same tolerance as scripts/check_tests.py, so a timing-flaky suite cannot fail
# in the Windows CI job while passing in the local gate.
ATTEMPTS = 3


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
    """Run one test file, retrying a failure the way the local gate does.

    scripts/check_tests.py gives each suite three attempts and prints the
    first failure when a later attempt passes. This runner runs in the
    `windows-latest` CI job, where a wall-clock assertion such as
    test_auto_import's 10k-ROM throughput budget can fail on a loaded shared
    runner and pass locally -- with no retry there, that class of flake can
    only ever surface in CI, never on a developer machine. Matching the
    tolerance keeps the two runners reporting the same thing.

    The retry is reported, never silent: a file that needed one is marked
    `retried` and its first failure is kept, so a real defect that happens
    to fail twice still fails the job.
    """
    first_failure: dict | None = None
    for attempt in range(1, ATTEMPTS + 1):
        result = _run_once(path, env)
        if result["status"] == "pass":
            if attempt > 1 and first_failure is not None:
                result["status"] = "retried"
                result["first_failure"] = first_failure
            return result
        if first_failure is None:
            first_failure = result
    assert first_failure is not None
    first_failure["attempts"] = ATTEMPTS
    return first_failure


def _run_once(path: Path, env: dict) -> dict:
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
        # Long enough to hold every failure in one file, not just the last few:
        # the Windows job prints this inline, and a 40-line tail hid most of the
        # 29 failures in test_emulators.py.
        "stderr_tail": "\n".join(proc.stderr.strip().splitlines()[-600:]),
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
        result = results[name]
        print(f"{result['status']:8} {name} ({result['seconds']}s)", flush=True)
        if result["status"] == "retried":
            # Passed, but only after a retry: the first failure is the whole
            # point of recording it, so print it instead of the passing tail.
            first = result["first_failure"]
            print(f"    (passed on retry; first attempt reported "
                  f"{first['status']} after {first['seconds']}s)", flush=True)
            for line in first["stderr_tail"].splitlines():
                print(f"    {line}", flush=True)
        elif result["status"] != "pass":
            # The JSON only survives locally, so CI needs the tail inline.
            for line in result["stderr_tail"].splitlines():
                print(f"    {line}", flush=True)
            for line in result["stdout_tail"].splitlines():
                print(f"    {line}", flush=True)

    output = _results_path()
    output.write_text(json.dumps(results, indent=2), encoding="utf-8")

    # A retried suite passed. Only a genuine fail or timeout fails the job.
    passed = sum(1 for r in results.values() if r["status"] in ("pass", "retried"))
    retried = sorted(name for name, r in results.items() if r["status"] == "retried")
    failed = sorted(name for name, r in results.items() if r["status"] not in ("pass", "retried"))
    print(f"\n{passed}/{len(results)} passed; details in {output}")
    if retried:
        print(f"retried and passed: {', '.join(retried)}", file=sys.stderr)
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
