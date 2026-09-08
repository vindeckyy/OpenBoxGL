#!/usr/bin/env python3
"""State transactions record opt-in catalog revisions atomically."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class SyncTransactionTests(unittest.TestCase):
    def test_opt_in_catalog_mutation_records_event_and_local_path_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["OPENBOX_DATA_DIR"] = directory
            import openbox
            from webapp_state import transact_state

            openbox.STATE_STORE.save({
                "games": [{"game_id": "g1", "name": "Quake", "path": "/old"}],
                "settings": {"library_sync_enabled": True}, "profiles": {},
            })
            transact_state(lambda state: state["games"][0].update({"name": "Quake II", "path": "/new"}))
            state = openbox.load_state()
            events = state["library_sync"]["outbox"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["catalog"]["name"], "Quake II")
            self.assertNotIn("path", events[0]["catalog"])
            self.assertTrue(state["games"][0]["path"].endswith("/new"))
            os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_openbox_update_state_records_and_identity_error_does_not_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["OPENBOX_DATA_DIR"] = directory
            import openbox

            openbox.STATE_STORE.save({
                "games": [{"game_id": "g1", "name": "Quake"}],
                "settings": {"library_sync_enabled": True}, "profiles": {},
            })
            openbox.update_state(lambda state: state["games"][0].update({"name": "Quake II"}))
            state = openbox.load_state()
            self.assertEqual(state["games"][0]["name"], "Quake II")
            self.assertEqual(len(state["library_sync"]["outbox"]), 1)

            openbox.STATE_STORE.save({
                "games": [{"game_id": "same", "name": "One"}],
                "settings": {"library_sync_enabled": True}, "profiles": {},
                "library_sync": [],
            })
            openbox.update_state(lambda state: state["games"].append({"game_id": "new", "name": "Three"}))
            state = openbox.load_state()
            self.assertEqual(len(state["games"]), 2)
            self.assertEqual(state["library_sync"]["error"]["code"], "SYNC_METADATA_INVALID")
            os.environ.pop("OPENBOX_DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()
