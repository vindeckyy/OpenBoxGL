#!/usr/bin/env python3
"""Webhook automation regression tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation import _attempt_delay  # noqa: E402


def test_retry_after_is_a_floor_clamped_to_30s():
    # Without a header the exponential schedule applies.
    assert _attempt_delay(0) == 1.0
    assert _attempt_delay(2) == 4.0
    # A server asking to wait longer must be honored, not shortened.
    assert _attempt_delay(0, "20") == 20.0
    # ...but never beyond the 30s clamp.
    assert _attempt_delay(0, "3600") == 30.0
    # A shorter Retry-After never undercuts the backoff schedule.
    assert _attempt_delay(3, "1") == 8.0
    # Garbage and non-positive values fall back to the schedule.
    assert _attempt_delay(1, "soon") == 2.0
    assert _attempt_delay(1, "-5") == 2.0
    print("  retry-after floor: ok")


def main():
    test_retry_after_is_a_floor_clamped_to_30s()


if __name__ == "__main__":
    main()
