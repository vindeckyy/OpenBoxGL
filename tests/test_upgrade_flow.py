#!/usr/bin/env python3
"""Upgrade flow (plan W1-12, W6-01): the migration report, the acknowledgement, and the rollback.

A v6 library is upgraded on purpose, so the app shows what changed before the user works with
it, and offers the rollback. The acknowledgement is tied to the migration that happened, so a
later migration is shown again.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.state import library_v7  # noqa: E402
from pkg.parity.parity_compat import readiness_checks  # noqa: E402
from handlers.setup import migration_status_payload  # noqa: E402
from state_store import JsonStateStore, default_state  # noqa: E402


def v6_library(games=2):
    return {
        "schema_version": 6,
        "games": [
            {"game_id": f"g{index}", "name": f"Game {index}", "path": f"/roms/g{index}.iso", "platform": "PS1"}
            for index in range(games)
        ],
        "profiles": {}, "history": [], "settings": {"theme": "dark"}, "playlists": [],
        "queue": [], "notifications": [], "ui_state": {}, "active_sessions": [], "trash": [],
        "party_decks": [], "emulator_settings": {}, "achievement_overrides": {},
    }


class MigrationStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def migrate(self):
        primary = self.data / "library.json"
        primary.write_bytes(json.dumps(v6_library()).encode("utf-8"))
        JsonStateStore(primary).migrate_primary_to_v7()
        return primary

    def test_a_migration_is_pending_until_the_user_acknowledges_it(self):
        self.migrate()
        status = library_v7.migration_status(self.data)
        self.assertTrue(status["pending"])
        self.assertEqual(status["report"]["from_schema"], 6)
        self.assertEqual(status["report"]["to_schema"], 7)
        self.assertTrue(status["backup_path"].endswith("library.v6.backup.json"))

    def test_acknowledging_the_migration_clears_the_pending_state(self):
        self.migrate()
        library_v7.acknowledge_migration(self.data)
        self.assertFalse(library_v7.migration_status(self.data)["pending"])

    def test_acknowledging_without_a_migration_says_so(self):
        self.assertEqual(library_v7.acknowledge_migration(self.data)["status"], "no_migration")
        self.assertFalse(library_v7.migration_status(self.data)["pending"])

    def test_the_report_names_the_games_and_the_copy_of_the_old_library(self):
        self.migrate()
        status = library_v7.migration_status(self.data)
        self.assertEqual(status["report"]["counts"]["games"], 2)
        self.assertEqual(status["rollback_command"], "openbox --rollback-library")


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.primary = self.data / "library.json"
        self.original = json.dumps(v6_library()).encode("utf-8")
        self.primary.write_bytes(self.original)
        JsonStateStore(self.primary).migrate_primary_to_v7()

    def test_the_rollback_needs_confirmation(self):
        preview = library_v7.rollback(self.data, confirmed=False)
        self.assertEqual(preview["status"], "confirmation_required")
        self.assertTrue(self.primary.is_file())
        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["schema_version"], 7)

    def test_a_confirmed_rollback_restores_the_v6_file(self):
        library_v7.rollback(self.data, confirmed=True, allow_running=True)
        self.assertEqual(self.primary.read_bytes(), self.original)
        self.assertTrue((self.data / "rollback-report.json").is_file())

    def test_a_rollback_while_the_server_runs_is_refused_by_default(self):
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            (self.data / "server.port").write_text(str(listener.getsockname()[1]), encoding="utf-8")
            with self.assertRaises(library_v7.RollbackRefused):
                library_v7.rollback(self.data, confirmed=True)


class FrozenWritesTests(unittest.TestCase):
    def test_a_rolled_back_library_is_not_written_again(self):
        import state_store

        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "library.json"
            primary.write_bytes(json.dumps(v6_library()).encode("utf-8"))
            store = JsonStateStore(primary)
            store.migrate_primary_to_v7()
            state_store.freeze_library_writes()
            try:
                with self.assertRaises(state_store.LibraryFrozenError):
                    store.save(default_state())
            finally:
                state_store.LIBRARY_FROZEN = False


class ReadinessCheckTests(unittest.TestCase):
    """The readiness checks the 1.19.2 preflight would have shown (plan 14.1).

    The maintainer declined the 1.19.2 release, so the same checks run on 2.0's
    first launch instead, before the migrated library is used.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.root = Path(__file__).resolve().parent.parent

    def _write_library(self, games=2):
        (self.data / "library.json").write_text(json.dumps(v6_library(games)), encoding="utf-8")

    def test_a_theme_with_an_unknown_token_is_named(self):
        self._write_library()
        (self.data / "themes").mkdir()
        (self.data / "themes" / "Broken.css").write_text(":root { --not-a-real-token: 1px; }\n", encoding="utf-8")
        items = readiness_checks(self.data, root=self.root)
        themes = [item for item in items if item["kind"] == "theme"]
        self.assertTrue(themes, "a theme with an unknown token was not reported")
        self.assertEqual(themes[0]["label"], "Broken")

    def test_a_plugin_with_an_unknown_api_version_is_named(self):
        self._write_library()
        plugin_dir = self.data / "plugins" / "future-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(json.dumps({
            "id": "future-plugin",
            "name": "Future Plugin",
            "version": "1.0.0",
            "entry": "plugin.py",
            "api_version": 9,
        }), encoding="utf-8")
        items = readiness_checks(self.data, root=self.root)
        plugins = [item for item in items if item["kind"] == "plugin"]
        self.assertTrue(plugins, "a plugin with an unknown API version was not reported")
        self.assertEqual(plugins[0]["id"], "plugin:future-plugin")

    def test_a_library_over_the_scale_is_named(self):
        self._write_library(games=3)
        with mock.patch("pkg.parity.parity_compat.READINESS_LIBRARY_LIMIT", 2):
            items = readiness_checks(self.data, root=self.root)
        self.assertTrue(any(item["id"] == "library:size" for item in items))

    def test_a_clean_library_reports_nothing_to_review(self):
        self._write_library()
        items = readiness_checks(self.data, root=self.root)
        # The data directory is the one thing that always differs here: the
        # test runs against a temporary directory, not the default one.
        self.assertEqual([item for item in items if item["kind"] != "library"], [])

    def test_the_migration_status_carries_the_readiness_items(self):
        self._write_library()
        (self.data / "themes").mkdir()
        (self.data / "themes" / "Broken.css").write_text(":root { --not-a-real-token: 1px; }\n", encoding="utf-8")
        status = migration_status_payload(self.data)
        self.assertTrue(any(item["kind"] == "theme" for item in status["readiness"]))


if __name__ == "__main__":
    unittest.main()
