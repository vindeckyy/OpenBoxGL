"""Contract tests for queue, tags, notifications, and webhook primitives."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import automation

from automation import EVENT_TYPES, build_event, sign_event, validate_webhook
from catalog import apply_tag_changes, bulk_update, normalize_tags, tag_counts
from notifications import add_notification, clear, mark_read, unread_count
from play_queue import advance, enqueue, normalize_queue, remove, reorder, resolve_queue


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.state = {"games": [
            {"game_id": "g1", "name": "Alpha", "platform": "PC", "path": "/bin/true"},
            {"game_id": "g2", "name": "Beta", "path": ""},
            {"game_id": "g3", "name": "Gamma", "path": "/bin/true"},
        ], "queue": []}

    def test_enqueue_resolve_advance_and_remove(self):
        enqueue(self.state, ["g1", "g2", "g3"], note="later")
        resolved = resolve_queue(self.state)
        self.assertEqual([item["name"] for item in resolved], ["Alpha", "Beta", "Gamma"])
        self.assertEqual(advance(self.state)["game_id"], "g1")
        self.assertEqual(advance(self.state, "g1")["game_id"], "g3")
        self.assertEqual(remove(self.state, ["g2"]), 1)

    def test_advance_persists_skips_before_returning_valid_item(self):
        # Skip flags recorded while scanning must reach state even when a
        # later valid entry is returned.
        state = {"games": [
            {"game_id": "g1", "name": "Broken", "path": ""},
            {"game_id": "g2", "name": "OK", "path": "/bin/true"},
        ], "queue": [
            {"game_id": "g1", "skip": False},
            {"game_id": "g2", "skip": False},
        ]}
        self.assertEqual(advance(state)["game_id"], "g2")
        self.assertTrue(state["queue"][0]["skip"])
        self.assertFalse(state["queue"][1]["skip"])

    def test_resolve_queue_path_exists_checks_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real.iso"
            real.write_bytes(b"x")
            state = {"games": [
                {"game_id": "g1", "path": str(real)},
                {"game_id": "g2", "path": "/does/not/exist.iso"},
            ], "queue": [{"game_id": "g1"}, {"game_id": "g2"}]}
            resolved = resolve_queue(state)
            self.assertTrue(resolved[0]["path_exists"])
            self.assertFalse(resolved[1]["path_exists"])

    def test_reorder_rejects_partial_queue(self):
        enqueue(self.state, ["g1", "g3"])
        with self.assertRaises(ValueError):
            reorder(self.state, ["g1"])
        reorder(self.state, ["g3", "g1"])
        self.assertEqual([item["game_id"] for item in self.state["queue"]], ["g3", "g1"])

    def test_normalize_queue_bounds_and_types(self):
        self.assertEqual(normalize_queue("bad"), [])
        self.assertEqual(normalize_queue([{"game_id": "", "note": "x"}, {"game_id": "g1"}])[0]["game_id"], "g1")


class TagTests(unittest.TestCase):
    def test_normalization_and_case_insensitive_changes(self):
        self.assertEqual(normalize_tags(["  RPG  ", "rpg", "Action"]), ["RPG", "Action"])
        game = {"tags": ["RPG", "Story"]}
        self.assertTrue(apply_tag_changes(game, add=["rpg", "Arcade"], remove=["story"]))
        self.assertEqual(game["tags"], ["RPG", "Arcade"])

    def test_bulk_tags_and_counts_ignore_hidden_games(self):
        games = [{"game_id": "g1", "tags": ["RPG"]}, {"game_id": "g2", "tags": ["rpg", "Arcade"]}, {"game_id": "g3", "tags": ["Hidden"], "hidden": True}]
        self.assertEqual(bulk_update(games, ["g1"], {"tags_add": ["Arcade"]}), 1)
        self.assertEqual(tag_counts(games), [{"tag": "Arcade", "count": 2}, {"tag": "RPG", "count": 2}])


class NotificationTests(unittest.TestCase):
    def test_dedupe_read_and_clear(self):
        state = {"notifications": []}
        first = add_notification(state, kind="webhook", level="error", title="Failed", body="x", dedupe_key="evt-1", now="2026-01-01T00:00:00+00:00")
        second = add_notification(state, kind="webhook", level="error", title="Failed", body="y", dedupe_key="evt-1", now="2026-01-01T00:00:01+00:00")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(unread_count(state), 1)
        mark_read(state)
        self.assertEqual(unread_count(state), 0)
        clear(state)
        self.assertEqual(state["notifications"], [])


class WebhookTests(unittest.TestCase):
    def test_delivery_uses_injected_clock_for_retry_timing(self):
        from automation import WebhookDispatcher
        calls = []

        def clock():
            calls.append(1)
            return len(calls) * 10.0

        def opener(request, timeout=0):
            # Always retryable: forces the retry sleep path.
            return type("Response", (), {
                "status": 429,
                "headers": {"Retry-After": ""},
                "__enter__": lambda self: self,
                "__exit__": lambda self, *_: False,
            })()

        results = []
        dispatcher = WebhookDispatcher(
            on_result=lambda *args: results.append(args),
            opener=opener,
            clock=clock,
            max_workers=1,
        )
        with mock.patch("automation.time.sleep") as sleep, mock.patch.object(automation.LOGGER, "warning"):
            self.assertTrue(dispatcher.enqueue(
                [{"id": "w1", "url": "https://example.com/hook", "events": ["library.changed"], "enabled": True, "attempts": 2, "timeout": 1, "secret": ""}],
                build_event("library.changed", {"action": "add", "count": 1}),
            ))
            dispatcher.start()
            for _ in range(200):
                if results:
                    break
                time.sleep(0.01)
            dispatcher.shutdown(wait_seconds=1)
        # The worker may finish the final attempt during shutdown.
        for _ in range(200):
            if results:
                break
            time.sleep(0.01)
        self.assertEqual(len(results), 1, "one terminal on_result per envelope")
        # The injected clock must have been read around the retry sleep.
        self.assertTrue(calls, "injected clock must be exercised")
        self.assertTrue(sleep.called, "retry backoff must sleep")

    def test_event_allowlist_and_signature(self):
        event = build_event("session.started", {"name": "Alpha", "secret": "must-drop"})
        self.assertEqual(event["data"], {"name": "Alpha"})
        body = b"{}"
        self.assertEqual(len(sign_event("secret", "stamp", body)), 64)

    def test_validation_requires_allowed_event_and_https(self):
        with self.assertRaises(ValueError):
            validate_webhook({"url": "https://example.com", "events": ["unknown"]}, resolver=lambda host: ["93.184.216.34"])
        with self.assertRaises(ValueError):
            validate_webhook({"url": "http://example.com", "events": [EVENT_TYPES[0]]}, resolver=lambda host: ["93.184.216.34"])


if __name__ == "__main__":
    unittest.main()
