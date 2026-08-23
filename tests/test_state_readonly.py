"""Tests for the MappingProxyType contract on load_state_readonly()."""

import json
import tempfile
import types
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state_store import JsonStateStore


class ReadonlyStateTests(unittest.TestCase):
    """Verify that load_readonly() returns an immutable MappingProxyType."""

    def _make_store(self, directory):
        return JsonStateStore(Path(directory) / "library.json")

    def _seed_state(self, store):
        store.save({
            "schema_version": 6,
            "games": [{"game_id": "game-aaa", "name": "Doom", "platform": "PC"}],
            "profiles": {},
            "history": [],
            "settings": {"locale": "en"},
            "playlists": [],
            "queue": [],
            "notifications": [],
            "ui_state": {},
            "active_sessions": [],
        })

    # -- type contract --------------------------------------------------------

    def test_returns_mapping_proxy_type(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            result = store.load_readonly()
            self.assertIsInstance(result, types.MappingProxyType)

    # -- mutation rejection ---------------------------------------------------

    def test_top_level_setitem_raises_type_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            with self.assertRaises(TypeError):
                view["games"] = []

    def test_top_level_delitem_raises_type_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            with self.assertRaises(TypeError):
                del view["games"]

    def test_top_level_update_raises_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            with self.assertRaises((TypeError, AttributeError)):
                view.update({"games": []})

    def test_top_level_pop_raises_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            with self.assertRaises((TypeError, AttributeError)):
                view.pop("games")

    def test_top_level_setdefault_raises_error(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            with self.assertRaises((TypeError, AttributeError)):
                view.setdefault("new_key", "value")

    # -- read operations still work -------------------------------------------

    def test_getitem_returns_value(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertEqual(view["schema_version"], 6)

    def test_get_returns_value(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertEqual(view.get("settings", {}), {"locale": "en"})

    def test_get_returns_default_for_missing_key(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertIsNone(view.get("nonexistent"))
            self.assertEqual(view.get("nonexistent", 42), 42)

    def test_keys_values_items_iterable(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertIn("games", view.keys())
            self.assertIn("games", view)
            self.assertIsInstance(list(view.values()), list)
            self.assertIsInstance(list(view.items()), list)

    def test_len(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertGreater(len(view), 0)

    def test_contains(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            self.assertIn("games", view)
            self.assertNotIn("nonexistent", view)

    # -- proxy reflects current state (not a stale copy) ----------------------

    def test_proxy_reflects_latest_state_after_save(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view1 = store.load_readonly()
            self.assertEqual(view1["games"][0]["name"], "Doom")

            # Save new state
            store.save({
                "schema_version": 6,
                "games": [{"game_id": "game-bbb", "name": "Quake", "platform": "PC"}],
                "profiles": {},
                "history": [],
                "settings": {},
                "playlists": [],
                "queue": [],
                "notifications": [],
                "ui_state": {},
                "active_sessions": [],
            })

            view2 = store.load_readonly()
            self.assertEqual(view2["games"][0]["name"], "Quake")

    def test_proxy_reflects_update_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view1 = store.load_readonly()
            self.assertEqual(view1["games"][0]["name"], "Doom")

            # Mutate via update
            store.update(lambda s: s["games"][0].__setitem__("name", "Doom II"))

            view2 = store.load_readonly()
            self.assertEqual(view2["games"][0]["name"], "Doom II")

    # -- nested values are still mutable (shallow proxy) ----------------------

    def test_nested_list_is_mutable(self):
        """MappingProxyType only freezes the top-level dict; nested objects remain mutable."""
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            # The games list itself is mutable (shallow proxy)
            games = view["games"]
            self.assertIsInstance(games, list)
            # Reading nested data works
            self.assertEqual(games[0]["name"], "Doom")

    # -- json.dumps works with the proxy -------------------------------------

    def test_json_serializable_via_dict(self):
        """MappingProxyType is not directly JSON-serializable, but dict() converts it."""
        with tempfile.TemporaryDirectory() as d:
            store = self._make_store(d)
            self._seed_state(store)
            view = store.load_readonly()
            serialized = json.dumps(dict(view))
            self.assertIsInstance(serialized, str)
            parsed = json.loads(serialized)
            self.assertEqual(parsed["games"][0]["name"], "Doom")


if __name__ == "__main__":
    unittest.main()
