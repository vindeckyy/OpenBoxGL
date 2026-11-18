"""Unit tests for Deal Radar CRUD, fixture normalization, bounds, and durable drop alerts."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import DealsInvalidAppId, DealsLimitExceeded  # noqa: E402
from notifications import CAP as NOTICE_CAP, add_notification  # noqa: E402
from pkg.parity import parity_deals as pd  # noqa: E402
import state_store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "deals"


def _fake_opener_for(payload: dict):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()
            return False

    opener = mock.Mock()
    opener.open.side_effect = lambda *a, **kw: _Resp(json.dumps(payload).encode("utf-8"))
    return opener


class DealsUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.state = {
            "games": [
                {
                    "game_id": "g-hk",
                    "name": "Hollow Knight",
                    "steam_app_id": "367520",
                    "price_paid_cents": 1499,
                    "price_currency": "EUR",
                }
            ],
            "deals": {"watchlist": [], "prices": {}, "last_poll_at": ""},
            "notifications": [],
            "settings": {"deals_enabled": True, "deals_min_drop_pct": 10, "deals_poll_hours": 168},
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_shapes_cache_currency_passthrough_and_limits(self):
        search_payload = json.loads((FIXTURES / "storesearch.json").read_text(encoding="utf-8"))
        hits = pd.search_steam("hollow knight", cc="de", opener=_fake_opener_for(search_payload))
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0]["appid"], "1030300")

        sale_payload = json.loads((FIXTURES / "appdetails_sale.json").read_text(encoding="utf-8"))
        obs_sale = pd.fetch_steam_appdetails(self.data_dir, "367520", cc="us", opener=_fake_opener_for(sale_payload))
        self.assertEqual(obs_sale["status"], "priced")
        self.assertEqual(obs_sale["price_cents"], 749)
        self.assertEqual(obs_sale["discount_pct"], 50)
        self.assertEqual(obs_sale["currency"], "USD")

        free_payload = json.loads((FIXTURES / "appdetails_free.json").read_text(encoding="utf-8"))
        obs_free = pd.fetch_steam_appdetails(self.data_dir, "440", opener=_fake_opener_for(free_payload))
        self.assertEqual(obs_free["status"], "free")
        self.assertEqual(obs_free["price_cents"], 0)

        miss_payload = json.loads((FIXTURES / "appdetails_missing.json").read_text(encoding="utf-8"))
        obs_miss = pd.fetch_steam_appdetails(self.data_dir, "9999999", opener=_fake_opener_for(miss_payload))
        self.assertEqual(obs_miss["status"], "not_found")

        # Missing price_overview returns no_price cleanly
        no_price_obs = pd._normalize_appdetails_payload({"55": {"success": True, "data": {"name": "Soon"}}}, "55", "us")
        self.assertEqual(no_price_obs["status"], "no_price")

        # Mixed currency (EUR paid vs USD current) produces None paid_delta_cents (zero conversion)
        w = pd.watch_game(self.state, {"appid": "367520", "name": "Hollow Knight"})
        pd.apply_poll_observation(self.state, w["id"], obs_sale, self.state["settings"])
        listed = pd.list_deals(self.state)
        self.assertIsNone(listed["watchlist"][0]["paid_delta_cents"])

        # Appid validation & watchlist cap
        with self.assertRaises(DealsInvalidAppId):
            pd.validate_appid("not-digits")
        with mock.patch.object(pd, "MAX_WATCHLIST", 1):
            with self.assertRaises(DealsLimitExceeded):
                pd.watch_game(self.state, {"appid": "1030300", "name": "Silksong"})

    def test_first_poll_silent_both_drop_branches_history_cap_and_durable_dedupe(self):
        item = pd.watch_game(
            self.state,
            {"appid": "367520", "name": "Hollow Knight", "target_price_cents": 800, "note": "x" * 400},
        )
        self.assertEqual(len(item["note"]), pd.MAX_NOTE_LEN)

        t0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
        # First poll seeds silently
        r0 = pd.apply_poll_observation(
            self.state,
            item["id"],
            {"status": "priced", "price_cents": 1499, "discount_pct": 50, "currency": "USD"},
            self.state["settings"],
            now=t0,
        )
        self.assertFalse(r0["notified"])
        self.assertEqual(self.state["notifications"], [])

        # Target + drop notification triggers once
        r1 = pd.apply_poll_observation(
            self.state,
            item["id"],
            {"status": "priced", "price_cents": 749, "discount_pct": 50, "currency": "USD"},
            self.state["settings"],
            now=t0,
        )
        self.assertTrue(r1["notified"])
        self.assertEqual(item["last_notified_price_cents"], 749)

        # Evict feed past CAP=200 so dedupe_key disappears; polling 749 again STILL does not re-notify
        for i in range(NOTICE_CAP + 5):
            add_notification(self.state, kind="info", level="info", title=f"N{i}", body="")
        r2 = pd.apply_poll_observation(
            self.state,
            item["id"],
            {"status": "priced", "price_cents": 749, "discount_pct": 50, "currency": "USD"},
            self.state["settings"],
            now=t0,
        )
        self.assertFalse(r2["notified"])

        # History caps at MAX_HISTORY_POINTS=50 (oldest dropped)
        for idx in range(55):
            pd.record_manual_entry(self.state, {"id": item["id"], "price_cents": 700 + idx, "currency": "USD"})
        self.assertEqual(len(self.state["deals"]["prices"][item["id"]]), pd.MAX_HISTORY_POINTS)

        # Unwatch purges both entry and prices[id]
        pd.unwatch_game(self.state, item["id"])
        self.assertNotIn(item["id"], self.state["deals"]["prices"])
        self.assertEqual(self.state["deals"]["watchlist"], [])

    def test_manual_entries_never_polled_and_warn_and_repair_cleans_orphans(self):
        manual = pd.watch_game(self.state, {"name": "GOG Classic", "manual": True, "target_price_cents": 500})
        pd.record_manual_entry(self.state, {"id": manual["id"], "price_cents": 499, "currency": "EUR"})
        self.assertEqual(manual["last_price_cents"], 499)

        called = []
        res = pd.refresh_deals_pass(
            self.data_dir,
            self.state,
            self.state["settings"],
            commit_fn=lambda fn: fn(self.state),
            opener=mock.Mock(open=lambda *a, **kw: called.append(a)),
        )
        self.assertEqual(res["polled"], 0)
        self.assertEqual(called, [])

        # Inject orphan prices key and verify state_store warn-and-repair drops it cleanly
        self.state["deals"]["prices"]["ghost-id"] = [{"price_cents": 100}]
        state_store._normalize_22_additive_fields(self.state)
        self.assertNotIn("ghost-id", self.state["deals"]["prices"])


if __name__ == "__main__":
    unittest.main()
