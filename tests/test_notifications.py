"""Tests for translated server notifications and their locale coverage."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notifications import SERVER_NOTICE_KEYS, add_notification  # noqa: E402

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def flatten(value, prefix=""):
    out = {}
    if not isinstance(value, dict):
        return out
    for key, item in value.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            out.update(flatten(item, full))
        else:
            out[full] = item
    return out


class ServerNoticeTests(unittest.TestCase):
    def test_keys_and_params_are_stored_and_bounded(self):
        state = {}
        item = add_notification(
            state,
            kind="cloud_sync",
            level="info",
            title="Cloud sync succeeded",
            body="Synced 2 games.",
            title_key="notify.server.cloud_sync_ok_title",
            body_key="notify.server.cloud_sync_ok_body",
            params={"count": 2, "detail": "x" * 500, "nested": {"a": 1}, 1: "skip"},
            now="2026-10-18T00:00:00+00:00",
        )
        self.assertEqual(item["title_key"], "notify.server.cloud_sync_ok_title")
        self.assertEqual(item["body_key"], "notify.server.cloud_sync_ok_body")
        self.assertEqual(item["params"]["count"], 2)
        self.assertEqual(len(item["params"]["detail"]), 200)
        self.assertEqual(item["params"]["nested"], "{'a': 1}")
        self.assertNotIn(1, item["params"])

    def test_old_notification_payload_stays_valid(self):
        state = {}
        item = add_notification(
            state, kind="system", level="info", title="Old", body="Body",
        )
        self.assertNotIn("title_key", item)
        self.assertNotIn("params", item)

    def test_every_locale_has_every_server_notice_key(self):
        locales = {}
        for path in sorted((ROOT / "locales").glob("*.json")):
            locales[path.stem] = flatten(json.loads(path.read_text(encoding="utf-8")))
        for key, placeholders in SERVER_NOTICE_KEYS.items():
            self.assertIn(key, locales["en"], key)
            for locale, strings in locales.items():
                self.assertIn(key, strings, f"{locale} is missing {key}")
                self.assertEqual(
                    set(PLACEHOLDER_RE.findall(strings[key])),
                    set(placeholders),
                    f"{locale}:{key} placeholders drifted",
                )


    def test_whats_new_highlights_have_locale_keys(self):
        source = (ROOT / "static" / "whatsnew.js").read_text(encoding="utf-8")
        names = re.search(r"const HIGHLIGHTS = \[([^\]]+)\]", source).group(1)
        highlights = re.findall(r"'([^']+)'", names)
        self.assertTrue(highlights)
        for path in sorted((ROOT / "locales").glob("*.json")):
            strings = flatten(json.loads(path.read_text(encoding="utf-8")))
            for name in highlights:
                self.assertIn(f"whats_new.{name}_title", strings, f"{path.stem} missing {name} title")
                self.assertIn(f"whats_new.{name}_body", strings, f"{path.stem} missing {name} body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
