#!/usr/bin/env python3
"""Performance no-regression gate (plan W7-04).

Compares two `scripts/perf_bench.py` reports (p95 milliseconds) and fails when a measurement
regressed against the baseline. A regression needs two conditions: the relative change is above
--max-regression percent, and the absolute change is above --min-delta-ms. The absolute floor
matters: a metric that moves from 0.3 ms to 0.4 ms is 33% worse and completely irrelevant, so a
ratio alone would fail builds on timer noise.

    python3 -B scripts/check_perf_regression.py --baseline base.json --current current.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_MAX_REGRESSION = 10.0
DEFAULT_MIN_DELTA_MS = 1.0
SIZES = ("10000", "20000")


def load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a perf report")
    return payload


def p95(report: dict, size: str, key: str):
    section = report.get(size)
    if not isinstance(section, dict):
        return None
    value = section.get(key)
    if isinstance(value, dict):
        value = value.get("p95_ms")
    return value if isinstance(value, (int, float)) else None


def compare(baseline: dict, current: dict) -> list[dict]:
    rows = []
    for size in SIZES:
        keys = set(baseline.get(size) or {}) & set(current.get(size) or {})
        for key in sorted(keys):
            before = p95(baseline, size, key)
            after = p95(current, size, key)
            if not isinstance(before, (int, float)) or not isinstance(after, (int, float)) or before <= 0:
                continue
            delta = after - before
            percent = delta / before * 100
            rows.append({
                "size": size,
                "key": key,
                "baseline_ms": round(before, 1),
                "current_ms": round(after, 1),
                "delta_ms": round(delta, 1),
                "percent": round(percent, 1),
            })
    return rows


def regressions(rows: list[dict], max_regression: float, min_delta_ms: float) -> list[dict]:
    return [
        row for row in rows
        if row["percent"] > max_regression and row["delta_ms"] > min_delta_ms
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--max-regression", type=float, default=DEFAULT_MAX_REGRESSION)
    parser.add_argument("--min-delta-ms", type=float, default=DEFAULT_MIN_DELTA_MS)
    args = parser.parse_args(argv)

    rows = compare(load(Path(args.baseline)), load(Path(args.current)))
    if not rows:
        print("FAIL: the two reports share no measurement")
        return 1
    failed = regressions(rows, args.max_regression, args.min_delta_ms)
    print("| size | key | baseline ms | current ms | delta ms | change |")
    print("|---|---|---:|---:|---:|---:|")
    for row in rows:
        print(f"| {row['size']} | {row['key']} | {row['baseline_ms']} | {row['current_ms']} | {row['delta_ms']} | {row['percent']:+.1f}% |")
    if failed:
        print(f"FAIL: {len(failed)} measurement(s) regressed more than {args.max_regression:g}% by more than {args.min_delta_ms:g} ms")
        for row in failed:
            print(f"  {row['size']} {row['key']}: {row['baseline_ms']} ms -> {row['current_ms']} ms ({row['percent']:+.1f}%)")
        return 1
    print(f"no regression above {args.max_regression:g}% and {args.min_delta_ms:g} ms across {len(rows)} measurements")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
