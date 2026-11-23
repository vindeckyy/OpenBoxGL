"""Privacy, host/redirect allowlist, and zero-outbound-when-disabled tests."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from api_errors import DealsHostBlocked  # noqa: E402
from pkg.parity import parity_compat  # noqa: E402
from pkg.parity import parity_deals as pd  # noqa: E402


class DealsPrivacyTests(unittest.TestCase):
    def test_host_allowlist_and_redirect_handler_block_non_steam(self):
        pd.validate_allowlisted_url("https://store.steampowered.com/api/appdetails?appids=367520")
        for blocked in (
            "http://store.steampowered.com/api/appdetails",
            "https://evil.example.com/api/appdetails",
            "https://api.steampowered.com/ISteamApps/GetAppList/v2",
            "https://store.steampowered.com@evil.example.com/",
            "file:///etc/passwd",
        ):
            with self.assertRaises(DealsHostBlocked):
                pd.validate_allowlisted_url(blocked)

        handler = pd._AllowlistedRedirectHandler()
        req = urllib.request.Request("https://store.steampowered.com/api/appdetails")
        with self.assertRaises(DealsHostBlocked):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/steal")

    def test_disabled_makes_zero_socket_calls_and_compat_reports_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state = {
                "games": [],
                "deals": {"watchlist": [{"id": "d1", "appid": "367520", "manual": False}], "prices": {}},
                "settings": {"deals_enabled": False},
            }
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network must not be called")), mock.patch(
                "urllib.request.OpenerDirector.open", side_effect=AssertionError("opener must not be called")
            ):
                self.assertFalse(pd.deals_due(state, state["settings"]))
                res = pd.refresh_deals_pass(data_dir, state, state["settings"], commit_fn=lambda fn: fn(state))
                self.assertEqual(res["polled"], 0)

            (data_dir / "library.json").write_text('{"settings":{"deals_enabled":false},"deals":{"watchlist":[{"id":"d1"}]}}', encoding="utf-8")
            report = parity_compat.build_report(data_dir, root=ROOT)
            self.assertEqual(report["deals"]["enabled"], False)
            self.assertEqual(report["deals"]["host_allowlist"], ["store.steampowered.com"])
            self.assertEqual(report["deals"]["payload"], "appid_and_search_query_only")
            self.assertEqual(report["deals"]["watchlist_count"], 1)


if __name__ == "__main__":
    unittest.main()
