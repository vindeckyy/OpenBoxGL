#!/usr/bin/env python3
"""Coverage gapfill for Flagship 2 changed lines.

Covers missed changed lines in:
  - handlers/metadata.py  (auto-scrape job functions, scrape-settings routes,
                           media-candidates route, exact-candidate apply edges)
  - handlers/steamgrid.py (hygiene report/fix/undo route branches)
  - handlers/media.py     (media audit route, bulk batch-commit error branch)
  - pkg/parity/parity_screenscraper.py (hash_lookup body)

Standalone-script style: ``python3 -B tests/test_gapfill_f2.py``.
All provider I/O is stubbed; no network.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="openbox-gapfill-f2-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_DIR

import pkg.parity  # noqa: F401,E402  # register flat-import finder
import handlers.metadata as hm  # noqa: E402
import handlers.media as media_module  # noqa: E402
from api_errors import BadRequest, Conflict  # noqa: E402
from handlers import steamgrid  # noqa: E402
from handlers.media import MediaHandlers  # noqa: E402
from handlers.metadata import MetadataHandlers  # noqa: E402
from pkg.parity import parity_screenscraper as ss  # noqa: E402
from pkg.parity.parity_artwork_hygiene import write_undo_manifest  # noqa: E402


class _Handler:
    """Minimal send_json/authorized double shared by the route tests."""

    def __init__(self, authorized=True):
        self.responses = []
        self._authorized = authorized

    def authorized(self):
        return self._authorized

    def handle_unauthorized(self):
        self.responses.append((403, {"error": "unauthorized"}))

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload))


class _MetadataHandler(MetadataHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


class _Cancel:
    def __init__(self, cancelled=False):
        self._cancelled = cancelled
        self.progress_calls = []

    def is_set(self):
        return self._cancelled

    def progress(self, **kwargs):
        self.progress_calls.append(kwargs)


# ── handlers/media.py: /api/media/audit ────────────────────────────────────


class MediaAuditRouteTests(unittest.TestCase):
    def setUp(self):
        self.games = [
            {
                "game_id": "g1",
                "name": "One",
                "platform": "PC",
                "launchbox_db_id": 10,
                "cover": "/covers/exists.png",
                "background": "/bg/missing.png",
                "screenshots": ["/shots/a.png", "/shots/b.png"],
            },
            {
                "game_id": "g2",
                "name": "Two",
                "platform": "NES",
                "cover": "",
                "screenshots": "not-a-list",
                "manual": "/manuals/exists.png",
            },
        ]

    def _audit(self, query):
        handler = _Handler()
        target = object.__new__(MediaHandlers)
        target.send_json = handler.send_json
        with (
            mock.patch.object(media_module, "load_state_view", return_value={"games": self.games}),
            mock.patch(
                "pkg.state.media_probe.media_probe_paths_batch",
                side_effect=lambda values: {str(value): ("exists" in str(value)) for value in values},
            ),
        ):
            target._api_get_api_media_audit(SimpleNamespace(query=query))
        return handler.responses[-1]

    def test_audit_platform_filter(self):
        status, payload = self._audit("platform=PC")
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"], 1)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["missing_cover"], 0)
        self.assertEqual(payload["missing_background"], 1)
        self.assertEqual(payload["missing_screenshots"], 1)
        self.assertEqual(payload["missing_manual"], 1)

    def test_audit_all_platforms_and_non_list_screenshots(self):
        status, payload = self._audit("")
        self.assertEqual(status, 200)
        self.assertEqual(payload["games"], 2)
        self.assertEqual(payload["matched"], 1)
        # g1's screenshots are all missing; g2's non-list screenshots count as missing.
        self.assertEqual(payload["missing_screenshots"], 2)
        self.assertEqual(payload["missing_manual"], 1)
        self.assertEqual(payload["missing_cover"], 1)


# ── handlers/media.py: bulk worker batch-commit error branch ───────────────


class BulkMediaBatchErrorTests(unittest.TestCase):
    def setUp(self):
        media_module.MEDIA_JOB.clear()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.games = [
            {
                "game_id": f"g{index:03d}",
                "name": f"Game {index}",
                "launchbox_db_id": str(1000 + index),
                "platform": "PC",
            }
            for index in range(30)
        ]

    def test_batch_commit_failure_marks_all_failed(self):
        database = Path(self.tempdir.name) / "metadata.sqlite"
        database.write_bytes(b"stub")
        state = {"games": self.games, "settings": {}}

        def fake_metadata(game, *_args):
            return dict(game, cover=f"/media/{game['game_id']}.png")

        handler = object.__new__(MediaHandlers)
        handler.send_json = mock.Mock()
        with (
            mock.patch.object(media_module, "METADATA_DATABASE", database),
            mock.patch.object(media_module, "load_state", return_value=state),
            mock.patch.object(media_module, "apply_game_metadata", side_effect=fake_metadata),
            mock.patch.object(media_module, "transact_state", side_effect=OSError("db locked")),
            mock.patch.object(media_module, "bump_media_epoch"),
            mock.patch.object(
                media_module.JOB_MANAGER,
                "submit",
                side_effect=lambda _name, worker: (worker(), {"job_id": "j"})[1],
            ),
        ):
            handler.bulk_media({"media": ["cover"], "platform": "all"})

        self.assertEqual(media_module.MEDIA_JOB["state"], "error")
        self.assertTrue(
            any("batch state update failed" in error for error in media_module.MEDIA_JOB["errors"]),
            media_module.MEDIA_JOB["errors"],
        )
        self.assertEqual(len(media_module.MEDIA_JOB["failed_game_ids"]), 30)


# ── handlers/metadata.py: media-candidates route edges ─────────────────────


class MediaCandidatesRouteEdgeTests(unittest.TestCase):
    def test_unparseable_database_id_is_bad_request(self):
        handler = _MetadataHandler()
        with self.assertRaises(BadRequest):
            handler._api_get_api_v2_metadata_media_candidates(SimpleNamespace(query="database_id=xyz"))

    def test_missing_database_is_conflict(self):
        handler = _MetadataHandler()
        missing = Path(_DATA_DIR) / "no-such-metadata.db"
        self.assertFalse(missing.is_file())
        with mock.patch.object(hm, "METADATA_DATABASE", missing):
            with self.assertRaises(Conflict):
                handler._api_get_api_v2_metadata_media_candidates(SimpleNamespace(query="database_id=42"))


# ── handlers/metadata.py: exact-candidate apply edges ─────────────────────


class ApplyMetadataExactEdgeTests(unittest.TestCase):
    def _run(self, game, media_urls, candidates, overwrite=False, download=None):
        state = {"games": [game], "settings": {}}
        database = Path(_DATA_DIR) / "metadata.db"
        database.write_bytes(b"stub")
        handler = _MetadataHandler()
        download_mock = mock.Mock(side_effect=download if download is not None else (lambda url, dest: str(dest)))
        with (
            mock.patch.object(hm, "METADATA_DATABASE", database),
            mock.patch.object(hm, "DATA", Path(_DATA_DIR) / "library.json"),
            mock.patch.object(hm, "load_state", return_value=state),
            mock.patch.object(hm, "game_from_payload", side_effect=lambda _s, _p: game),
            mock.patch.object(hm, "apply_game_metadata", return_value={}),
            mock.patch.object(hm, "_lbdb_candidate_urls", return_value=candidates),
            mock.patch.object(hm, "download_bytes", download_mock),
            mock.patch.object(hm, "transact_state", side_effect=lambda m: (m(state), None)),
            mock.patch.object(hm, "bump_media_epoch"),
        ):
            handler.apply_metadata(
                {
                    "game_id": game["game_id"],
                    "database_id": 10,
                    "media": ["cover", "background"],
                    "overwrite": overwrite,
                    "media_urls": media_urls,
                }
            )
        return handler, game, download_mock

    def test_existing_artwork_is_kept_without_overwrite(self):
        game = {"game_id": "g-1", "name": "Alpha", "cover": "/old/cover.png"}
        url = "https://images.launchbox-app.com/exact.png"
        _handler, updated, download_mock = self._run(game, {"cover": url}, {("cover", url)}, overwrite=False)
        download_mock.assert_not_called()
        self.assertEqual(updated["cover"], "/old/cover.png")

    def test_failed_download_skips_kind_and_keeps_going(self):
        game = {"game_id": "g-1", "name": "Alpha"}
        cover_url = "https://images.launchbox-app.com/cover.png"
        bg_url = "https://images.launchbox-app.com/bg.png"

        def download(url, dest):
            if url == cover_url:
                raise OSError("network down")
            return str(dest)

        _handler, updated, download_mock = self._run(
            game,
            {"cover": cover_url, "background": bg_url},
            {("cover", cover_url), ("background", bg_url)},
            download=download,
        )
        self.assertEqual(download_mock.call_count, 2)
        self.assertNotIn("cover", updated)
        background = Path(updated["background"])
        self.assertEqual(background.parent, Path(_DATA_DIR) / "media" / "launchbox" / "10")
        self.assertEqual(background.name, "background.png")

    def test_extensionless_url_defaults_to_jpg(self):
        game = {"game_id": "g-1", "name": "Alpha"}
        url = "https://images.launchbox-app.com/nosuffix?size=large"
        _handler, _updated, download_mock = self._run(game, {"cover": url}, {("cover", url)})
        _dest = download_mock.call_args[0][1]
        self.assertTrue(str(_dest).endswith("cover.jpg"), _dest)


# ── handlers/metadata.py: auto-scrape helpers ───────────────────────────────


class AutoScrapeHelperTests(unittest.TestCase):
    def test_igdb_configured_flag(self):
        with mock.patch.object(hm, "igdb_credentials", return_value=("id", "secret")):
            self.assertTrue(hm._igdb_configured())
        with mock.patch.object(hm, "igdb_credentials", side_effect=ValueError("nope")):
            self.assertFalse(hm._igdb_configured())

    def test_normalize_scrape_title(self):
        self.assertEqual(hm._normalize_scrape_title("Super Mario 64!"), "supermario64")
        self.assertEqual(hm._normalize_scrape_title(None), "")

    def test_unmatched_filters_batch_and_provider_ids(self):
        state = {
            "games": [
                {"game_id": "a", "import_batch_id": "b1"},
                {"game_id": "b", "import_batch_id": "b1", "launchbox_db_id": 1},
                {"game_id": "c", "import_batch_id": "b1", "igdb_id": 2},
                {"game_id": "d", "import_batch_id": "b1", "screenscraper_id": 3},
                {"game_id": "e", "import_batch_id": "b2"},
            ]
        }
        unmatched = hm._auto_scrape_unmatched(state, "b1")
        self.assertEqual([game["game_id"] for game in unmatched], ["a"])


# ── handlers/metadata.py: screenscraper auto-scrape pass ───────────────────


class ScreenscraperPassTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _rom(self, name):
        path = self.root / name
        path.write_bytes(b"rom-bytes")
        return path

    def _state(self, games):
        return {"games": games, "settings": {"scrape_screenscraper_enabled": True}}

    def _game(self, game_id, rom_path, batch="b1", **extra):
        return {
            "game_id": game_id,
            "name": f"Game {game_id}",
            "platform": "SNES",
            "path": str(rom_path),
            "import_batch_id": batch,
            **extra,
        }

    def test_pass_loop_errors_and_single_tier(self):
        rom1, rom2, rom3 = self._rom("g1.sfc"), self._rom("g2.sfc"), self._rom("g3.sfc")
        state = self._state(
            [
                self._game("g1", rom1),  # hash lookup blows up
                self._game("g2", rom2),  # single-tier: skipped
                self._game("g3", rom3),  # dual-tier: transact blows up
                self._game("g4", self.root / "missing.sfc"),  # no file on disk: filtered
                self._game("g5", rom1, batch="other"),  # wrong batch: filtered
                self._game("g6", rom2, launchbox_db_id=9),  # already matched: filtered
            ]
        )

        def fake_match(path, system_id=None, cache_dir=None):
            if path == str(rom1):
                raise OSError("net down")
            if path == str(rom2):
                return ({"id": 21}, "single")
            if path == str(rom3):
                return ({"id": 22, "name": "Game g3"}, hm.HASH_TIER_DUAL)
            raise AssertionError(f"unexpected path {path}")

        with (
            mock.patch.object(hm, "load_state", return_value=state),
            mock.patch.object(hm, "DATA", self.root / "library.json"),
            mock.patch.object(hm, "screenscraper_configured", return_value=True),
            mock.patch.object(hm, "confident_hash_match", side_effect=fake_match),
            mock.patch.object(hm, "system_id_for_platform", return_value=4),
            mock.patch.object(hm, "apply_screenscraper_metadata") as apply,
            mock.patch.object(hm, "transact_state", side_effect=RuntimeError("locked")),
        ):
            summary = {}
            hm._auto_scrape_screenscraper_pass("b1", None, summary)
        self.assertEqual(summary["screenscraper"]["matched"], 0)
        errors = summary["screenscraper"]["errors"]
        self.assertEqual(len(errors), 2)
        self.assertTrue(errors[0].startswith("Game g1: "))
        self.assertTrue(errors[1].startswith("Game g3: "))
        apply.assert_not_called()

    def test_pass_applies_dual_match(self):
        rom = self._rom("g1.sfc")
        state = self._state([self._game("g1", rom)])
        applied = []

        def fake_transact(mutator):
            mutator(state)
            return (None, None)

        with (
            mock.patch.object(hm, "load_state", return_value=state),
            mock.patch.object(hm, "DATA", self.root / "library.json"),
            mock.patch.object(hm, "screenscraper_configured", return_value=True),
            mock.patch.object(
                hm,
                "confident_hash_match",
                return_value=({"id": 22, "name": "Game g1"}, hm.HASH_TIER_DUAL),
            ),
            mock.patch.object(hm, "system_id_for_platform", return_value=4),
            mock.patch.object(
                hm,
                "apply_screenscraper_metadata",
                side_effect=lambda target, metadata: applied.append(metadata["id"]),
            ),
            mock.patch.object(hm, "transact_state", side_effect=fake_transact),
        ):
            summary = {}
            hm._auto_scrape_screenscraper_pass("b1", None, summary)
        self.assertEqual(summary["screenscraper"]["matched"], 1)
        self.assertEqual(applied, [22])
        game = state["games"][0]
        self.assertEqual(game["screenscraper_id"], 22)
        self.assertEqual(game["matched_by"], "hash")
        self.assertEqual(game["match_confidence"], hm.HASH_TIER_DUAL)

    def test_pass_honours_cancellation(self):
        rom = self._rom("g1.sfc")
        state = self._state([self._game("g1", rom)])
        with (
            mock.patch.object(hm, "load_state", return_value=state),
            mock.patch.object(hm, "DATA", self.root / "library.json"),
            mock.patch.object(hm, "screenscraper_configured", return_value=True),
            mock.patch.object(hm, "confident_hash_match") as match,
        ):
            summary = {}
            hm._auto_scrape_screenscraper_pass("b1", _Cancel(cancelled=True), summary)
        match.assert_not_called()
        self.assertEqual(summary["screenscraper"]["matched"], 0)


# ── handlers/metadata.py: IGDB auto-scrape pass ─────────────────────────────


class IgdbPassTests(unittest.TestCase):
    def _state(self, games):
        return {"games": games, "settings": {"scrape_igdb_enabled": True}}

    def _game(self, game_id, name, **extra):
        return {
            "game_id": game_id,
            "name": name,
            "platform": "SNES",
            "import_batch_id": "b1",
            **extra,
        }

    def _patches(self, state, search, fetch, transact):
        return (
            mock.patch.object(hm, "load_state", return_value=state),
            mock.patch.object(hm, "igdb_credentials", return_value=("id", "secret")),
            mock.patch.object(hm, "search_igdb_games", side_effect=search),
            mock.patch.object(hm, "fetch_igdb_game", side_effect=fetch),
            mock.patch.object(hm, "apply_igdb_metadata"),
            mock.patch.object(hm, "transact_state", side_effect=transact),
        )

    def test_pass_loop_branches(self):
        state = self._state(
            [
                self._game("a", ""),  # blank name
                self._game("b", "Bravo"),  # search blows up
                self._game("c", "Charlie"),  # no title match
                self._game("d", "Delta", year="1999"),  # year mismatch
                self._game("e", "Echo"),  # fetch blows up
                self._game("f", "Foxtrot", year="2005"),  # transact blows up
            ]
        )

        def fake_search(name, **_kwargs):
            if name == "Bravo":
                raise OSError("net down")
            return {
                "Charlie": [{"id": 10, "name": "Unrelated"}],
                "Delta": [{"id": 11, "name": "Delta!", "year": "2001"}],
                "Echo": [{"id": 12, "name": "Echo"}],
                "Foxtrot": [{"id": 13, "name": "Foxtrot", "year": "2005"}],
            }[name]

        def fake_fetch(igdb_id):
            if igdb_id == 12:
                raise OSError("gone")
            return {"id": igdb_id, "name": "Foxtrot"}

        def boom_transact(_mutator):
            raise RuntimeError("locked")

        patches = self._patches(state, fake_search, fake_fetch, transact=boom_transact)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            summary = {}
            hm._auto_scrape_igdb_pass("b1", None, summary)
        self.assertEqual(summary["igdb"]["matched"], 0)
        errors = summary["igdb"]["errors"]
        self.assertEqual(len(errors), 3)
        self.assertTrue(errors[0].startswith("Bravo: "))
        self.assertTrue(errors[1].startswith("Echo: "))
        self.assertTrue(errors[2].startswith("Foxtrot: "))

    def test_pass_applies_exact_title_match(self):
        state = self._state([self._game("g", "Golf")])

        def fake_transact(mutator):
            mutator(state)
            return (None, None)

        patches = self._patches(
            state,
            search=lambda name, **_k: [{"id": 14, "name": "Golf"}],
            fetch=lambda _igdb_id: {"id": 14, "name": "Golf"},
            transact=fake_transact,
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            summary = {}
            hm._auto_scrape_igdb_pass("b1", None, summary)
        self.assertEqual(summary["igdb"]["matched"], 1)
        game = state["games"][0]
        self.assertEqual(game["matched_by"], "igdb")
        self.assertEqual(game["match_confidence"], "exact_title")

    def test_pass_honours_cancellation(self):
        state = self._state([self._game("g", "Golf")])
        patches = self._patches(state, search=mock.Mock(), fetch=mock.Mock(), transact=mock.Mock())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            summary = {}
            hm._auto_scrape_igdb_pass("b1", _Cancel(cancelled=True), summary)
        self.assertEqual(summary["igdb"]["matched"], 0)
        self.assertEqual(summary["igdb"]["errors"], [])


# ── handlers/metadata.py: match worker + wait-for-match-job ─────────────────


class MatchWorkerTests(unittest.TestCase):
    def test_match_worker_without_database_skips_lbdb(self):
        missing = Path(_DATA_DIR) / "no-such-metadata.db"
        with (
            mock.patch.object(hm, "METADATA_DATABASE", missing),
            mock.patch.object(hm, "load_state", return_value={"settings": {}}),
        ):
            summary = hm._auto_scrape_match_worker(None, "b1", None)
        self.assertEqual(summary["batch"], "b1")
        self.assertIsNone(summary["preview_id"])
        self.assertEqual(summary["lbdb"], {"skipped": "metadata database not downloaded"})
        self.assertTrue(summary["screenscraper"]["skipped"])
        self.assertTrue(summary["igdb"]["skipped"])


class WaitForMatchJobTests(unittest.TestCase):
    def test_terminal_state_returns_immediately(self):
        class FakeJM:
            def snapshot(self, _name):
                return {"state": "done"}

        with mock.patch.object(hm, "JOB_MANAGER", FakeJM()):
            self.assertEqual(hm._wait_for_match_job("b1", None), "done")

    def test_cancelled_event_short_circuits(self):
        with mock.patch.object(hm, "AUTO_SCRAPE_MATCH_WAIT_SECONDS", 600):
            self.assertEqual(hm._wait_for_match_job("b1", _Cancel(cancelled=True)), "cancelled")

    def test_zero_wait_deadline_times_out(self):
        with mock.patch.object(hm, "AUTO_SCRAPE_MATCH_WAIT_SECONDS", 0):
            self.assertEqual(hm._wait_for_match_job("b1", None), "timeout")

    def test_timeout_polls_then_sleeps(self):
        class FakeJM:
            def snapshot(self, _name):
                return {"state": "queued"}

        with (
            mock.patch.object(hm, "JOB_MANAGER", FakeJM()),
            mock.patch.object(hm, "AUTO_SCRAPE_MATCH_WAIT_SECONDS", 600),
            mock.patch("time.sleep") as sleep,
            mock.patch("time.monotonic", side_effect=[1000.0, 1000.0, 2000.0]),
        ):
            result = hm._wait_for_match_job("b1", None)
        self.assertEqual(result, "timeout")
        sleep.assert_called_once_with(hm.AUTO_SCRAPE_MATCH_POLL_SECONDS)


# ── handlers/metadata.py: SteamGridDB auto-scrape fill ─────────────────────


class SteamgridFillTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.plans = {
            "Alpha": {"found": [{"id": 100}], "urls": {"cover": "https://cdn.example/alpha.png"}},
            "Bravo": {"error": OSError("down")},
            "Charlie": {"found": []},
            "Delta": {"found": [{"id": 101}], "urls": {"cover": ""}},
            "Echo": {"found": [{"id": 102}], "urls": {"cover": "https://cdn.example/echo.png"}},
            "India": {"found": [{"id": 103}], "urls": {"cover": "https://cdn.example/india.png"}},
        }
        self.current = {}
        self.state = {
            "games": [
                {
                    "game_id": "g1",
                    "name": "Alpha",
                    "import_batch_id": "b1",
                    "launchbox_db_id": 10,
                    "cover": "",
                    "background": "/has/bg.png",
                },
                {
                    "game_id": "g2",
                    "name": "Bravo",
                    "import_batch_id": "b1",
                    "igdb_id": 5,
                    "cover": "/has/cover.png",
                    "background": "",
                },
                {"game_id": "g3", "name": "Charlie", "import_batch_id": "b1", "screenscraper_id": 9, "cover": ""},
                {"game_id": "g4", "name": "Delta", "import_batch_id": "b1", "launchbox_db_id": 11, "cover": ""},
                {"game_id": "g5", "name": "Echo", "import_batch_id": "b1", "launchbox_db_id": 12, "cover": ""},
                {
                    "game_id": "g6",
                    "name": "Foxtrot",
                    "import_batch_id": "b1",
                    "launchbox_db_id": 13,
                    "cover": "/has/c.png",
                    "background": "/has/b.png",
                },
                {"game_id": "g7", "name": "Golf", "import_batch_id": "b1", "cover": ""},
                {"game_id": "g8", "name": "Hotel", "import_batch_id": "other", "launchbox_db_id": 14, "cover": ""},
                {"game_id": "g9", "name": "India", "import_batch_id": "b1", "launchbox_db_id": 15, "cover": ""},
            ],
            "settings": {"scrape_steamgrid_enabled": True},
        }

    def _fake_search(self, name, limit=1, cache_dir=None):
        plan = self.plans[name]
        if "error" in plan:
            raise plan["error"]
        self.current["name"] = name
        return plan["found"]

    def _fake_download(self, url, dest):
        if "echo" in url:
            raise OSError("net down")
        return str(dest)

    def _patches(self, transact):
        return (
            mock.patch.object(hm, "load_state", return_value=self.state),
            mock.patch.object(hm, "DATA", self.root / "library.json"),
            mock.patch.object(hm, "steamgrid_configured", return_value=True),
            mock.patch.object(hm, "search_steamgrid_games", side_effect=self._fake_search),
            mock.patch.object(
                hm, "steamgrid_game_info", side_effect=lambda _sgd_id, cache_dir=None: {"_name": self.current["name"]}
            ),
            mock.patch.object(
                hm,
                "choose_steamgrid_media",
                side_effect=lambda metadata, _missing: dict(self.plans[metadata["_name"]]["urls"]),
            ),
            mock.patch.object(hm, "download_bytes", side_effect=self._fake_download),
            mock.patch.object(hm, "transact_state", side_effect=transact),
            mock.patch.object(hm, "bump_media_epoch"),
        )

    def test_fill_loop_branches(self):
        calls = []

        def fake_transact(mutator):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("locked")
            mutator(self.state)
            return (None, None)

        patches = self._patches(fake_transact)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patches[7],
            patches[8] as bump,
        ):
            result = hm._auto_scrape_steamgrid_fill("b1", ["cover", "background"], None)
        self.assertEqual(result["filled"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(result["errors"][0].startswith("Bravo: "))
        self.assertTrue(result["errors"][1].startswith("India: "))
        alpha = self.state["games"][0]
        self.assertEqual(
            Path(alpha["cover"]).parent,
            self.root / "media" / "steamgrid" / "Alpha",
        )
        self.assertTrue(alpha["cover"].endswith("cover.png"))
        bump.assert_called_once()

    def test_fill_honours_cancellation(self):
        patches = self._patches(transact=mock.Mock())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            result = hm._auto_scrape_steamgrid_fill("b1", ["cover"], _Cancel(cancelled=True))
        self.assertEqual(result, {"filled": 0, "errors": []})

    def test_fill_skips_when_no_steamgrid_kinds_requested(self):
        with (
            mock.patch.object(hm, "load_state", return_value=self.state),
            mock.patch.object(hm, "steamgrid_configured", return_value=True),
        ):
            result = hm._auto_scrape_steamgrid_fill("b1", ["screenshots"], None)
        self.assertEqual(result, {"skipped": "no steamgrid kinds requested"})


# ── handlers/metadata.py: media worker LBDB path ───────────────────────────


class AutoScrapeMediaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database = self.root / "metadata.db"
        self.database.write_bytes(b"stub")
        self.state = {
            "games": [
                {"game_id": "g1", "name": "G1", "import_batch_id": "b9", "launchbox_db_id": "10"},
                {"game_id": "g2", "name": "G2", "import_batch_id": "b9", "launchbox_db_id": "11"},
                {"game_id": "g3", "name": "G3", "import_batch_id": "b9"},
                {"game_id": "g4", "name": "G4", "import_batch_id": "other", "launchbox_db_id": "12"},
            ],
            "settings": {},
        }

    def _patches(self, apply, transact):
        return (
            mock.patch.object(hm, "_wait_for_match_job", return_value="done"),
            mock.patch.object(hm, "METADATA_DATABASE", self.database),
            mock.patch.object(hm, "load_state", return_value=self.state),
            mock.patch.object(hm, "DATA", self.root / "library.json"),
            mock.patch.object(hm, "apply_game_metadata", side_effect=apply),
            mock.patch.object(hm, "transact_state", side_effect=transact),
            mock.patch.object(hm, "bump_media_epoch"),
            mock.patch.object(hm, "_auto_scrape_steamgrid_fill", return_value={"filled": 0}),
        )

    def test_media_worker_applies_lbdb_changes(self):
        def fake_apply(game, *_args):
            if game["game_id"] == "g2":
                raise ValueError("bad row")
            return dict(game, cover="/new/cover.png")

        def fake_transact(mutator):
            mutator(self.state)
            return (None, None)

        patches = self._patches(fake_apply, fake_transact)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6] as bump,
            patches[7] as fill,
        ):
            summary = hm._auto_scrape_media_worker(None, "b9", ["cover"], False)
        self.assertEqual(summary["match_wait"], "done")
        self.assertEqual(summary["targets"], 2)
        self.assertEqual(summary["lbdb"]["updated"], 1)
        self.assertEqual(len(summary["lbdb"]["errors"]), 1)
        self.assertTrue(summary["lbdb"]["errors"][0].startswith("G2: "))
        self.assertEqual(self.state["games"][0]["cover"], "/new/cover.png")
        bump.assert_called_once()
        fill.assert_called_once()

    def test_media_worker_honours_cancellation(self):
        patches = self._patches(apply=mock.Mock(), transact=mock.Mock())
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            summary = hm._auto_scrape_media_worker(_Cancel(cancelled=True), "b9", ["cover"], False)
        self.assertEqual(summary["targets"], 2)
        self.assertEqual(summary["lbdb"]["updated"], 0)
        self.assertEqual(summary["lbdb"]["errors"], [])


# ── handlers/metadata.py: queue_auto_scrape workers ─────────────────────────


class QueueAutoScrapeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database = self.root / "metadata.db"
        self.database.write_bytes(b"stub")

    def _queue(self):
        captured = {}

        class FakeJM:
            def submit(self, name, worker, **kwargs):
                captured[name] = worker
                return {"job_id": f"job-{name}"}

        with (
            mock.patch.object(hm, "JOB_MANAGER", FakeJM()),
            mock.patch.object(hm, "METADATA_DATABASE", self.database),
            mock.patch.object(
                hm,
                "load_state",
                return_value={"settings": {"auto_import_media_types": ["cover"]}},
            ),
            mock.patch.object(hm, "create_match_preview_record", return_value={"preview_id": "p1"}),
        ):
            match_id, media_id, preview_id = hm.queue_auto_scrape("batch-7")
        self.assertEqual(sorted(captured), ["metadata-match:batch-7", "metadata-media:batch-7"])
        self.assertEqual(match_id, "job-metadata-match:batch-7")
        self.assertEqual(media_id, "job-metadata-media:batch-7")
        self.assertEqual(preview_id, "p1")
        return captured

    def test_match_worker_runs_preview_job_and_passes(self):
        captured = self._queue()
        with (
            mock.patch.object(hm, "METADATA_DATABASE", self.database),
            mock.patch.object(hm, "run_match_preview_job") as run_job,
            mock.patch.object(
                hm,
                "load_match_preview",
                return_value={"counts": {"exact": 2}, "auto_applied": ["g1"]},
            ),
            mock.patch.object(hm, "load_state", return_value={"settings": {}}),
        ):
            summary = captured["metadata-match:batch-7"](None)
        run_job.assert_called_once()
        self.assertEqual(summary["lbdb"]["counts"], {"exact": 2})
        self.assertEqual(summary["lbdb"]["auto_applied"], ["g1"])
        self.assertTrue(summary["screenscraper"]["skipped"])
        self.assertTrue(summary["igdb"]["skipped"])

    def test_media_worker_runs_with_database_present(self):
        captured = self._queue()
        with (
            mock.patch.object(hm, "METADATA_DATABASE", self.database),
            mock.patch.object(hm, "_wait_for_match_job", return_value="done") as wait,
            mock.patch.object(hm, "load_state", return_value={"games": [], "settings": {}}),
            mock.patch.object(hm, "bump_media_epoch"),
            mock.patch.object(hm, "_auto_scrape_steamgrid_fill", return_value={"filled": 0}) as fill,
        ):
            summary = captured["metadata-media:batch-7"](None)
        wait.assert_called_once_with("batch-7", None)
        self.assertEqual(summary["targets"], 0)
        self.assertEqual(summary["lbdb"]["updated"], 0)
        fill.assert_called_once_with("batch-7", ["cover"], None)


# ── handlers/metadata.py: auto-scrape route error mapping ──────────────────


class AutoScrapeRouteErrorTests(unittest.TestCase):
    def test_queue_value_error_becomes_bad_request(self):
        handler = _MetadataHandler()
        with (
            mock.patch.object(hm, "load_state", return_value={"settings": {"scrape_after_import": True}}),
            mock.patch.object(hm, "queue_auto_scrape", side_effect=ValueError("bad batch")),
        ):
            with self.assertRaises(BadRequest):
                hm.metadata_auto_scrape(handler, {"import_batch_id": "b1"})


# ── handlers/metadata.py: _lbdb_candidate_urls ─────────────────────────────


class LbdbCandidateUrlsTests(unittest.TestCase):
    def test_candidate_urls_come_from_database(self):
        import sqlite3

        from metadata import IMAGE_URL

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "metadata.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE images (database_id INTEGER, filename TEXT, type TEXT, region TEXT)")
            connection.executemany(
                "INSERT INTO images VALUES (?,?,?,?)",
                [
                    (7, "box.png", "Box - Front", "World"),
                    (7, "", "Box - Front", "World"),
                    (8, "other.png", "Box - Front", "World"),
                ],
            )
            connection.commit()
            connection.close()
            with mock.patch.object(hm, "METADATA_DATABASE", db):
                urls = hm._lbdb_candidate_urls(7)
        kinds = {kind for kind, _url in urls}
        self.assertIn("cover", kinds)
        self.assertIn(("cover", IMAGE_URL + "box.png"), urls)
        self.assertTrue(all(url.startswith(IMAGE_URL) for _kind, url in urls))


# ── handlers/steamgrid.py: hygiene route branches ──────────────────────────


class SteamgridHygieneGapTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.cache = self.root / "cache"
        self.state = {
            "games": [{"game_id": "g1", "name": "One", "cover": ""}],
            "settings": {},
        }

    def _png(self, name):
        path = self.root / name
        path.write_bytes(b"png-bytes")
        return path

    def _run_fix(self, targets, replace=None, cancel=None):
        captured = {}

        def fake_submit(name, worker, **_kwargs):
            captured["worker"] = worker
            return {"job_id": "j1"}

        with (
            mock.patch.object(steamgrid, "_settings", return_value={}),
            mock.patch.object(steamgrid, "provider_available", return_value=True),
            mock.patch.object(steamgrid.openbox, "load_state", return_value=self.state),
            mock.patch.object(steamgrid, "build_report", return_value={"issues": []}),
            mock.patch.object(steamgrid, "select_fixable", return_value=targets),
            mock.patch.object(steamgrid, "_hygiene_replace_one", side_effect=replace),
            mock.patch.object(steamgrid, "write_undo_manifest") as manifest,
            mock.patch.object(steamgrid, "bump_media_epoch"),
            mock.patch.object(steamgrid.JOB_MANAGER, "submit", side_effect=fake_submit),
        ):
            handler = _Handler()
            steamgrid.steamgrid_hygiene_fix(handler, {"fields": ["cover", "background"]})
            status, payload = handler.responses[-1]
            self.assertEqual(status, 202)
            self.assertEqual(payload["job_id"], "j1")
            result = captured["worker"](cancel)
        return result, manifest

    def test_report_unauthorized(self):
        handler = _Handler(authorized=False)
        steamgrid.steamgrid_hygiene_report(handler, SimpleNamespace(query=""))
        self.assertEqual(handler.responses[-1][0], 403)

    def test_report_with_digit_limit(self):
        handler = _Handler()
        with (
            mock.patch.object(steamgrid.openbox, "load_state", return_value=self.state),
            mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache),
            mock.patch.object(steamgrid, "build_report", return_value={"scanned": 9}),
            mock.patch.object(steamgrid, "list_undo_batches", return_value=[]),
        ):
            steamgrid.steamgrid_hygiene_report(handler, SimpleNamespace(query="limit=2"))
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["batches"], [])

    def test_fix_unauthorized(self):
        handler = _Handler(authorized=False)
        steamgrid.steamgrid_hygiene_fix(handler, {})
        self.assertEqual(handler.responses[-1][0], 403)

    def test_fix_rejects_bad_limit(self):
        with (
            mock.patch.object(steamgrid, "_settings", return_value={}),
            mock.patch.object(steamgrid, "provider_available", return_value=True),
        ):
            with self.assertRaises(BadRequest):
                steamgrid.steamgrid_hygiene_fix(_Handler(), {"limit": "abc"})

    def test_fix_worker_mixed_results(self):
        def fake_replace(game, target, _batch_id, records, entry):
            if target["field"] == "background":
                raise ValueError("no art")
            records.append({"game_id": target["game_id"], "field": target["field"]})
            entry["status"] = "applied"

        targets = [
            {"game_id": "g1", "name": "One", "field": "cover"},
            {"game_id": "ghost", "name": "Ghost", "field": "cover"},
            {"game_id": "g1", "name": "One", "field": "background"},
        ]
        cancel = _Cancel()
        result, manifest = self._run_fix(targets, fake_replace, cancel)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertFalse(result["cancelled"])
        self.assertEqual(
            [entry["status"] for entry in result["results"]],
            ["applied", "game_missing", "error"],
        )
        self.assertIn("no art", result["results"][2]["error"])
        manifest.assert_called_once()
        self.assertTrue(cancel.progress_calls)

    def test_fix_worker_honours_cancellation(self):
        targets = [{"game_id": "g1", "name": "One", "field": "cover"}]
        result, manifest = self._run_fix(targets, cancel=_Cancel(cancelled=True))
        self.assertEqual(result["results"], [])
        self.assertEqual(result["applied"], 0)
        self.assertTrue(result["cancelled"])
        manifest.assert_not_called()

    def test_fix_worker_without_cancel_event(self):
        def fake_replace(_game, target, _batch_id, records, entry):
            records.append({"game_id": target["game_id"], "field": target["field"]})
            entry["status"] = "applied"

        targets = [{"game_id": "g1", "name": "One", "field": "cover"}]
        result, _manifest = self._run_fix(targets, fake_replace, None)
        self.assertEqual(result["applied"], 1)
        self.assertFalse(result["cancelled"])

    def test_replace_one_rejects_nameless_game(self):
        entry = {"game_id": "g1", "name": "", "field": "cover"}
        with self.assertRaises(ValueError):
            steamgrid._hygiene_replace_one({"game_id": "g1", "name": ""}, entry, "batch-1", [], entry)

    def test_replace_one_marks_not_found_without_search_hits(self):
        entry = {"game_id": "g1", "name": "One", "field": "cover"}
        with mock.patch.object(steamgrid, "search_games", return_value=[]):
            steamgrid._hygiene_replace_one({"game_id": "g1", "name": "One"}, entry, "batch-1", [], entry)
        self.assertEqual(entry["status"], "not_found")

    def test_replace_one_marks_not_found_without_media_url(self):
        entry = {"game_id": "g1", "name": "One", "field": "cover"}
        with (
            mock.patch.object(steamgrid, "search_games", return_value=[{"id": 7, "name": "One"}]),
            mock.patch.object(steamgrid, "game_assets", return_value=[]),
            mock.patch.object(steamgrid, "choose_media", return_value={}),
        ):
            steamgrid._hygiene_replace_one({"game_id": "g1", "name": "One"}, entry, "batch-1", [], entry)
        self.assertEqual(entry["status"], "not_found")

    def test_undo_unauthorized(self):
        handler = _Handler(authorized=False)
        steamgrid.steamgrid_hygiene_undo(handler, {"batch_id": "batch-1"})
        self.assertEqual(handler.responses[-1][0], 403)

    def _write_manifest(self, batch_id, records):
        return write_undo_manifest(self.cache, batch_id, records)

    def _undo(self, batch_id, extra_patches=()):
        from contextlib import ExitStack

        handler = _Handler()
        patches = [
            mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache),
            mock.patch.object(
                steamgrid,
                "transact_state",
                side_effect=lambda mutate: (mutate(self.state), None),
            ),
            mock.patch.object(steamgrid, "bump_media_epoch"),
            *extra_patches,
        ]
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            steamgrid.steamgrid_hygiene_undo(handler, {"batch_id": batch_id})
        return handler.responses[-1]

    def test_undo_skips_empty_record(self):
        original = self._png("original.png")
        backup_dir = self.cache / "artwork-hygiene" / "batch-e"
        backup_dir.mkdir(parents=True)
        backup = backup_dir / "g1-cover.png"
        backup.write_bytes(original.read_bytes())
        self._write_manifest(
            "batch-e",
            [
                {"game_id": "g1", "field": "cover", "previous": str(original), "created": False, "backup": str(backup)},
                {},
            ],
        )
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": "replaced"}]
        _status, payload = self._undo("batch-e")
        self.assertEqual(payload["counts"], {"removed": 0, "restored": 1})
        self.assertEqual(self.state["games"][0]["cover"], str(original))

    def test_undo_skips_record_when_lookup_raises_bad_request(self):
        self._write_manifest(
            "batch-b",
            [
                {"game_id": "ghost", "field": "cover", "previous": "", "created": True, "backup": ""},
            ],
        )
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": "/c.png"}]
        extra = mock.patch.object(steamgrid, "game_from_payload", side_effect=BadRequest("gone"))
        _status, payload = self._undo("batch-b", extra_patches=[extra])
        self.assertEqual(payload["counts"], {"removed": 0, "restored": 0})

    def test_undo_removed_clears_field(self):
        self._write_manifest(
            "batch-r",
            [
                {"game_id": "g1", "field": "cover", "previous": "", "created": True, "backup": ""},
            ],
        )
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": "/new/cover.png"}]
        _status, payload = self._undo("batch-r")
        self.assertEqual(payload["counts"], {"removed": 1, "restored": 0})
        self.assertEqual(self.state["games"][0]["cover"], "")

    def test_undo_with_deleted_game_raises_indexerror(self):
        """Documents a suspected bug: the mutate guard catches BadRequest,
        but game_from_payload raises IndexError for a deleted game, so the
        whole undo aborts instead of skipping that record."""
        self._write_manifest(
            "batch-g",
            [
                {"game_id": "ghost", "field": "cover", "previous": "", "created": True, "backup": ""},
            ],
        )
        self.state["games"] = [{"game_id": "g1", "name": "One", "cover": "/c.png"}]
        handler = _Handler()
        with (
            mock.patch.object(steamgrid, "_cache_dir", return_value=self.cache),
            mock.patch.object(
                steamgrid,
                "transact_state",
                side_effect=lambda mutate: (mutate(self.state), None),
            ),
            mock.patch.object(steamgrid, "bump_media_epoch"),
        ):
            with self.assertRaises(IndexError):
                steamgrid.steamgrid_hygiene_undo(handler, {"batch_id": "batch-g"})


# ── pkg/parity/parity_screenscraper.py: hash_lookup ─────────────────────────


class HashLookupTests(unittest.TestCase):
    def _payload(self, jeu_id=42):
        return {"response": {"jeu": {"id": jeu_id, "nom": "Test Game", "medias": []}}}

    def test_md5_lookup_posts_and_normalizes(self):
        with mock.patch.object(ss, "ss_request", return_value=self._payload()) as req:
            result = ss.hash_lookup("/tmp/game.sfc", {"md5": "m" * 32, "crc": "c" * 8}, hash_kind="md5")
        self.assertEqual(result["id"], 42)
        self.assertEqual(result["name"], "Test Game")
        endpoint, params = req.call_args[0]
        self.assertEqual(endpoint, "jeuInfos.php")
        self.assertEqual(params["rommd5"], "m" * 32)
        self.assertNotIn("romcrc", params)
        self.assertNotIn("systemeid", params)

    def test_crc_lookup_with_system_id(self):
        with mock.patch.object(ss, "ss_request", return_value=self._payload(7)) as req:
            ss.hash_lookup(
                "/tmp/game.zip",
                {"md5": "m", "crc": "deadbeef"},
                hash_kind="crc",
                system_id="4",
            )
        _endpoint, params = req.call_args[0]
        self.assertEqual(params["romcrc"], "deadbeef")
        self.assertEqual(params["systemeid"], 4)

    def test_cache_hit_skips_request(self):
        with (
            mock.patch.object(ss, "cache_get", return_value=self._payload(9)) as get,
            mock.patch.object(ss, "ss_request") as req,
        ):
            result = ss.hash_lookup(
                "/tmp/game.sfc",
                {"md5": "m", "crc": "c"},
                hash_kind="md5",
                cache_dir="/tmp/cache",
            )
        req.assert_not_called()
        get.assert_called_once()
        self.assertEqual(result["id"], 9)

    def test_cache_miss_stores_response(self):
        with (
            mock.patch.object(ss, "cache_get", return_value=None),
            mock.patch.object(ss, "cache_put") as put,
            mock.patch.object(ss, "ss_request", return_value=self._payload(11)),
        ):
            result = ss.hash_lookup(
                "/tmp/game.sfc",
                {"md5": "m", "crc": "c"},
                hash_kind="md5",
                cache_dir="/tmp/cache",
            )
        put.assert_called_once()
        self.assertEqual(result["id"], 11)

    def test_empty_jeu_raises(self):
        with mock.patch.object(ss, "ss_request", return_value={"response": {}}):
            with self.assertRaises(ValueError):
                ss.hash_lookup("/tmp/game.sfc", {"md5": "m", "crc": "c"}, hash_kind="crc")


if __name__ == "__main__":
    unittest.main()
