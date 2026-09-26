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
See ADR 0060.
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


_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\s+([^\n#]*)|import\s+([^\n#]*))", re.MULTILINE
)


def _modules_under_test(text: str) -> set[str]:
    """Return the leaf module names a test imports from the repository.

    Tests reach runtime code through several shapes, so every dotted identifier
    in an import statement counts: ``import parity_radio``,
    ``from pkg.parity import parity_radio``, ``from state_store import
    JsonStateStore``, ``from pkg.parity.parity_backup import ...``. Comparing a
    bare module name against a dotted path is why this gate found nothing.
    """
    names: set[str] = set()
    for source, from_clause, plain in _IMPORT_RE.findall(text):
        candidates: list[str] = []
        if source:
            candidates.append(source)
        for clause in (from_clause, plain):
            if clause:
                candidates.extend(part.split(" as ")[0] for part in clause.split(","))
        for candidate in candidates:
            candidate = candidate.strip()
            if re.fullmatch(r"[A-Za-z_][\w.]*", candidate):
                names.add(candidate.rsplit(".", 1)[-1])
    return names


def _runtime_modules() -> list[Path]:
    """Every module the runtime ships, taken from the canonical manifest.

    Root-level modules such as ``state_store.py`` carry rolling windows too, and
    an ad-hoc glob of a few directories keeps missing them as the tree grows.
    runtime_modules.txt is already kept complete by its own gate.
    """
    manifest = ROOT / "runtime_modules.txt"
    if not manifest.is_file():
        return []
    listed = [ROOT / line.strip() for line in manifest.read_text(encoding="utf-8").splitlines()]
    # The manifest also names shipped non-Python files (the release public key).
    return [path for path in listed if path.suffix == ".py" and path.is_file()]


def _runtime_windows() -> dict[str, set[str]]:
    """Map importable module name -> its rolling *_DAYS constants."""
    windows: dict[str, set[str]] = {}
    for path in _runtime_modules():
        found = set(WINDOW_DAYS_RE.findall(path.read_text(encoding="utf-8")))
        if found:
            windows[path.stem] = found
    return windows


def _exemptions() -> dict[str, str]:
    """Return the reviewed clock-coupling exemptions."""
    if not CONTRACT.is_file():
        return {}
    try:
        return dict(json.loads(CONTRACT.read_text(encoding="utf-8")).get("exempt", {}))
    except json.JSONDecodeError:
        return {}


def scan() -> list[str]:
    """Return one problem string per unmarked or unreviewed clock-coupled test."""
    windows = _runtime_windows()
    if not windows:
        return ["no rolling *_DAYS windows found; the check may be misconfigured"]
    exemptions = _exemptions()
    problems: list[str] = []
    for path in sorted(TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not PINNED_CLOCK_RE.search(text):
            continue
        imported = _modules_under_test(text)
        coupled = sorted(
            f"{module}.{const.split('=')[0].strip().rstrip(':')}"
            for module, consts in windows.items()
            for const in consts
            if module in imported or module.replace("parity_", "") in imported
        )
        if not coupled:
            continue
        if MARKER_RE.search(text):
            # A marker alone must not exempt itself: the point of the ledger is
            # that a human reviewed why this coupling is safe.
            if path.name not in exemptions:
                problems.append(
                    f"{path.name}: carries a '# clock-coupled' marker but has no entry in "
                    f"{CONTRACT.name}; an exemption has to be recorded to be reviewed"
                )
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

