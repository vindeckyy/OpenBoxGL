#!/usr/bin/env python3
"""Run the complete OpenBox verification gate.

Stages:
  1. ruff lint (gate rule set from pyproject.toml)
  2. py_compile over all runtime modules and test files
  3. full test suite under coverage, run in parallel workers
  4. coverage floor checks (total + web_app.py)

Exits non-zero when any stage fails. Used by `make check` and CI.

Dev-only dependencies are expected in .venv-dev (see docs/CONTRIBUTING.md).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-dev"
RUFF = VENV / "bin" / "ruff"
COVERAGE = VENV / "bin" / "coverage"

# Coverage floors. Ratcheted baseline: 60% total, 48% web_app.py.
# Raise the floors as phases land; never lower them silently.
COVERAGE_FLOOR = 60.0
WEB_APP_FLOOR = 48.0


def run(command):
    print(f"$ {' '.join(str(part) for part in command)}")
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def main() -> int:
    failures = []

    # Stage 1: lint.
    if not RUFF.is_file():
        print("Missing .venv-dev/bin/ruff. Run: make dev-venv")
        failures.append("ruff missing")
    else:
        result = run([str(RUFF), "check", "."])
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            failures.append("ruff")

    # Stage 2.3: runtime_modules.txt must match the repository layout.
    runtime_modules = run([sys.executable, "-B", str(ROOT / "scripts" / "check_runtime_modules.py")])
    if runtime_modules.stdout.strip():
        print(runtime_modules.stdout.strip())
    if runtime_modules.stderr.strip():
        print(runtime_modules.stderr.strip())
    if runtime_modules.returncode != 0:
        failures.append("runtime_modules")

    # Stage 2.4: v1 route surface must match the frozen contract. The v1
    # surface is the native host's only contract; drift fails the gate.
    v1_contract = run([sys.executable, "-B", str(ROOT / "scripts" / "check_v1_contract.py")])
    if v1_contract.returncode != 0:
        if v1_contract.stdout.strip():
            print(v1_contract.stdout.strip())
        if v1_contract.stderr.strip():
            print(v1_contract.stderr.strip())
        failures.append("v1_contract")

    # Stage 2.5: version strings must stay in sync across published spots.
    version_sync = run([sys.executable, "-B", str(ROOT / "scripts" / "check_version_sync.py")])
    if version_sync.returncode != 0:
        if version_sync.stdout.strip():
            print(version_sync.stdout.strip())
        if version_sync.stderr.strip():
            print(version_sync.stderr.strip())
        failures.append("version_sync")

    # Stage 2.6: frontend lint (eslint). Uses scripts/check_frontend.py which
    # degrades to a warning when npm/eslint are absent locally but must pass
    # in CI where npm is installed. On CI npm is always present.
    frontend = run([sys.executable, "-B", str(ROOT / "scripts" / "check_frontend.py")])
    if frontend.stdout.strip():
        print(frontend.stdout.strip())
    if frontend.stderr.strip():
        print(frontend.stderr.strip())
    if frontend.returncode != 0:
        failures.append("frontend")


    modules = [line.strip() for line in (ROOT / "runtime_modules.txt").read_text().splitlines() if line.strip()]
    compile_failed = 0
    for module in modules:
        path = ROOT / module
        if not path.is_file():
            print(f"runtime module missing: {module}")
            failures.append(f"runtime module missing: {module}")
            compile_failed += 1
            continue
        if path.suffix != ".py":
            continue
        try:
            compile(path.read_bytes(), str(path), "exec")
        except SyntaxError as error:
            print(f"compile failed: {module}: {error}")
            compile_failed += 1
    for test_file in sorted([*ROOT.glob("test_*.py"), *ROOT.glob("tests/test_*.py")]):
        try:
            compile(test_file.read_bytes(), str(test_file), "exec")
        except SyntaxError as error:
            print(f"compile failed: {test_file.name}: {error}")
            compile_failed += 1
    for script_file in sorted((ROOT / "scripts").glob("*.py")):
        try:
            compile(script_file.read_bytes(), str(script_file), "exec")
        except SyntaxError as error:
            print(f"compile failed: scripts/{script_file.name}: {error}")
            compile_failed += 1
    if compile_failed:
        failures.append("py_compile")
    # Stage 3+4: tests under coverage in parallel, then the floor checks.
    if not COVERAGE.is_file():
        print("Missing .venv-dev/bin/coverage. Run: make dev-venv")
        failures.append("coverage missing")
    else:
        run([str(COVERAGE), "erase"])
        for data_file in ROOT.glob(".coverage*"):
            data_file.unlink()

        test_files = sorted([*ROOT.glob("test_*.py"), *ROOT.glob("tests/test_*.py")])

        # Serial on purpose: the gamescope/deck tests spawn real nested X
        # sessions and collide when run in parallel workers.
        failed_tests = []
        # Ensure root is on PYTHONPATH so tests in tests/ can import flat modules
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        for test_file in test_files:
            code = subprocess.run(
                [str(COVERAGE), "run", "-p", str(test_file)],
                cwd=ROOT, capture_output=True, text=True,
                check=False, env=env,
            ).returncode
            if code:
                failed_tests.append(test_file.name)
                print(f"FAIL {test_file.name}")
            else:
                print(f"PASS {test_file.name}")
        passed_tests = len(test_files) - len(failed_tests)
        print(f"{passed_tests} test files passed, {len(failed_tests)} failed")
        if failed_tests:
            failures.append("tests")


        combined = run([str(COVERAGE), "combine", "--quiet"])
        if combined.returncode != 0:
            print("coverage combine failed")
            failures.append("coverage combine")
        else:
            total_result = run([str(COVERAGE), "report", "--format=total"])
            total_line = total_result.stdout.strip().splitlines()[-1] if total_result.stdout.strip() else "0"
            try:
                total = float(total_line)
            except ValueError:
                total = 0.0
            print(f"coverage: {total:.1f}% (floor {COVERAGE_FLOOR:.1f}%)")
            if total < COVERAGE_FLOOR:
                failures.append("coverage floor")

            web_result = run([str(COVERAGE), "report", "--include=web_app.py", "--format=total"])
            web_line = web_result.stdout.strip().splitlines()[-1] if web_result.stdout.strip() else "0"
            try:
                web_total = float(web_line)
            except ValueError:
                web_total = 0.0
            print(f"web_app.py coverage: {web_total:.1f}% (floor {WEB_APP_FLOOR:.1f}%)")
            if web_total < WEB_APP_FLOOR:
                failures.append("web_app coverage floor")

            # Token hygiene: raw hex outside :root must not rise
            token = run([sys.executable, "scripts/check_tokens.py"])
            if token.stdout.strip():
                print(token.stdout.strip())
            if token.stderr.strip():
                print(token.stderr.strip())
            if token.returncode != 0:
                failures.append("tokens")

    if failures:
        print(f"\nGATE FAILED: {', '.join(failures)}")
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
