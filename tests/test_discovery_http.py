"""HTTP contract tests for Game DNA discovery routes (Flagship 9).

Covers /api/v2/library/dna/{search,status,index/rebuild}, the additive
pick params, and the library mutation invalidation hooks.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

GAMES = [
    {
        "game_id": "g-stardew",
        "name": "Stardew Valley",
        "genre": "Simulation",
        "tags": ["farming", "cozy"],
        "description": "A relaxing farming sim with wholesome villagers.",
        "platform": "PC",
        "user_rating": 5,
        "progress": "Unplayed",
        "time_to_beat_hours": 60,
    },
    {
        "game_id": "g-hollow",
        "name": "Hollow Knight",
        "genre": "Metroidvania",
        "tags": ["soulslike"],
        "description": "Punishing 2D adventure with stamina-based combat.",
        "platform": "PC",
    },
    {
        "game_id": "g-doom",
        "name": "DOOM",
        "genre": "Shooter",
        "tags": ["fps"],
        "description": "Rip and tear through demons in this intense shooter.",
        "platform": "PC",
    },
]


class DiscoveryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.previous_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = cls.tempdir.name
        import web_app
        from openbox import save_state

        cls.web_app = web_app
        cls.save_state = staticmethod(save_state)
        web_app.TOKEN = "dna-test-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()
        if cls.previous_data_dir is None:
            os.environ.pop("OPENBOX_DATA_DIR", None)
        else:
            os.environ["OPENBOX_DATA_DIR"] = cls.previous_data_dir

    def setUp(self):
        self.save_state(
            {
                "games": [dict(game) for game in GAMES],
                "profiles": {},
                "history": [],
                "settings": {"locale": "en"},
                "playlists": [],
                "queue": [],
                "notifications": [],
            }
        )
        # The state store normalizes game_id to stable ids on save; resolve
        # the real ids for assertions.
        from openbox import load_state_readonly

        self.ids = {g.get("name"): g.get("game_id") for g in load_state_readonly().get("games", [])}
        # Fresh DNA index per test (handlers write to OPENBOX_DATA_DIR).
        index_path = os.path.join(self.tempdir.name, "dna_index.json")
        if os.path.exists(index_path):
            os.unlink(index_path)

    def request(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"X-OpenBox-Token": "dna-test-token", "Content-Type": "application/json"},
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.request.HTTPError as error:
            return error.code, json.loads(error.read())

    # ── search ──
    def test_search_contract(self):
        status, payload = self.request("/api/v2/library/dna/search", {"query": "cozy farming", "limit": 5})
        self.assertEqual(status, 200)
        self.assertIn("results", payload)
        self.assertIn("parse", payload)
        self.assertIn("degraded", payload)
        self.assertFalse(payload["degraded"])
        top = payload["results"][0]
        self.assertEqual(top["game_id"], self.ids["Stardew Valley"])
        self.assertEqual(top["name"], "Stardew Valley")
        self.assertIsInstance(top["score"], (int, float))
        self.assertTrue(top["why"], "every result must carry why-chips")

    def test_search_genre_word_is_soft(self):
        # "adventure" parses as a genre predicate, but DNA search ranks it
        # instead of hard-filtering (Stardew is Simulation, not Adventure).
        status, payload = self.request("/api/v2/library/dna/search", {"query": "cozy farming adventure"})
        self.assertEqual(status, 200)
        ids = [row["game_id"] for row in payload["results"]]
        self.assertIn(self.ids["Stardew Valley"], ids)

    def test_search_requires_query(self):
        status, _payload = self.request("/api/v2/library/dna/search", {"query": "  "})
        self.assertEqual(status, 400)

    def test_search_limit_bounds(self):
        status, _payload = self.request("/api/v2/library/dna/search", {"query": "cozy", "limit": "nope"})
        self.assertEqual(status, 400)

    def test_search_games_like_anchor(self):
        status, payload = self.request("/api/v2/library/dna/search", {"query": "games like Hollow Knight"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["branch"], "similarity")
        self.assertEqual(payload["anchor"]["game_id"], self.ids["Hollow Knight"])
        ids = [row["game_id"] for row in payload["results"]]
        self.assertNotIn(self.ids["Hollow Knight"], ids)
        self.assertTrue(any("like Hollow Knight" in chip for row in payload["results"] for chip in row["why"]))

    def test_search_filters(self):
        status, payload = self.request(
            "/api/v2/library/dna/search", {"query": "adventure", "filters": {"genre": ["Shooter"]}}
        )
        self.assertEqual(status, 200)
        for row in payload["results"]:
            self.assertNotEqual(row["game_id"], self.ids["Stardew Valley"])

    def test_search_degraded_title_fallback(self):
        import handlers.discovery as discovery

        old = discovery.SYNC_BUILD_THRESHOLD
        discovery.SYNC_BUILD_THRESHOLD = 1  # 3 games >= 1 → background build
        try:
            status, payload = self.request("/api/v2/library/dna/search", {"query": "Stardew"})
        finally:
            discovery.SYNC_BUILD_THRESHOLD = old
        self.assertEqual(status, 200)
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["branch"], "title-fallback")
        self.assertEqual(payload["results"][0]["game_id"], self.ids["Stardew Valley"])

    # ── status ──
    def test_status_contract(self):
        # No index yet (setUp deletes it) → honest "missing" state.
        status, payload = self.request("/api/v2/library/dna/status")
        self.assertEqual(status, 200)
        for key in ("indexed", "total", "coverage_pct", "index_version", "lexicon_version", "build_ms", "state"):
            self.assertIn(key, payload)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["coverage_pct"], 100.0)
        self.assertEqual(payload["state"], "missing")
        # A search builds the small index synchronously; status turns ready.
        self.request("/api/v2/library/dna/search", {"query": "cozy"})
        status, payload = self.request("/api/v2/library/dna/status")
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["indexed"], 3)

    def test_status_reports_stale_before_reconcile(self):
        # Build the index, then mutate a game behind the index's back.
        self.request("/api/v2/library/dna/search", {"query": "cozy"})
        games = [dict(game) for game in GAMES]
        games[0]["description"] = "A completely rewritten description."
        self.save_state({"games": games})
        status, payload = self.request("/api/v2/library/dna/status")
        self.assertEqual(status, 200)
        # Stale must be computed from the on-disk index before the freshness
        # pass rewrites its signature — never optimistically "ready".
        self.assertEqual(payload["state"], "stale")
        # The freshness pass repaired it; a second read is ready again.
        status, payload = self.request("/api/v2/library/dna/status")
        self.assertEqual(payload["state"], "ready")

    # ── rebuild ──
    def test_rebuild_job(self):
        status, payload = self.request("/api/v2/library/dna/index/rebuild", {})
        self.assertEqual(status, 202)
        self.assertTrue(payload["job_id"])
        # Wait for the background job to finish.
        deadline = time.time() + 15
        while time.time() < deadline:
            code, status_payload = self.request("/api/v2/library/dna/status")
            self.assertEqual(code, 200)
            if status_payload["state"] == "ready" and status_payload["indexed"] == 3:
                break
            time.sleep(0.2)
        self.assertEqual(status_payload["indexed"], 3)
        index_path = os.path.join(self.tempdir.name, "dna_index.json")
        self.assertTrue(os.path.exists(index_path))

    # ── picker integration ──
    def test_pick_defaults_unchanged(self):
        status, payload = self.request("/api/v2/library/pick", {"mood": "any"})
        self.assertEqual(status, 200)
        self.assertIn("picks", payload)
        for pick in payload["picks"]:
            self.assertNotIn("dna_seed", pick)

    def test_pick_dna_params_accepted(self):
        status, payload = self.request(
            "/api/v2/library/pick",
            {
                "mood": "any",
                "seed_query": "cozy farming",
                "dna_boost": 1.0,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("picks", payload)

    def test_pick_dna_boost_validated(self):
        status, _payload = self.request(
            "/api/v2/library/pick",
            {
                "mood": "any",
                "seed_query": "cozy",
                "dna_boost": -1,
            },
        )
        self.assertEqual(status, 400)
        status, _payload = self.request(
            "/api/v2/library/pick",
            {
                "mood": "any",
                "dna_boost": "lots",
            },
        )
        self.assertEqual(status, 400)

    # ── mutation hooks ──
    def test_manual_entry_add_updates_index(self):
        status, _payload = self.request(
            "/api/v2/library/manual-entry",
            {
                "game": {
                    "name": "Cozy Grove",
                    "genre": "Simulation",
                    "description": "A cozy camping adventure on a haunted island.",
                },
            },
        )
        self.assertEqual(status, 200)
        status, payload = self.request("/api/v2/library/dna/search", {"query": "cozy camping"})
        self.assertEqual(status, 200)
        # "camping" only exists in the new game's description → it must rank.
        self.assertGreaterEqual(len(payload["results"]), 1)
        index_path = os.path.join(self.tempdir.name, "dna_index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual(index["doc_count"], 4)

    def test_bulk_tag_edit_updates_index(self):
        # Build the index first (mutations only update an existing index;
        # a missing index is built lazily by search/status).
        self.request("/api/v2/library/dna/search", {"query": "cozy"})
        status, _payload = self.request(
            "/api/games/bulk",
            {
                "ids": [self.ids["DOOM"]],
                "changes": {"tags": ["fps", "cozy"]},
            },
        )
        self.assertEqual(status, 200)
        index_path = os.path.join(self.tempdir.name, "dna_index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertIn("cozy", index["games"][self.ids["DOOM"]]["v"])

    def test_delete_removes_from_index(self):
        self.request("/api/v2/library/dna/search", {"query": "cozy"})
        status, _payload = self.request("/api/game/delete", {"game_id": self.ids["DOOM"]})
        self.assertEqual(status, 200)
        index_path = os.path.join(self.tempdir.name, "dna_index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertNotIn(self.ids["DOOM"], index["games"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
