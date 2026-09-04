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
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv-dev"
RUFF = VENV / "bin" / "ruff"
COVERAGE = VENV / "bin" / "coverage"

# Coverage floors. Ratcheted baseline: 72% total, 54% web_app.py.
# Raise the floors as phases land; never lower them silently.
COVERAGE_FLOOR = 72.0
WEB_APP_FLOOR = 54.0
CHANGED_LINE_FLOOR = 80.0
NEW_MODULE_FLOOR = 85.0


def run(command):
    print(f"$ {' '.join(str(part) for part in command)}")
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def _git_diff_base() -> str:
    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        merge = run(["git", "merge-base", "HEAD", upstream.stdout.strip()])
        if merge.returncode == 0 and merge.stdout.strip():
            return merge.stdout.strip()
    head = run(["git", "rev-parse", "HEAD"])
    return head.stdout.strip() if head.returncode == 0 else "HEAD"


def _runtime_modules_at(ref: str) -> set[str]:
    show = run(["git", "show", f"{ref}:runtime_modules.txt"])
    if show.returncode != 0:
        return set()
    return {
        line.strip()
        for line in show.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _check_new_module_coverage(coverage_bin: Path, failures: list[str]) -> None:
    base = _git_diff_base()
    current = {
        line.strip()
        for line in (ROOT / "runtime_modules.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    previous = _runtime_modules_at(base)
    new_modules = sorted(module for module in current - previous if module.endswith(".py"))
    if not new_modules:
        print("new runtime modules: none since diff base")
        return
    include = ",".join(new_modules)
    result = run([str(coverage_bin), "report", f"--include={include}", f"--fail-under={int(NEW_MODULE_FLOOR)}"])
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        failures.append(f"new runtime module coverage floor {NEW_MODULE_FLOOR:.0f}%")
    else:
        print(f"new runtime modules ({len(new_modules)}): >= {NEW_MODULE_FLOOR:.0f}% coverage")


def _check_changed_line_floor(coverage_bin: Path, failures: list[str]) -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.check_changed_coverage import measure_changed_lines

    hit, total, changed_files = measure_changed_lines()
    if not changed_files:
        print(f"changed-line coverage: no Python changes since diff base; pass (floor {CHANGED_LINE_FLOOR:.0f}%)")
        return
    if total == 0:
        print(f"changed-line coverage: no executable changed lines; pass (floor {CHANGED_LINE_FLOOR:.0f}%)")
        return
    pct = 100.0 * hit / total
    print(f"changed-line coverage: {hit}/{total} = {pct:.1f}% (floor {CHANGED_LINE_FLOOR:.0f}%)")
    if pct < CHANGED_LINE_FLOOR:
        failures.append(f"changed-line coverage floor {CHANGED_LINE_FLOOR:.0f}%")


def main() -> int:
    if shutil.which("xvfb-run") and not os.environ.get("OPENBOX_HEADLESS_GATE"):
        env = os.environ.copy()
        env["OPENBOX_HEADLESS_GATE"] = "1"
        res = subprocess.run(["xvfb-run", "-a", sys.executable, *sys.argv], env=env, check=False)
        return res.returncode

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

    # Stage 2.7: i18n key coverage (1.7.2). All locale files must have
    # 100% key coverage and all data-i18n keys must exist in en.json.
    i18n_check = run([sys.executable, "-B", str(ROOT / "scripts" / "check_i18n.py")])
    if i18n_check.stdout.strip():
        print(i18n_check.stdout.strip())
    if i18n_check.stderr.strip():
        print(i18n_check.stderr.strip())
    if i18n_check.returncode != 0:
        failures.append("i18n")


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
        env["COVERAGE_RUN"] = "1"
        for test_file in test_files:
            command = [str(COVERAGE), "run", "-p", str(test_file)]
            last_output = ""
            code = 1
            for _attempt in range(3):
                result = subprocess.run(
                    command,
                    cwd=ROOT, capture_output=True, text=True,
                    check=False, env=env,
                )
                code = result.returncode
                last_output = (result.stdout or "") + (result.stderr or "")
                if code == 0:
                    break
            if code:
                failed_tests.append(test_file.name)
                print(f"FAIL {test_file.name}")
                if last_output.strip():
                    tail = last_output.strip().splitlines()[-40:]
                    print("\n".join(tail))
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

            _check_changed_line_floor(COVERAGE, failures)
            _check_new_module_coverage(COVERAGE, failures)

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
