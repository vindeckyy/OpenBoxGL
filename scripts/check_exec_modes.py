#!/usr/bin/env python3
"""Fail when a tracked file carries a shebang but not the exec bit.

The CI Lint step runs ``ruff check .`` on Linux, where EXE001 rejects any file
with a ``#!`` line that is not mode 100755. Commit ``f605e27`` landed eight
such files -- seven ratchet scripts and one test -- and the Lint step stayed red
on every commit after it, in both the x86_64 and aarch64 gate jobs.

The failure is invisible from a Windows checkout, which is the trap: there is no
exec bit there, so ruff cannot report EXE001 and every local gate stays green
while CI is red. The git *index* does record the mode on every platform, so this
gate reads the index rather than the filesystem, and fails wherever it runs.

Checked: every tracked ``*.py`` and ``*.sh`` whose first line is a shebang. The
rule is deliberately not limited to ``*.sh`` -- the eight that broke were mostly
Python, and a shebang that does nothing is either an exec-bit bug now or a
"run me directly" claim that is quietly false.

Run directly:  python3 -B scripts/check_exec_modes.py
See ADR 0060 (gates must be true, not merely present).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The exec bit is a contract for anything a human or a CI step may invoke
# directly, so it applies to Python entry points as well as shell scripts.
SHEBANG_SUFFIXES = (".py", ".sh")

# The only mode git uses for a regular file; 120000 is a symlink, 160000 a gitlink.
REGULAR_EXECUTABLE = "100755"


def tracked_modes() -> dict[str, str]:
    """Map tracked path -> index mode, read straight from the git index."""
    result = subprocess.run(
        ["git", "ls-files", "-s", "-z"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    if result.returncode != 0:
        print("could not read the git index (git ls-files -s failed)", file=sys.stderr)
        raise SystemExit(2)
    modes: dict[str, str] = {}
    # With -z the records are NUL-separated: "<mode> <sha> <stage>\t<path>".
    # The mode is always the FIRST field; reading a different index is how a
    # first draft of this gate flagged all 102 shebang files in the repo.
    for record in result.stdout.split("\0"):
        if not record.strip():
            continue
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if path and parts:
            modes[path] = parts[0]
    return modes


def has_shebang(root: Path, path: str) -> bool:
    """True when the file's first line is a shebang."""
    try:
        with (root / path).open("rb") as handle:
            first = handle.readline(64)
    except OSError:
        return False
    return first.startswith(b"#!")


def find_offenders(
    modes: dict[str, str],
    root: Path = ROOT,
    suffixes: tuple[str, ...] = SHEBANG_SUFFIXES,
) -> list[tuple[str, str]]:
    """Tracked files that carry a shebang but are not mode 100755.

    The suffix set is a parameter rather than an inline branch so a test can
    narrow it: a gate that quietly stopped checking .py files would be the
    same class of silent regression this gate exists to prevent, and that is
    exactly the mistake that is invisible without an assertion on the input.
    """
    offenders: list[tuple[str, str]] = []
    for path, mode in sorted(modes.items()):
        if not path.endswith(suffixes):
            continue
        if has_shebang(root, path) and mode != REGULAR_EXECUTABLE:
            offenders.append((path, mode))
    return offenders


def main() -> int:
    offenders = find_offenders(tracked_modes())

    if not offenders:
        print("exec modes OK: every shebang file is mode 100755")
        return 0

    print(f"FAIL: {len(offenders)} shebang file(s) are not executable.\n")
    print("  ruff EXE001 fails the CI Lint step on Linux for each of these, and")
    print("  nothing local can catch it from a Windows checkout. Fix with:\n")
    for path, mode in offenders:
        print(f"    git update-index --chmod=+x {path}   (currently {mode})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
