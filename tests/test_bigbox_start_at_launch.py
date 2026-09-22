"""F5a backend slice: --bigbox launch flag and the bigbox_start_at_launch setting."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from handlers.settings import clean_settings  # noqa: E402
from pkg.state.cache import _public_settings_uncached  # noqa: E402
from settings_schema import KNOWN_SETTINGS  # noqa: E402
from web_app import bigbox_launch_url  # noqa: E402


class BigBoxLaunchUrlTests(unittest.TestCase):
    def test_bigbox_flag_appends_deeplink(self):
        with mock.patch.object(sys, "argv", ["web_app.py", "--bigbox"]):
            url = bigbox_launch_url("http://127.0.0.1:1234/?token=abc")
        self.assertEqual(url, "http://127.0.0.1:1234/?token=abc&deeplink=bigbox")

    def test_no_flag_leaves_url_untouched(self):
        with mock.patch.object(sys, "argv", ["web_app.py"]):
            url = bigbox_launch_url("http://127.0.0.1:1234/?token=abc")
        self.assertEqual(url, "http://127.0.0.1:1234/?token=abc")


class BigBoxStartAtLaunchSettingTests(unittest.TestCase):
    def test_key_is_registered(self):
        self.assertIn("bigbox_start_at_launch", KNOWN_SETTINGS)

    def test_clean_settings_normalizes_to_bool(self):
        self.assertIs(clean_settings({})["bigbox_start_at_launch"], False)
        self.assertIs(clean_settings({"bigbox_start_at_launch": 1})["bigbox_start_at_launch"], True)

    def test_public_settings_exposes_it(self):
        exposed = _public_settings_uncached({"settings": {"bigbox_start_at_launch": True}})
        self.assertIs(exposed["bigbox_start_at_launch"], True)
        self.assertIs(_public_settings_uncached({"settings": {}})["bigbox_start_at_launch"], False)


if __name__ == "__main__":
    unittest.main()
