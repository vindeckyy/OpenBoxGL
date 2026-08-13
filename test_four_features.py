"""Contract tests for queue, tags, notifications, and webhook primitives."""
from __future__ import annotations

import unittest

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
