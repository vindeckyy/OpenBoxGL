"""Job isolation, politeness pacing, and mid-run cancellation tests for Deal Radar."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from pkg.parity import parity_deals as pd  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "deals"


class DealsJobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_per_entry_error_isolation_and_cancel_mid_run(self):
        state = {
            "deals": {
                "watchlist": [
                    {"id": "d1", "appid": "100", "name": "Bad Game", "manual": False},
                    {"id": "d2", "appid": "367520", "name": "Hollow Knight", "manual": False},
                    {"id": "d3", "appid": "730", "name": "CS2", "manual": False},
                ],
                "prices": {"d1": [], "d2": [], "d3": []},
                "last_poll_at": "",
            },
            "settings": {"deals_enabled": True, "deals_poll_hours": 168, "deals_min_drop_pct": 10},
        }
        sale_payload = json.loads((FIXTURES / "appdetails_sale.json").read_text(encoding="utf-8"))
        cancel = threading.Event()
        waits = []

        def fake_fetch(_dd, appid, **_kw):
            if appid == "100":
                raise OSError("temporary timeout")
            if appid == "367520":
                return pd._normalize_appdetails_payload(sale_payload, "367520", "us")
            raise AssertionError("Should stop before 3rd item once cancelled")

        original_wait = cancel.wait

        def spy_wait(timeout=None):
            waits.append(timeout)
            # Cancel right after processing item #2 so item #3 never starts
            if len(waits) == 2:
                cancel.set()
            return original_wait(0)

        cancel.wait = spy_wait  # type: ignore[method-assign]
        t0 = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(pd, "fetch_steam_appdetails", side_effect=fake_fetch):
            res = pd.refresh_deals_pass(
                self.data_dir,
                state,
                state["settings"],
                commit_fn=lambda fn: fn(state),
                cancel_event=cancel,
                spacing_seconds=1.0,
                now=t0,
            )

        self.assertTrue(res["cancelled"])
        self.assertEqual(res["polled"], 1)
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(state["deals"]["watchlist"][0]["fetch_error"], "temporary timeout")
        self.assertEqual(state["deals"]["watchlist"][1]["last_price_cents"], 749)
        self.assertEqual(waits, [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
