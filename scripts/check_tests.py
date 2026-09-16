#!/usr/bin/env python3
"""Run the complete OpenBox verification gate.

Stages:
  1. ruff lint (gate rule set from pyproject.toml)
  2. runtime_modules drift, v1 contract, version sync, frontend lint, i18n keys,
     CSP framing, docs links, generated api-v2 freshness
  3. py_compile over all runtime modules, test files, and scripts
  4. full test suite under coverage, run serially (gamescope/X tests collide
     in parallel workers), with a per-file timeout and skip accounting
  5. coverage floor checks (83 total + 73 web_app.py + 95 changed-line + 85
     new-module + touched-module) and design-token hygiene

Exits non-zero when any stage fails. Used by `make check` and CI.

Test retries are reported and a retry that rescues a pass marks the file FLAKY,
which fails the gate: flaky tests must be fixed, not papered over. Environment
skips (gamescope/7z/webkit) are counted and printed; set
OPENBOX_STRICT_SKIPS=1 to fail on any skip. Per-file timeout defaults to 300s
(OPENBOX_TEST_TIMEOUT overrides).

Dev-only dependencies are expected in .venv-dev (see docs/CONTRIBUTING.md).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.git_diff_base import DiffBaseUnresolved, resolve_diff_base  # noqa: E402
VENV = ROOT / ".venv-dev"
RUFF = VENV / "bin" / "ruff"
COVERAGE = VENV / "bin" / "coverage"

# Coverage floors. Ratcheted baseline: 83% total, 73% web_app.py (measured
# 83.804 / 73.429 on 2026-09-16; rounded down for margin).
# Raise the floors as phases land; never lower them silently.
COVERAGE_FLOOR = 83.0
WEB_APP_FLOOR = 73.0
CHANGED_LINE_FLOOR = 95.0
NEW_MODULE_FLOOR = 85.0
# A single test file that runs longer than this is a hang, not a slow test.
TEST_TIMEOUT = float(os.environ.get("OPENBOX_TEST_TIMEOUT", "300"))
TEST_ATTEMPTS = 3
SKIP_NOTE_RE = re.compile(r"\bskipp(?:ed|ing)\b", re.IGNORECASE)


def run(command, env=None):
    print(f"$ {' '.join(str(part) for part in command)}")
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False, env=env)


def _skip_notes(output: str) -> list[str]:
    notes = []
    for line in output.splitlines():
        candidate = line.strip()
        if candidate and SKIP_NOTE_RE.search(candidate):
            notes.append(candidate[:200])
    return notes


def _git_diff_base() -> str | None:
    return resolve_diff_base(run=run)


def _runtime_modules_at(ref: str) -> set[str]:
    show = run(["git", "show", f"{ref}:runtime_modules.txt"])
    if show.returncode != 0:
        raise RuntimeError(f"git show {ref}:runtime_modules.txt failed: {show.stderr.strip()}")
    return {
        line.strip()
        for line in show.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _unresolved_base_message() -> str:
    return (
        "diff base unresolved: pass OPENBOX_DIFF_BASE, add an upstream, or fetch "
        "origin (use OPENBOX_DIFF_BASE=HEAD to intentionally skip the comparison)"
    )


def _check_new_module_coverage(coverage_bin: Path, base: str, failures: list[str]) -> None:
    current = {
        line.strip()
        for line in (ROOT / "runtime_modules.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    try:
        previous = _runtime_modules_at(base)
    except RuntimeError as exc:
        print(exc)
        failures.append("runtime_modules base unreadable")
        return
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


def _check_changed_line_floor(coverage_bin: Path, base: str, failures: list[str]) -> None:
    from scripts.check_changed_coverage import measure_changed_lines

    try:
        hit, total, changed_files = measure_changed_lines(base)
    except (DiffBaseUnresolved, RuntimeError) as exc:
        print(f"changed-line coverage: {exc}")
        failures.append("changed-line diff base unreadable")
        return
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


def _check_touched_module_floor(failures: list[str]) -> None:
    """Run the standalone touched-module floor used by CI inside `make check`.

    The script also fails any touched module sitting at 0% coverage; keeping
    one implementation avoids drift between the local gate and CI.
    """
    env = os.environ.copy()
    env["PATH"] = f"{VENV / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    venv_python = VENV / "bin" / "python"
    interpreter = str(venv_python) if venv_python.is_file() else sys.executable
    result = subprocess.run(
        [
            interpreter,
            "-B",
            str(ROOT / "scripts" / "check_changed_coverage.py"),
            f"--fail-under={CHANGED_LINE_FLOOR:.0f}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        failures.append("touched-module coverage")


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

    # Stage 2.8: CSP framing contract (1.12.0). Only the document endpoints
    # may relax frame-ancestors; the 1.11 reader regression must not return.
    csp_check = run([sys.executable, "-B", str(ROOT / "scripts" / "check_csp.py")])
    if csp_check.stdout.strip():
        print(csp_check.stdout.strip())
    if csp_check.stderr.strip():
        print(csp_check.stderr.strip())
    if csp_check.returncode != 0:
        failures.append("csp")

    # Stage 2.9: relative markdown links in README.md and docs/ must resolve.
    links_check = run([sys.executable, "-B", str(ROOT / "scripts" / "check_docs_links.py")])
    if links_check.stdout.strip():
        print(links_check.stdout.strip())
    if links_check.stderr.strip():
        print(links_check.stderr.strip())
    if links_check.returncode != 0:
        failures.append("docs links")

    # Stage 2.10: docs/api-v2.md must match the live route registry.
    api_docs_check = run([sys.executable, "-B", str(ROOT / "scripts" / "gen_api_docs.py"), "--check"])
    if api_docs_check.stdout.strip():
        print(api_docs_check.stdout.strip())
    if api_docs_check.stderr.strip():
        print(api_docs_check.stderr.strip())
    if api_docs_check.returncode != 0:
        failures.append("api docs freshness")

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
        flaky_tests = []
        timeout_tests = []
        skip_total = 0
        skip_files = 0
        # Ensure root is on PYTHONPATH so tests in tests/ can import flat modules
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["COVERAGE_RUN"] = "1"
        # Isolate state: openbox.py binds DATA at import time per test
        # process. Without a suite-wide temp dir, any test importing state
        # modules before setting its own writes fixtures into the real
        # developer library. Explicitly-set dirs are honored.
        if not env.get("OPENBOX_DATA_DIR"):
            import tempfile as _tempfile

            env["OPENBOX_DATA_DIR"] = _tempfile.mkdtemp(prefix="openbox-gate-data.")
        for test_file in test_files:
            command = [str(COVERAGE), "run", "-p", str(test_file)]
            last_output = ""
            code = 1
            attempts_used = 0
            timed_out = False
            for attempt in range(1, TEST_ATTEMPTS + 1):
                attempts_used = attempt
                try:
                    result = subprocess.run(
                        command,
                        cwd=ROOT, capture_output=True, text=True,
                        check=False, env=env, timeout=TEST_TIMEOUT,
                    )
                except subprocess.TimeoutExpired as exc:
                    timed_out = True
                    code = 124
                    chunks = []
                    for stream in (exc.stdout, exc.stderr):
                        if stream:
                            chunks.append(stream if isinstance(stream, str) else stream.decode("utf-8", "replace"))
                    last_output = "\n".join(chunks)
                    break
                code = result.returncode
                last_output = (result.stdout or "") + (result.stderr or "")
                if code == 0:
                    break
            notes = _skip_notes(last_output)
            if notes:
                skip_total += len(notes)
                skip_files += 1
                for note in notes[:3]:
                    print(f"SKIP {test_file.name}: {note}")
            if timed_out:
                timeout_tests.append(test_file.name)
                print(f"TIMEOUT {test_file.name} after {TEST_TIMEOUT:.0f}s")
                if last_output.strip():
                    print("\n".join(last_output.strip().splitlines()[-20:]))
            elif code:
                failed_tests.append(test_file.name)
                print(f"FAIL {test_file.name}")
                if last_output.strip():
                    tail = last_output.strip().splitlines()[-40:]
                    print("\n".join(tail))
            elif attempts_used > 1:
                flaky_tests.append(test_file.name)
                print(f"FLAKY {test_file.name} (passed on attempt {attempts_used}/{TEST_ATTEMPTS}; retries hide a defect)")
            else:
                print(f"PASS {test_file.name}")
        passed_tests = len(test_files) - len(failed_tests) - len(timeout_tests)
        print(f"{passed_tests} test files passed, {len(failed_tests)} failed, {len(timeout_tests)} timed out")
        if skip_total:
            print(f"environment skips: {skip_total} note(s) across {skip_files} file(s)")
            if os.environ.get("OPENBOX_STRICT_SKIPS") == "1":
                failures.append(f"environment skips ({skip_total})")
        if failed_tests:
            failures.append("tests")
        if timeout_tests:
            failures.append(f"test timeouts ({', '.join(timeout_tests)})")
        if flaky_tests:
            failures.append(f"flaky tests ({', '.join(flaky_tests)})")
        gate_data_dir = env.get("OPENBOX_DATA_DIR", "")
        if gate_data_dir and "openbox-gate-data." in gate_data_dir:
            shutil.rmtree(gate_data_dir, ignore_errors=True)


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

            base = _git_diff_base()
            if base is None:
                print(_unresolved_base_message())
                failures.append("diff base unresolved")
            else:
                _check_changed_line_floor(COVERAGE, base, failures)
                _check_new_module_coverage(COVERAGE, base, failures)
                _check_touched_module_floor(failures)

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
