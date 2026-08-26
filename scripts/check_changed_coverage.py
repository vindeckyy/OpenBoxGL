#!/usr/bin/env python3
"""Measure changed-line and touched-module coverage from a combined .coverage file.

Reads git diff against a merge base, intersects changed Python lines with
coverage executed lines, and fails when the ratio is below --fail-under.
Also fails when any touched included module has 0% coverage.

Typical usage after scripts/check_tests.py:

  python3 -B scripts/check_changed_coverage.py --fail-under=95
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(command: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _diff_base(ref: str | None) -> str:
    if ref:
        return ref.strip()
    upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        merge = _run(["git", "merge-base", "HEAD", upstream.stdout.strip()])
        if merge.returncode == 0 and merge.stdout.strip():
            return merge.stdout.strip()
    head = _run(["git", "rev-parse", "HEAD"])
    return head.stdout.strip() if head.returncode == 0 else "HEAD"


def _changed_python_files(base: str) -> list[str]:
    diff = _run(["git", "diff", "--name-only", f"{base}...HEAD", "--", "*.py"])
    if diff.returncode != 0:
        diff = _run(["git", "diff", "--name-only", base, "HEAD", "--", "*.py"])
    files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return sorted(files)


def _changed_line_numbers(base: str, rel_path: str) -> set[int]:
    diff = _run(["git", "diff", "-U0", f"{base}...HEAD", "--", rel_path])
    if diff.returncode != 0:
        diff = _run(["git", "diff", "-U0", base, "HEAD", "--", rel_path])
    lines: set[int] = set()
    current_file = None
    for raw in diff.stdout.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        if current_file != rel_path:
            continue
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", raw)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count == 0:
            continue
        lines.update(range(start, start + count))
    return lines


def _load_coverage():
    from coverage import Coverage

    data_path = ROOT / ".coverage"
    if not data_path.is_file():
        combined = _run(["coverage", "combine", "--quiet"])
        if combined.returncode != 0:
            raise RuntimeError("missing .coverage and coverage combine failed")
    cov = Coverage(data_file=str(data_path))
    cov.load()
    return cov


def _module_percent(cov, rel_path: str) -> float | None:
    abs_path = str((ROOT / rel_path).resolve())
    try:
        _filename, statements, _excluded, missing, _missing_formatted = cov.analysis2(abs_path)
    except Exception:
        return None
    if not statements:
        return None
    hit = len(set(statements) - set(missing))
    return 100.0 * hit / len(set(statements))


def measure_changed_lines(base: str | None = None, include: set[str] | None = None) -> tuple[int, int, list[str]]:
    """Return (hit, total_executable_changed, changed_files)."""
    resolved_base = _diff_base(base)
    changed_files = _changed_python_files(resolved_base)
    if include is not None:
        changed_files = [path for path in changed_files if path in include]
    if not changed_files:
        return 0, 0, changed_files

    cov = _load_coverage()
    total_changed = 0
    total_hit = 0
    for rel_path in changed_files:
        abs_path = str((ROOT / rel_path).resolve())
        changed_lines = _changed_line_numbers(resolved_base, rel_path)
        if not changed_lines:
            continue
        try:
            _filename, statements, _excluded, missing, _missing_formatted = cov.analysis2(abs_path)
        except Exception:
            total_changed += len(changed_lines)
            continue
        relevant = changed_lines & set(statements)
        if not relevant:
            continue
        hit = relevant - set(missing)
        total_changed += len(relevant)
        total_hit += len(hit)
    return total_hit, total_changed, changed_files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include", default="", help="comma-separated .py paths to limit scope")
    parser.add_argument("--fail-under", type=float, default=95.0, help="minimum changed-line percent")
    parser.add_argument("--diff-base", default=None, help="git ref for diff base")
    args = parser.parse_args(argv)

    include = {item.strip() for item in args.include.split(",") if item.strip()}
    base = _diff_base(args.diff_base)
    changed_files = _changed_python_files(base)
    if include:
        changed_files = [path for path in changed_files if path in include]
    if not changed_files:
        print(f"changed-line coverage: no Python changes since {base}; pass")
        return 0

    try:
        cov = _load_coverage()
    except Exception as exc:
        print(f"check_changed_coverage: {exc}", file=sys.stderr)
        return 1

    failures: list[str] = []
    total_changed = 0
    total_hit = 0

    for rel_path in changed_files:
        abs_path = str((ROOT / rel_path).resolve())
        changed_lines = _changed_line_numbers(base, rel_path)
        if not changed_lines:
            continue
        try:
            _filename, statements, _excluded, missing, _missing_formatted = cov.analysis2(abs_path)
        except Exception:
            failures.append(f"{rel_path}: not measured by coverage")
            total_changed += len(changed_lines)
            continue

        stmt_set = set(statements)
        miss_set = set(missing)
        relevant = changed_lines & stmt_set
        if not relevant:
            continue
        hit = relevant - miss_set
        total_changed += len(relevant)
        total_hit += len(hit)
        module_pct = _module_percent(cov, rel_path)
        if module_pct is not None and module_pct <= 0.0:
            failures.append(f"{rel_path}: touched module at 0%")

    if total_changed == 0:
        print(f"changed-line coverage: no executable changed lines since {base}; pass")
    else:
        pct = 100.0 * total_hit / total_changed
        print(f"changed-line coverage: {total_hit}/{total_changed} = {pct:.1f}% (floor {args.fail_under:.1f}%)")
        if pct < args.fail_under:
            failures.append(
                f"changed-line coverage {pct:.1f}% below floor {args.fail_under:.1f}%"
            )

    touched_include = ",".join(changed_files)
    report = _run(
        [
            "coverage",
            "report",
            f"--include={touched_include}",
            f"--fail-under={int(args.fail_under)}",
        ]
    )
    if report.stdout.strip():
        print(report.stdout.strip())
    if report.stderr.strip():
        print(report.stderr.strip(), file=sys.stderr)
    if report.returncode != 0:
        failures.append("touched-module coverage below fail-under")

    if failures:
        print("CHANGED COVERAGE FAILED:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("CHANGED COVERAGE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
