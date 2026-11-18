"""Unit tests for Player's Log time capsules (redaction, bounds, exactly-once unlock)."""

from __future__ import annotations

from datetime import date
import sys
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import (  # noqa: E402
    BadRequest,
    CapsuleBodyTooLarge,
    CapsuleInvalidDate,
    CapsuleLocked,
    CapsuleNotFound,
)
from notifications import CAP as NOTICE_CAP, add_notification  # noqa: E402
from pkg.parity import parity_playerlog as pl  # noqa: E402


class PlayerLogCapsuleTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "games": [{"game_id": "g-1", "name": "Chrono Trigger", "platform": "SNES"}],
            "time_capsules": [],
            "notifications": [],
            "ui_state": {},
        }

    def test_bounds_and_invalid_dates(self):
        with self.assertRaises(CapsuleBodyTooLarge):
            pl.create_capsule(
                self.state,
                {"title": "Too long", "body": "x" * 4001, "unlock_at": "2027-01-01"},
            )
        with self.assertRaises(BadRequest):
            pl.create_capsule(
                self.state,
                {"title": "t" * 121, "body": "ok", "unlock_at": "2027-01-01"},
            )
        for bad_date in ("", "2026-02-29", "2027/01/01", "not-a-date"):
            with self.assertRaises(CapsuleInvalidDate):
                pl.create_capsule(
                    self.state,
                    {"title": "Date check", "body": "hi", "unlock_at": bad_date},
                )
        # Leap-day 2028-02-29 is valid and works seamlessly
        leap = pl.create_capsule(
            self.state,
            {"title": "Leap memory", "body": "2028 note", "kind": "question", "unlock_at": "2028-02-29"},
            today=date(2026, 11, 17),
        )
        self.assertEqual(leap["unlock_at"], "2028-02-29")
        self.assertNotIn("body", leap)

    def test_strict_redaction_idempotent_open_and_delete_while_locked(self):
        cap = pl.create_capsule(
            self.state,
            {
                "game_id": "g-1",
                "title": "Boss thoughts",
                "body": "Secret impression",
                "unlock_at": "2026-12-01",
            },
            today=date(2026, 11, 17),
        )
        self.assertNotIn("body", cap)
        listed = pl.list_capsules(self.state, today=date(2026, 11, 17))
        self.assertNotIn("body", listed["capsules"][0])
        nxt = pl.next_capsule(self.state, today=date(2026, 11, 28))
        self.assertNotIn("body", nxt["capsule"])
        self.assertEqual(nxt["days_until"], 3)

        # Locked before midnight on unlock_at
        with self.assertRaises(CapsuleLocked):
            pl.open_capsule(self.state, cap["id"], today=date(2026, 11, 30))

        # Opens on unlock_at and returns body idempotently even if clock moves backward
        opened1 = pl.open_capsule(self.state, cap["id"], today=date(2026, 12, 1), now_iso="2026-12-01T00:00:01Z")
        self.assertEqual(opened1["body"], "Secret impression")
        opened2 = pl.open_capsule(self.state, cap["id"], today=date(2026, 11, 15))
        self.assertEqual(opened2["body"], "Secret impression")
        self.assertEqual(opened2["opened_at"], "2026-12-01T00:00:01Z")

        # Game deletion preserves capsule label and record
        self.state["games"].clear()
        after_game_del = pl.list_capsules(self.state, today=date(2026, 12, 1))
        self.assertEqual(len(after_game_del["capsules"]), 1)
        self.assertEqual(after_game_del["capsules"][0]["game_name"], "Chrono Trigger")

        # Delete works both locked and unlocked
        sealed = pl.create_capsule(self.state, {"title": "Locked", "body": "b", "unlock_at": "2030-01-01"})
        res = pl.delete_capsule(self.state, sealed["id"])
        self.assertTrue(res["deleted"])
        with self.assertRaises(CapsuleNotFound):
            pl.delete_capsule(self.state, sealed["id"])

    def test_exactly_once_unlock_even_after_notice_eviction_and_backward_clock(self):
        pl.create_capsule(
            self.state,
            {"title": "Future self", "body": "Keep enjoying RPGs", "unlock_at": "2026-11-20"},
            today=date(2026, 11, 17),
        )
        # Due on 2026-11-20
        u1 = pl.unlock_due_capsules(self.state, today=date(2026, 11, 20), now_iso="2026-11-20T01:00:00Z")
        self.assertEqual(len(u1), 1)
        self.assertEqual(u1[0]["body"], "Keep enjoying RPGs")
        cap_notices = [n for n in self.state["notifications"] if n.get("kind") == "capsule"]
        self.assertEqual(len(cap_notices), 1)

        # Evict every notification from the capped CAP=200 feed so dedupe_key disappears
        for idx in range(NOTICE_CAP + 10):
            add_notification(self.state, kind="info", level="info", title=f"Notice {idx}", body="")
        self.assertFalse(any(n.get("kind") == "capsule" for n in self.state["notifications"]))

        # Running unlock sweep again (or after backward clock shift) NEVER duplicates
        u2 = pl.unlock_due_capsules(self.state, today=date(2026, 11, 25))
        u3 = pl.unlock_due_capsules(self.state, today=date(2026, 11, 18))
        self.assertEqual(u2, [])
        self.assertEqual(u3, [])
        self.assertFalse(any(n.get("kind") == "capsule" for n in self.state["notifications"]))

    def test_validation_leap_year_manual_sessions_and_trashed_shapes(self):
        # Input validation branches
        with self.assertRaises(BadRequest):
            pl.create_capsule(self.state, "bad")  # type: ignore[arg-type]
        with self.assertRaises(BadRequest):
            pl.create_capsule(self.state, {"title": "Empty", "body": "   ", "unlock_at": "2027-01-01"})
        with self.assertRaises(BadRequest):
            pl.create_capsule(self.state, {"title": "Bad kind", "body": "ok", "kind": "poll", "unlock_at": "2027-01-01"})

        # Leap-day Feb 29 anniversary rolls cleanly to Feb 28 in non-leap year
        self.assertEqual(pl._add_years(date(2024, 2, 29), 1), date(2025, 2, 28))
        self.assertEqual(pl._add_years(date(2024, 2, 29), 4), date(2028, 2, 29))
        self.assertIsNone(pl._parse_date_prefix("bad-prefix"))
        self.assertEqual(pl._binary_search_crossing([(date(2026, 1, 1), 0)], 10, "2026-01-01"), ("2026-01-01", True))

        # Game name fallback + manual_sessions + nested & string trash formats
        st = {
            "games": [
                {
                    "game_id": "g-manual",
                    "name": "Hollow Knight",
                    "added_at": "2024-02-29",
                    "playtime_seconds": 15 * 3600,
                    "manual_sessions": [{"date": "2025-05-01", "seconds": 15 * 3600}],
                },
                {"game_id": "g-tr1", "name": "Tr1", "playtime_seconds": 50 * 3600},
                {"game_id": "g-tr2", "name": "Tr2", "playtime_seconds": 50 * 3600},
            ],
            "history": [{"game": "Hollow Knight", "started": "2025-06-01T12:00:00", "seconds": 3600}],
            "trash": ["g-tr1", {"game": {"game_id": "g-tr2"}}],
            "ui_state": {},
        }
        ms = pl.compute_milestones(st, today=date(2026, 6, 1))
        ids = {m["id"] for m in ms["unlocked"]}
        self.assertIn("m:playtime_game:g-manual:10h", ids)
        self.assertIn("m:library_age:1y", ids)
        self.assertNotIn("m:playtime_game:g-tr1:10h", ids)
        self.assertNotIn("m:playtime_game:g-tr2:10h", ids)


if __name__ == "__main__":
    unittest.main()
