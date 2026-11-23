"""IGDB concurrency cap (2), 30-day cache TTL, and credentials-absent tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from pkg.parity import parity_itinerary as pi  # noqa: E402


class ItineraryIgdbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_creds_absent_makes_zero_igdb_calls(self):
        games = [{"game_id": f"g-{i}", "name": f"Game {i}", "platform": "PC"} for i in range(4)]
        with mock.patch("pkg.parity.parity_igdb.credentials", return_value=None):
            res = pi.fetch_missing_igdb_estimates(
                self.data_dir,
                games,
                search_fn=mock.Mock(side_effect=AssertionError("must not call IGDB")),
            )
        self.assertTrue(res["skipped_no_creds"])
        self.assertEqual(res["fetched"], 0)

    def test_concurrency_cap_two_error_isolation_and_ttl_cache(self):
        games = [{"game_id": f"g-{i}", "name": f"Game {i}", "platform": "PC"} for i in range(6)]
        lock = threading.Lock()
        active = 0
        max_seen = 0

        def fake_search(name, _plat, limit=1):
            nonlocal active, max_seen
            with lock:
                active += 1
                max_seen = max(max_seen, active)
            try:
                time.sleep(0.02)
                if name == "Game 2":
                    raise OSError("network flake")
                return [{"igdb_id": int(name.split()[-1]) + 100}]
            finally:
                with lock:
                    active -= 1

        def fake_ttb(igdb_id):
            return 14.0 if igdb_id != 103 else None

        t0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        with mock.patch("pkg.parity.parity_igdb.credentials", return_value=("cid", "csec")):
            res = pi.fetch_missing_igdb_estimates(
                self.data_dir,
                games,
                search_fn=fake_search,
                fetch_ttb_fn=fake_ttb,
                now=t0,
            )

        self.assertLessEqual(max_seen, 2)
        self.assertEqual(res["fetched"], 4)
        self.assertEqual(len(res["errors"]), 1)

        # Within 30d TTL, Game 0 hits disk cache and yields "igdb"; None result for Game 3 degrades honestly
        est_hit = pi.estimate_game_hours(games[0], data_dir=self.data_dir, now=t0 + timedelta(days=5))
        self.assertEqual(est_hit["est_source"], "igdb")
        self.assertEqual(est_hit["est_hours"], 14.0)

        est_miss = pi.estimate_game_hours(games[3], data_dir=self.data_dir, now=t0 + timedelta(days=5))
        self.assertEqual(est_miss["est_source"], "default")

        # Expired (>30 days via stat mtime) no longer counts as fresh cache hit
        cache_file = self.data_dir / "cache" / "igdb" / "g-0.json"
        old_ts = (t0 - timedelta(days=31)).timestamp()
        import os
        os.utime(cache_file, (old_ts, old_ts))
        hit, _ = pi.read_igdb_cached_hours(self.data_dir, "g-0", now=t0)
        self.assertFalse(hit)


if __name__ == "__main__":
    unittest.main()
