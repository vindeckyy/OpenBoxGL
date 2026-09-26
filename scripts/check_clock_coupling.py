#!/usr/bin/env python3
"""Fail when a test pins a calendar date against a wall-clock code path.

Commit ``71d75ec`` fixed ``tests/test_parity_radio.py``: a fixture materialized
a playlist at a hardcoded ``NOW = 2026-09-12`` and asserted the handler *reused*
it, while the handler judged staleness with ``datetime.now()`` against
``RADIO_STALE_DAYS = 7``. The test passed for a week, then would have failed
the release gate forever. That was not bad luck; the tree is full of window
constants that make the same trap reachable.

This gate finds the pattern statically instead of waiting for the date:

1. A test module that pins a literal date/time constant (``NOW = "2026-.."``,
   ``FIXED = ...``) is **clock-coupled** when the module it exercises defines a
   rolling ``*_DAYS`` window, and it must carry a ``# clock-coupled`` marker
   naming the mitigation (an injected ``now`` or a relative fixture).
2. Marked modules are allowlisted in ``scripts/contracts/clock_coupling.json``
   with a reason, so the exemption is reviewed rather than forgotten.
3. New unmarked couplings fail the gate.

The marker convention is deliberate: the automated check cannot prove a fixture
is correct, so a human records why this particular coupling is safe.

Run directly:  python3 -B scripts/check_clock_coupling.py
See ADR 0049.
"""


from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TESTS = ROOT / "tests"
CONTRACT = ROOT / "scripts" / "contracts" / "clock_coupling.json"

# A test module that pins the calendar.
PINNED_CLOCK_RE = re.compile(
    r"""^\s*(NOW|FIXED_NOW|FIXED|TODAY|CURRENT_TIME|BASE_TIME|FROZEN_NOW)\s*[:=]""",
    re.MULTILINE,
)
# Rolling windows in the code under test.
WINDOW_DAYS_RE = re.compile(r"^[A-Z_]*_DAYS\s*[:=]\s*[0-9]", re.MULTILINE)
MARKER_RE = re.compile(r"#\s*clock-coupled", re.IGNORECASE)


def _module_under_test(text: str) -> set[str]:
    """Return the runtime modules a test imports from the repository."""
    names: set[str] = set()
    pattern = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE)
    for match in pattern.finditer(text):
        names.add(match.group(1))
    return names


def _runtime_windows() -> dict[str, set[str]]:
    """Map importable module name -> its rolling *_DAYS constants."""
    windows: dict[str, set[str]] = {}
    candidates = [ROOT / "web_app.py"]
    candidates += sorted((ROOT / "pkg" / "parity").glob("parity_*.py"))
    candidates += sorted((ROOT / "pkg" / "state").glob("*.py"))
    for path in candidates:
        if not path.is_file():
            continue
        found = set(WINDOW_DAYS_RE.findall(path.read_text(encoding="utf-8")))
        if not found:
            continue
        name = path.stem
        if name == "__init__":
            name = path.parent.name
        windows[name] = found
    return windows


def scan() -> list[str]:
    """Return one problem string per unmarked clock-coupled test module."""
    windows = _runtime_windows()
    if not windows:
        return ["no rolling *_DAYS windows found; the check may be misconfigured"]
    problems: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not PINNED_CLOCK_RE.search(text):
            continue
        imported = _module_under_test(text)
        coupled = sorted(
            f"{module}.{const.split('=')[0].strip().rstrip(':')}"
            for module, consts in windows.items()
            for const in consts
            if module in imported or module.replace("parity_", "") in imported
        )
        if not coupled:
            continue
        if MARKER_RE.search(text):
            continue
        problems.append(
            f"{path.name}: pins a calendar constant and exercises rolling window(s) "
            f"{', '.join(coupled)}; add a '# clock-coupled: <mitigation>' comment and "
            f"record the exemption in scripts/contracts/clock_coupling.json"
        )
    return problems



def main() -> int:
    # Validate the exemption ledger so it cannot rot into a blanket waiver.
    ledger_problems: list[str] = []
    if CONTRACT.is_file():
        try:
            data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            ledger_problems.append(f"{CONTRACT.relative_to(ROOT)} is not valid JSON: {exc}")
            data = {}
        for name, reason in sorted(data.get("exempt", {}).items()):
            if not str(reason).strip():
                ledger_problems.append(f"{name}: exemption has an empty reason")
            target = TESTS / name
            if not target.is_file():
                ledger_problems.append(f"{name}: exemption refers to a test file that no longer exists")
            elif not MARKER_RE.search(target.read_text(encoding="utf-8")):
                ledger_problems.append(
                    f"{name}: exemption is recorded but the test has no '# clock-coupled' marker"
                )
    else:
        ledger_problems.append(f"{CONTRACT.relative_to(ROOT)} is missing")

    problems = scan()
    if problems or ledger_problems:
        total = len(problems) + len(ledger_problems)
        print(f"FAIL: clock-coupled tests would break on a future date ({total}):")
        for line in problems + ledger_problems:
            print(f"  {line}")
        print("")
        print("A test that pins a date while the code under test reads datetime.now() passes")
        print("until the date passes it, then fails the release gate forever. Inject `now` or")
        print("materialize the fixture relative to now, then mark the file '# clock-coupled:'.")
        return 1

    print("clock coupling OK: no unmarked calendar-pinned tests exercise rolling windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())

