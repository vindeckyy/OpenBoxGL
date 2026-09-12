#!/usr/bin/env python3
"""Tests for pkg/parity/parity_memories (S5 memory roots auto-import).

Covers discovery of Steam/RetroArch/Dolphin screenshot roots, sha256 dedupe
against existing media, per-game matching (never wrong-game attach), caps,
the disabled-path no-op, the Activity-job registration, settings plumbing,
and the v2 HTTP routes in handlers/media.py.
"""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_DATA_ROOT = tempfile.TemporaryDirectory(prefix="openbox-memories-test-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_ROOT.name
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pkg" / "parity"))

import pkg.parity  # noqa: E402,F401  # registers the flat parity_* import finder
import parity_memories as memories  # noqa: E402  # flat import == the module handlers use
from parity_memories import (  # noqa: E402
    MemoryCandidate,
    apply_import_plan,
    configured_roots,
    discover_memory_roots,
    match_memory_game,
    memories_import_enabled,
    normalize_memory_key,
    run_import,
)


PNG_A = b"\x89PNG\r\n\x1a\n" + b"a" * 64
PNG_B = b"\x89PNG\r\n\x1a\n" + b"b" * 64
PNG_C = b"\x89PNG\r\n\x1a\n" + b"c" * 64


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _write(path, data=PNG_A):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _steam_root(home, appid="480"):
    """Create <home>/.steam/steam/userdata/<uid>/760/remote/<appid>/screenshots."""
    return home / ".steam" / "steam" / "userdata" / "7" / "760" / "remote" / appid / "screenshots"


def _state(**kwargs):
    state = {"games": [], "settings": {"memories_import_enabled": True}}
    state.update(kwargs)
    return state


class DiscoveryTests(unittest.TestCase):
    def test_discovers_steam_retroarch_dolphin_and_custom_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            steam = _steam_root(home)
            steam.mkdir(parents=True)
            retro = home / ".config" / "retroarch" / "screenshots"
            retro.mkdir(parents=True)
            dolphin = home / ".local" / "share" / "dolphin-emu" / "ScreenShots"
            dolphin.mkdir(parents=True)
            custom = home / "extra-shots"
            custom.mkdir()
            roots, skipped = discover_memory_roots(home=home, extra_roots=[str(custom)])
            kinds = {root["kind"] for root in roots}
            paths = {root["path"] for root in roots}
            self.assertIn("steam", kinds)
            self.assertIn("retroarch", kinds)
            self.assertIn("dolphin", kinds)
            self.assertIn("custom", kinds)
            self.assertIn(steam.parent.parent, paths)  # the remote dir is the steam root
            self.assertEqual(skipped, [])

    def test_steam_remote_dirs_cover_every_account(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for uid in ("111", "222"):
                (
                    home / ".steam" / "steam" / "userdata" / uid / "760" / "remote"
                ).mkdir(parents=True)
            roots, _ = discover_memory_roots(home=home, extra_roots=[])
            remote_dirs = [root["path"] for root in roots if root["kind"] == "steam"]
            self.assertEqual(len(remote_dirs), 2)

    def test_missing_and_unreadable_roots_are_skipped_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            missing = home / "nowhere"
            not_a_dir = _write(home / "file.txt", b"x")
            with self.assertLogs("openbox", level="WARNING"):
                roots, skipped = discover_memory_roots(
                    home=home, extra_roots=[str(missing), str(not_a_dir)]
                )
            self.assertEqual(roots, [])
            self.assertEqual(len(skipped), 2)

    def test_retroarch_cfg_screenshot_directory_is_honored(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cfg_dir = home / ".config" / "retroarch"
            cfg_dir.mkdir(parents=True)
            relocated = home / "ra-shots"
            relocated.mkdir()
            (cfg_dir / "retroarch.cfg").write_text(
                f'screenshot_directory = "{relocated}"\n', encoding="utf-8"
            )
            roots, _ = discover_memory_roots(home=home, extra_roots=[])
            retro_paths = [root["path"] for root in roots if root["kind"] == "retroarch"]
            self.assertIn(relocated, retro_paths)

    def test_discovery_dedupes_identical_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            custom = home / "shots"
            custom.mkdir()
            roots, _ = discover_memory_roots(
                home=home, extra_roots=[str(custom), str(custom) + "/", str(custom.resolve())]
            )
            self.assertEqual(len([r for r in roots if r["kind"] == "custom"]), 1)

    def test_retroarch_cfg_bare_key_line_does_not_crash(self):
        # A malformed cfg line (key with no '=') must not abort discovery.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cfg_dir = home / ".config" / "retroarch"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "retroarch.cfg").write_text(
                "screenshot_directory\nvideo_driver = \"gl\"\n", encoding="utf-8"
            )
            retro = home / ".config" / "retroarch" / "screenshots"
            retro.mkdir()
            roots, _ = discover_memory_roots(home=home, extra_roots=[])
            retro_paths = [root["path"] for root in roots if root["kind"] == "retroarch"]
            self.assertIn(retro, retro_paths)

    def test_retroarch_cfg_default_value_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cfg_dir = home / ".config" / "retroarch"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "retroarch.cfg").write_text(
                'screenshot_directory = "default"\n', encoding="utf-8"
            )
            retro = home / ".config" / "retroarch" / "screenshots"
            retro.mkdir()
            roots, _ = discover_memory_roots(home=home, extra_roots=[])
            retro_paths = [root["path"] for root in roots if root["kind"] == "retroarch"]
            self.assertIn(retro, retro_paths)

    def test_retroarch_cfg_without_key_scans_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cfg_dir = home / ".config" / "retroarch"
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "retroarch.cfg").write_text('video_driver = "gl"\n', encoding="utf-8")
            retro = home / ".config" / "retroarch" / "screenshots"
            retro.mkdir()
            roots, _ = discover_memory_roots(home=home, extra_roots=[])
            self.assertIn(retro, [root["path"] for root in roots if root["kind"] == "retroarch"])

    def test_unreadable_userdata_dir_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            userdata = home / ".steam" / "steam" / "userdata"
            userdata.mkdir(parents=True)
            userdata.chmod(0)
            try:
                roots, _ = discover_memory_roots(home=home, extra_roots=[])
            finally:
                userdata.chmod(0o755)
            self.assertEqual([r for r in roots if r["kind"] == "steam"], [])


class ScanPathTests(unittest.TestCase):
    """Direct coverage over scan_candidates / _iter_image_files branches."""

    def test_steam_scan_skips_missing_screenshots_dir_and_thumbnails(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / "remote"
            (remote / "480").mkdir(parents=True)  # no screenshots dir -> skipped
            shots = remote / "481" / "screenshots"
            _write(shots / "shot.jpg", PNG_A)
            _write(shots / "thumbnails" / "tiny.jpg", PNG_B)
            candidates = list(memories.scan_candidates([{"kind": "steam", "path": remote}]))
            self.assertEqual([c.path.name for c in candidates], ["shot.jpg"])
            self.assertEqual(candidates[0].appid, "481")

    def test_scan_cancel_stops_at_root_boundary_and_mid_walk(self):
        with tempfile.TemporaryDirectory() as directory:
            remote = Path(directory) / "remote"
            shots = remote / "481" / "screenshots"
            _write(shots / "a.jpg", PNG_A)
            _write(shots / "b.jpg", PNG_B)
            custom = Path(directory) / "custom"
            _write(custom / "c.png", PNG_C)

            class AlwaysSet:
                def is_set(self):
                    return True

            roots = [{"kind": "custom", "path": custom}]
            self.assertEqual(
                list(memories.scan_candidates(roots, cancel=AlwaysSet())), []
            )

            class SetAfterFirst:
                def __init__(self):
                    self.calls = 0

                def is_set(self):
                    self.calls += 1
                    return self.calls > 1

            roots = [{"kind": "steam", "path": remote}, {"kind": "custom", "path": custom}]
            candidates = list(memories.scan_candidates(roots, cancel=SetAfterFirst()))
            self.assertEqual(candidates, [])

            # cancel flipping inside the plain-directory walk
            candidates = list(
                memories.scan_candidates(
                    [{"kind": "custom", "path": custom}], cancel=SetAfterFirst()
                )
            )
            self.assertEqual(candidates, [])

    def test_dolphin_walk_yields_title_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "ScreenShots"
            _write(root / "GALE01" / "GALE01-4.png", PNG_A)
            candidates = list(memories.scan_candidates([{"kind": "dolphin", "path": root}]))
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].title_id, "GALE01")

    def test_symlinked_files_and_dirs_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shots"
            root.mkdir()
            real = _write(root / "real.png", PNG_A)
            elsewhere = Path(directory) / "elsewhere"
            _write(elsewhere / "x.png", PNG_B)
            os.symlink(real, root / "linked.png")
            os.symlink(elsewhere, root / "linked_dir")
            candidates = list(memories.scan_candidates([{"kind": "custom", "path": root}]))
            self.assertEqual([c.path.name for c in candidates], ["real.png"])


class DedupeIndexTests(unittest.TestCase):
    def test_from_state_tolerates_malformed_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = _write(Path(directory) / "media" / "shot.png", PNG_A)
            missing = Path(directory) / "gone.png"
            state = {
                "games": [{
                    "game_id": "g1",
                    "memories": [
                        "corrupt",
                        {"sha256": "d", "bytes": "notanint"},
                        {"path": str(existing)},  # size recovered via stat
                        {"path": str(missing)},   # stat fails -> ignored
                    ],
                    "screenshots": [str(missing)],  # unreadable -> ignored
                }],
                "memories_unassigned": [{"sha256": "u", "path": "/u.png", "bytes": 3}],
            }
            index = memories._DedupeIndex.from_state(state)
            self.assertIn("d", index.digests)
            self.assertIn("u", index.digests)
            self.assertIn(existing.stat().st_size, index.sizes)

    def test_is_duplicate_tolerates_unreadable_stored_file(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = _write(Path(directory) / "media" / "shot.png", PNG_A)
            state = {"games": [{"game_id": "g1", "screenshots": [str(existing)]}]}
            index = memories._DedupeIndex.from_state(state)
            real_sha = memories._sha256_file

            def flaky(path):
                if Path(path) == existing:
                    raise OSError("gone")
                return real_sha(path)

            size = existing.stat().st_size
            with mock.patch.object(memories, "_sha256_file", side_effect=flaky):
                self.assertFalse(index.is_duplicate(size, "deadbeef"))
            # second call hits the memoized failure without re-raising
            self.assertFalse(index.is_duplicate(size, "deadbeef"))


class TakenAtTests(unittest.TestCase):
    def test_invalid_steam_stamp_falls_through_to_mtime(self):
        candidate = MemoryCandidate(
            path=Path("/x/99999999999999_1.png"), source="steam"
        )
        taken = memories._parse_taken_at(candidate, 0)
        self.assertTrue(taken.startswith("1970-01-01"))

    def test_retroarch_stamp_parsed(self):
        candidate = MemoryCandidate(
            path=Path("/ra/Game-240101-120000.png"), source="retroarch",
            names=("Game-240101-120000",),
        )
        self.assertTrue(
            memories._parse_taken_at(candidate, 0).startswith("2024-01-01T12:00:00")
        )

    def test_invalid_retroarch_stamp_falls_through(self):
        candidate = MemoryCandidate(
            path=Path("/ra/Game-991399-999999.png"), source="retroarch",
            names=("Game-991399-999999",),
        )
        self.assertTrue(memories._parse_taken_at(candidate, 0).startswith("1970-01-01"))


class MatcherTests(unittest.TestCase):
    def _games(self):
        return [
            {"game_id": "g-steam", "name": "Steam Game", "steam_app_id": "480", "path": "/roms/s.bin"},
            {"game_id": "g-chrono", "name": "Chrono Trigger", "path": "/roms/Chrono Trigger.sfc"},
            {"game_id": "g-melee", "name": "Melee", "rom_name": "GALE01", "path": "/roms/melee.iso"},
            {"game_id": "g-dup-a", "name": "Twin", "path": "/roms/twin-a.iso"},
            {"game_id": "g-dup-b", "name": "Twin", "path": "/roms/twin-b.iso"},
        ]

    def test_steam_appid_matches_exactly(self):
        candidate = MemoryCandidate(path=Path("/x/1.jpg"), source="steam", appid="480")
        game_id, count = match_memory_game(self._games(), candidate)
        self.assertEqual(game_id, "g-steam")
        self.assertEqual(count, 1)

    def test_duplicate_steam_appid_is_ambiguous(self):
        games = self._games() + [
            {"game_id": "g-steam-2", "name": "Again", "steam_app_id": "480"}
        ]
        candidate = MemoryCandidate(path=Path("/x/1.jpg"), source="steam", appid="480")
        game_id, count = match_memory_game(games, candidate)
        self.assertIsNone(game_id)
        self.assertEqual(count, 2)

    def test_retroarch_name_match_strips_timestamp_and_tags(self):
        candidate = MemoryCandidate(
            path=Path("/ra/Chrono Trigger-240101-120000.png"),
            source="retroarch",
            names=("Chrono Trigger-240101-120000",),
        )
        game_id, count = match_memory_game(self._games(), candidate)
        self.assertEqual(game_id, "g-chrono")
        self.assertEqual(count, 1)

    def test_dolphin_title_id_matches_rom_name(self):
        candidate = MemoryCandidate(
            path=Path("/d/GALE01/GALE01-4.png"), source="dolphin", title_id="GALE01"
        )
        game_id, count = match_memory_game(self._games(), candidate)
        self.assertEqual(game_id, "g-melee")
        self.assertEqual(count, 1)

    def test_ambiguous_name_never_attaches(self):
        candidate = MemoryCandidate(
            path=Path("/x/Twin-1.png"), source="custom", names=("Twin-1",)
        )
        game_id, count = match_memory_game(self._games(), candidate)
        self.assertIsNone(game_id)
        self.assertEqual(count, 2)

    def test_unmatched_returns_zero(self):
        candidate = MemoryCandidate(
            path=Path("/x/Nothing-1.png"), source="custom", names=("Nothing-1",)
        )
        game_id, count = match_memory_game(self._games(), candidate)
        self.assertIsNone(game_id)
        self.assertEqual(count, 0)

    def test_normalize_memory_key(self):
        self.assertEqual(normalize_memory_key("Chrono Trigger (USA) [!]"), "chronotrigger")
        self.assertEqual(normalize_memory_key("My Game-240101-120000"), "mygame")
        self.assertEqual(normalize_memory_key(""), "")

    def test_alternate_names_contribute_match_keys(self):
        games = [{"game_id": "g1", "name": "X", "alternate_names": ["Chrono Cross"]}]
        candidate = MemoryCandidate(
            path=Path("/x/Chrono Cross-1.png"), source="custom", names=("Chrono Cross-1",)
        )
        game_id, count = match_memory_game(games, candidate)
        self.assertEqual(game_id, "g1")
        self.assertEqual(count, 1)

    def test_dolphin_title_id_matches_path_stem_when_rom_name_absent(self):
        games = [{"game_id": "g1", "name": "Melee", "path": "/roms/GALE01.iso"}]
        candidate = MemoryCandidate(
            path=Path("/d/GALE01/GALE01-9.png"), source="dolphin", title_id="GALE01"
        )
        game_id, count = match_memory_game(games, candidate)
        self.assertEqual(game_id, "g1")
        self.assertEqual(count, 1)


class ImportTests(unittest.TestCase):
    def _run(self, state, data_root, home, **kwargs):
        return run_import(state, Path(data_root), home=home, **kwargs)

    def test_disabled_means_zero_filesystem_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            state = _state(settings={"memories_import_enabled": False})
            state["games"].append({"game_id": "g1", "name": "Game"})
            with mock.patch.object(
                memories, "discover_memory_roots", side_effect=AssertionError("scanned")
            ):
                summary = self._run(state, data_root, home)
            self.assertFalse(summary["enabled"])
            self.assertEqual(summary["added"], 0)
            self.assertNotIn("memories", state["games"][0])
            self.assertFalse((data_root / "media" / "memories").exists())

    def test_imports_new_files_and_attaches_to_games(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            steam = _steam_root(home, "480")
            _write(steam / "20200101120000_1.jpg", PNG_A)
            state = _state()
            state["games"].append(
                {"game_id": "g1", "name": "Steam Game", "steam_app_id": "480"}
            )
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 1)
            game = state["games"][0]
            self.assertEqual(len(game["memories"]), 1)
            entry = game["memories"][0]
            self.assertEqual(entry["sha256"], _sha256(PNG_A))
            self.assertEqual(entry["source"], "steam")
            self.assertTrue(entry["path"].startswith(str(data_root / "media" / "memories")))
            self.assertTrue(Path(entry["path"]).is_file())
            self.assertTrue(entry["taken_at"].startswith("2020-01-01T12:00:00"))
            self.assertTrue(entry["imported_at"])

    def test_dedupe_against_existing_screenshots_by_content(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            existing = _write(data_root / "media" / "existing.png", PNG_A)
            custom = home / "shots"
            _write(custom / "Game-240101-000000.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append(
                {"game_id": "g1", "name": "Game", "screenshots": [str(existing)]}
            )
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertNotIn("memories", state["games"][0])

    def test_rerun_imports_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            steam = _steam_root(home, "480")
            _write(steam / "20200101120000_1.jpg", PNG_A)
            state = _state()
            state["games"].append(
                {"game_id": "g1", "name": "Steam Game", "steam_app_id": "480"}
            )
            first = self._run(state, data_root, home)
            self.assertEqual(first["added"], 1)
            second = self._run(state, data_root, home)
            self.assertEqual(second["added"], 0)
            self.assertEqual(len(state["games"][0]["memories"]), 1)
            # new file lands on third run only
            _write(steam / "20200102120000_1.jpg", PNG_B)
            third = self._run(state, data_root, home)
            self.assertEqual(third["added"], 1)
            self.assertEqual(len(state["games"][0]["memories"]), 2)

    def test_same_content_two_sources_imports_once(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom_a = home / "a"
            custom_b = home / "b"
            _write(custom_a / "Game-1.png", PNG_A)
            _write(custom_b / "Game-2.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom_a), str(custom_b)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 1)
            self.assertEqual(summary["skipped"], 1)

    def test_caps_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            for index in range(5):
                _write(custom / f"Game-{index}.png", PNG_A + bytes([index]))
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            summary = self._run(
                state, data_root, home,
                limits={"max_files": 2, "max_bytes": 10**9, "max_per_game": 10},
            )
            self.assertEqual(summary["added"], 2)
            self.assertEqual(len(state["games"][0]["memories"]), 2)

    def test_per_game_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            for index in range(3):
                _write(custom / f"Game-{index}.png", PNG_A + bytes([index]))
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            summary = self._run(
                state, data_root, home, limits={"max_per_game": 2}
            )
            self.assertEqual(summary["added"], 2)
            self.assertEqual(summary["skipped"], 1)

    def test_oversized_file_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            big = _write(custom / "Game-1.png", PNG_A * 4)
            _write(custom / "Game-2.png", PNG_B)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            summary = self._run(
                state, data_root, home, limits={"max_file_bytes": len(PNG_A) * 2}
            )
            self.assertEqual(summary["added"], 1)
            self.assertEqual(summary["skipped"], 1)
            paths = [e["path"] for e in state["games"][0]["memories"]]
            self.assertFalse(any(str(big) in p for p in paths))

    def test_ambiguous_match_lands_in_unassigned_bucket(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Twin-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].extend(
                [
                    {"game_id": "a", "name": "Twin", "path": "/x/a.iso"},
                    {"game_id": "b", "name": "Twin", "path": "/x/b.iso"},
                ]
            )
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 0)
            self.assertEqual(summary["unassigned"], 1)
            self.assertNotIn("memories", state["games"][0])
            self.assertEqual(len(state["memories_unassigned"]), 1)
            entry = state["memories_unassigned"][0]
            self.assertEqual(entry["sha256"], _sha256(PNG_A))
            self.assertIn("_unassigned", entry["path"])
            self.assertTrue(Path(entry["path"]).is_file())

    def test_unmatched_files_are_skipped_not_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Nowhere-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 0)
            self.assertEqual(summary["unassigned"], 0)
            self.assertNotIn("memories_unassigned", state)

    def test_unreadable_scan_root_is_skipped_and_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Game-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            with mock.patch.object(
                memories.os, "walk", side_effect=OSError("denied")
            ), self.assertLogs("openbox", level="WARNING"):
                summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 0)
            self.assertTrue(summary["skipped_roots"])

    def test_progress_callback_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            steam = _steam_root(home, "480")
            _write(steam / "20200101120000_1.jpg", PNG_A)
            state = _state()
            state["games"].append(
                {"game_id": "g1", "name": "Steam Game", "steam_app_id": "480"}
            )
            calls = []
            self._run(
                state, data_root, home,
                progress=lambda **kw: calls.append(kw),
            )
            self.assertTrue(any(c.get("phase") for c in calls))

    def test_cancel_stops_import(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            for index in range(4):
                _write(custom / f"Game-{index}.png", PNG_A + bytes([index]))
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})

            class CancelAfterFirst:
                def __init__(self):
                    self.calls = 0

                def is_set(self):
                    self.calls += 1
                    return self.calls > 2

            summary = self._run(state, data_root, home, cancel=CancelAfterFirst())
            self.assertTrue(summary["cancelled"])
            self.assertLess(summary["added"], 4)

    def test_candidate_stat_failure_counts_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            victim = _write(custom / "Game-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            real_stat = Path.stat

            def flaky_stat(self, *args, **kwargs):
                if self == victim:
                    raise OSError("gone")
                return real_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", new=flaky_stat):
                summary = self._run(state, data_root, home)
            self.assertEqual(summary["failed"], 1)
            self.assertTrue(summary["errors"])
            self.assertNotIn("memories", state["games"][0])

    def test_candidate_hash_failure_counts_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            victim = _write(custom / "Game-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            real_sha = memories._sha256_file

            def flaky_sha(path):
                if Path(path) == victim:
                    raise OSError("io")
                return real_sha(path)

            with mock.patch.object(memories, "_sha256_file", side_effect=flaky_sha):
                summary = self._run(state, data_root, home)
            self.assertEqual(summary["failed"], 1)
            self.assertTrue(summary["errors"])

    def test_copy_failure_counts_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Game-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            with mock.patch.object(
                memories, "_copy_into_media", side_effect=OSError("disk full")
            ):
                summary = self._run(state, data_root, home)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["added"], 0)

    def test_existing_dest_is_reused_without_recopy(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Game-1.png", PNG_A)
            digest = _sha256(PNG_A)
            # Pre-place the content-addressed destination so the copier reuses it.
            dest_dir = data_root / "media" / "memories" / "g1"
            dest = _write(dest_dir / f"{digest}.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 1)
            self.assertEqual(state["games"][0]["memories"][0]["path"], str(dest))

    def test_dest_stat_race_still_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Game-1.png", PNG_A)
            digest = _sha256(PNG_A)
            dest = _write(
                data_root / "media" / "memories" / "g1" / f"{digest}.png", PNG_B
            )
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].append({"game_id": "g1", "name": "Game"})
            real_stat = Path.stat

            def flaky_stat(self, *args, **kwargs):
                # is_file() uses os.stat directly; the in-try size probe is the
                # patched Path.stat call that must fail.
                if self == dest:
                    raise OSError("race")
                return real_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", new=flaky_stat):
                summary = self._run(state, data_root, home)
            self.assertEqual(summary["added"], 1)
            self.assertTrue(dest.is_file())

    def test_unassigned_bucket_cap_skips_new_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            custom = home / "shots"
            _write(custom / "Twin-1.png", PNG_A)
            state = _state(settings={
                "memories_import_enabled": True,
                "memories_import_roots": [str(custom)],
            })
            state["games"].extend([
                {"game_id": "a", "name": "Twin"},
                {"game_id": "b", "name": "Twin"},
            ])
            state["memories_unassigned"] = [
                {"sha256": f"u{i}", "path": f"/u/{i}.png"}
                for i in range(memories.MAX_UNASSIGNED)
            ]
            summary = self._run(state, data_root, home)
            self.assertEqual(summary["unassigned"], 0)
            self.assertEqual(len(state["memories_unassigned"]), memories.MAX_UNASSIGNED)

    def test_non_numeric_limit_values_fall_back_to_defaults(self):
        limits = memories._default_limits({"max_files": "junk", "max_bytes": 5})
        self.assertEqual(limits["max_files"], memories.MAX_IMPORT_FILES)
        self.assertEqual(limits["max_bytes"], 5)


class ApplyPlanTests(unittest.TestCase):
    def test_apply_plan_attaches_and_bounds_unassigned(self):
        state = _state()
        state["games"].append({"game_id": "g1", "name": "Game"})
        plan = {
            "attach": {"g1": [{"path": "/m/a.png", "sha256": "x", "bytes": 1,
                              "source": "custom", "taken_at": "", "imported_at": "now"}]},
            "unassigned": [{"path": "/m/u.png", "sha256": "y", "bytes": 1,
                            "source": "custom", "taken_at": "", "imported_at": "now",
                            "hint": "orphan"}],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 1)
        self.assertEqual(state["games"][0]["memories"][0]["sha256"], "x")
        self.assertEqual(state["memories_unassigned"][0]["hint"], "orphan")

    def test_apply_plan_missing_game_counts_failed(self):
        state = _state()
        plan = {
            "attach": {"ghost": [{"path": "/m/a.png", "sha256": "x", "bytes": 1,
                                  "source": "custom", "taken_at": "", "imported_at": "n"}]},
            "unassigned": [],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 0)
        self.assertEqual(counts["failed"], 1)

    def test_apply_plan_replaces_duplicate_sha_entries(self):
        state = _state()
        state["games"].append({
            "game_id": "g1", "name": "Game",
            "memories": [{"path": "/old.png", "sha256": "x"}],
        })
        plan = {
            "attach": {"g1": [{"path": "/new.png", "sha256": "x", "bytes": 2,
                              "source": "steam", "taken_at": "", "imported_at": "n"}]},
            "unassigned": [],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 0)
        self.assertEqual(len(state["games"][0]["memories"]), 1)

    def test_apply_plan_bad_max_per_game_falls_back(self):
        state = _state()
        state["games"].append({"game_id": "g1", "name": "Game"})
        plan = {
            "attach": {"g1": [{"path": "/m/a.png", "sha256": "x", "bytes": 1,
                              "source": "s", "taken_at": "", "imported_at": "n"}]},
            "unassigned": [],
            "max_per_game": "junk",
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 1)

    def test_apply_plan_resolves_legacy_game_id_alias(self):
        state = _state()
        state["games"].append({
            "game_id": "new-id", "name": "Game", "legacy_game_ids": ["old-id"],
        })
        plan = {
            "attach": {"old-id": [{"path": "/m/a.png", "sha256": "x", "bytes": 1,
                                   "source": "s", "taken_at": "", "imported_at": "n"}]},
            "unassigned": [],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 1)
        self.assertEqual(state["games"][0]["memories"][0]["sha256"], "x")

    def test_apply_plan_breaks_when_game_already_at_cap(self):
        state = _state()
        state["games"].append({
            "game_id": "g1", "name": "Game",
            "memories": [{"path": "/m/old.png", "sha256": "a"}],
        })
        plan = {
            "attach": {"g1": [{"path": "/m/new.png", "sha256": "b", "bytes": 1,
                               "source": "s", "taken_at": "", "imported_at": "n"}]},
            "unassigned": [],
            "max_per_game": 1,
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["attached"], 0)
        self.assertEqual(len(state["games"][0]["memories"]), 1)

    def test_apply_plan_heals_non_list_unassigned(self):
        state = _state()
        state["memories_unassigned"] = "corrupt"
        plan = {
            "attach": {},
            "unassigned": [{"path": "/m/u.png", "sha256": "y", "bytes": 1,
                            "source": "s", "taken_at": "", "imported_at": "n"}],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["unassigned"], 1)
        self.assertEqual(state["memories_unassigned"][0]["sha256"], "y")

    def test_apply_plan_unassigned_cap_and_dedupe(self):
        state = _state()
        state["memories_unassigned"] = [
            {"sha256": f"u{i}"} for i in range(memories.MAX_UNASSIGNED)
        ]
        plan = {
            "attach": {},
            "unassigned": [
                {"path": "/m/a.png", "sha256": "u0", "bytes": 1,
                 "source": "s", "taken_at": "", "imported_at": "n"},
                {"path": "/m/b.png", "sha256": "new", "bytes": 1,
                 "source": "s", "taken_at": "", "imported_at": "n"},
            ],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        # bucket already at cap -> the loop breaks on the first plan entry
        self.assertEqual(counts["unassigned"], 0)
        self.assertEqual(len(state["memories_unassigned"]), memories.MAX_UNASSIGNED)

    def test_apply_plan_skips_duplicate_unassigned_sha(self):
        state = _state()
        state["memories_unassigned"] = [{"sha256": "y"}]
        plan = {
            "attach": {},
            "unassigned": [{"path": "/m/u.png", "sha256": "y", "bytes": 1,
                            "source": "s", "taken_at": "", "imported_at": "n"}],
            "counts": {},
        }
        counts = apply_import_plan(state, plan)
        self.assertEqual(counts["unassigned"], 0)
        self.assertEqual(len(state["memories_unassigned"]), 1)


class SettingsPlumbingTests(unittest.TestCase):
    def test_known_settings_includes_memories_keys(self):
        from settings_schema import KNOWN_SETTINGS, sanitize_settings

        self.assertIn("memories_import_enabled", KNOWN_SETTINGS)
        self.assertIn("memories_import_roots", KNOWN_SETTINGS)
        clean, dropped = sanitize_settings(
            {"memories_import_enabled": True, "memories_import_roots": ["/x"]}
        )
        self.assertEqual(dropped, [])
        self.assertTrue(clean["memories_import_enabled"])

    def test_clean_settings_normalizes_roots(self):
        from handlers.settings import clean_settings

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shots"
            root.mkdir()
            merged = {
                "memories_import_enabled": True,
                "memories_import_roots": [str(root), "", str(root)],
            }
            clean = clean_settings(merged)
            self.assertTrue(clean["memories_import_enabled"])
            self.assertEqual(clean["memories_import_roots"], [str(root)])
        clean = clean_settings({"memories_import_roots": None})
        self.assertEqual(clean["memories_import_roots"], [])

    def test_clean_settings_rejects_missing_root(self):
        from handlers.settings import clean_settings

        with self.assertRaises(ValueError):
            clean_settings({"memories_import_roots": ["/definitely/missing/dir"]})
        with self.assertRaises(ValueError):
            clean_settings({"memories_import_roots": ["relative/bad"]})
        with self.assertRaises(ValueError):
            clean_settings({"memories_import_roots": "not-a-list"})

    def test_public_settings_exposes_memories_keys(self):
        from pkg.state.cache import _public_settings_uncached

        public = _public_settings_uncached({
            "settings": {
                "memories_import_enabled": True,
                "memories_import_roots": ["/shots"],
            },
            "games": [],
        })
        self.assertTrue(public["memories_import_enabled"])
        self.assertEqual(public["memories_import_roots"], ["/shots"])

    def test_public_game_projection_includes_sanitized_memories(self):
        from openbox import save_state
        from webapp_state import DATA, public_state

        media_file = _write(
            Path(DATA).parent / "media" / "memories" / "gx" / "p.png", PNG_C
        )
        save_state({
            "games": [{
                "name": "Proj", "path": "/bin/true",
                "memories": [
                    {"path": str(media_file), "sha256": "p", "bytes": 5,
                     "source": "steam", "taken_at": "t", "imported_at": "i"},
                    {"path": "/not/approved/evil.png", "sha256": "e"},
                    "corrupt",
                ],
            }],
            "profiles": {}, "history": [], "playlists": [], "ui_state": {},
            "trash": [], "active_sessions": [], "settings": {},
        })
        view = public_state()
        game = view["games"][0]
        self.assertEqual(game["memories"][0]["sha256"], "p")
        self.assertEqual(game["memories"][0]["path"], str(media_file))
        self.assertEqual(len(game["memories"]), 1)
        self.assertTrue(game["has_memories"])

    def test_helpers(self):
        self.assertTrue(memories_import_enabled({"memories_import_enabled": True}))
        self.assertFalse(memories_import_enabled({}))
        self.assertFalse(memories_import_enabled(None))
        self.assertEqual(configured_roots({"memories_import_roots": [" /a ", "", "/b"]}), ["/a", "/b"])


class JobRegistrationTests(unittest.TestCase):
    def test_job_maps_to_operation_type_and_title(self):
        from job_manager import operation_title_for_name, operation_type_for_name

        self.assertEqual(operation_type_for_name("memories-import"), "media.memories_import")
        self.assertIn("memor", operation_title_for_name("memories-import").casefold())

    def test_operation_policy_registered(self):
        from pkg.state.operations import OPERATION_POLICIES

        self.assertIn("media.memories_import", OPERATION_POLICIES)


class _FakeMediaHandler:
    def __init__(self):
        self.sent = []
        self.headers = {}

    def send_json(self, status, payload, extra_headers=None):
        self.sent.append(("json", status, payload))

    def send_response(self, code, message=None):
        self.sent.append(("status", code))

    def headers_common(self, *args, **kwargs):
        pass

    def send_header(self, name, value):
        self.sent.append(("header", name, value))

    def end_headers(self):
        self.sent.append(("end",))


class _FakeCancel:
    def __init__(self):
        self.progress_calls = []

    def is_set(self):
        return False

    def progress(self, **kwargs):
        self.progress_calls.append(kwargs)
        return kwargs


class MemoriesRouteTests(unittest.TestCase):
    def _handler(self):
        from handlers.media import MediaHandlers

        class Handler(_FakeMediaHandler, MediaHandlers):
            pass

        return Handler()

    def _parsed(self, query=""):
        from urllib.parse import urlsplit

        return urlsplit(f"/api/v2/memories?{query}")

    def test_import_disabled_returns_noop_without_job(self):
        from handlers import media as media_mod

        state = _state(settings={"memories_import_enabled": False})
        handler = self._handler()
        with mock.patch.object(media_mod, "load_state", return_value=state), mock.patch.object(
            media_mod.JOB_MANAGER, "submit"
        ) as submit:
            handler._api_post_api_v2_memories_import({})
        kind, status, payload = handler.sent[-1]
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        submit.assert_not_called()

    def test_import_enabled_submits_job_and_worker_imports(self):
        from handlers import media as media_mod

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data_root = Path(directory) / "data"
            steam = _steam_root(home, "480")
            _write(steam / "20200101120000_1.jpg", PNG_A)
            state = _state()
            state["games"].append(
                {"game_id": "g1", "name": "Steam Game", "steam_app_id": "480"}
            )
            captured = {}

            def fake_submit(name, worker, **kwargs):
                captured["name"] = name
                captured["worker"] = worker
                return {"job_id": "job-1", "state": "queued"}

            handler = self._handler()
            with mock.patch.object(media_mod, "load_state", return_value=state), mock.patch.object(
                media_mod.JOB_MANAGER, "submit", side_effect=fake_submit
            ), mock.patch.object(media_mod, "DATA", data_root / "library.json"), mock.patch.object(
                media_mod, "transact_state", side_effect=lambda fn: (state, fn(state))
            ), mock.patch.object(media_mod, "bump_media_epoch"):
                handler._api_post_api_v2_memories_import({})
                kind, status, payload = handler.sent[-1]
                self.assertEqual(status, 202)
                self.assertEqual(payload["job_id"], "job-1")
                self.assertEqual(captured["name"], "memories-import")
                # run the captured worker synchronously against the fixture home
                real_discover = memories.discover_memory_roots
                def discover_with_home(*, home=None, extra_roots=None):
                    return real_discover(home=home or Path(home_dir), extra_roots=extra_roots)
                home_dir = home
                with mock.patch.object(
                    memories, "discover_memory_roots", side_effect=discover_with_home
                ) as disc:
                    result = captured["worker"](_FakeCancel())
                    self.assertTrue(disc.called)
                self.assertEqual(result["added"], 1)
                self.assertEqual(len(state["games"][0]["memories"]), 1)

    def test_import_worker_rechecks_enabled_before_scanning(self):
        from handlers import media as media_mod

        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory)
            enabled_state = _state(settings={"memories_import_enabled": True})
            disabled_state = _state(settings={"memories_import_enabled": False})
            captured = {}

            def fake_submit(name, worker, **kwargs):
                captured["worker"] = worker
                return {"job_id": "job-2", "state": "queued"}

            handler = self._handler()
            states = iter([enabled_state, disabled_state])
            with mock.patch.object(
                media_mod, "load_state", side_effect=lambda: next(states)
            ), mock.patch.object(
                media_mod.JOB_MANAGER, "submit", side_effect=fake_submit
            ), mock.patch.object(
                media_mod, "DATA", data_root / "library.json"
            ):
                handler._api_post_api_v2_memories_import({})
            with mock.patch.object(
                memories, "discover_memory_roots", side_effect=AssertionError("scanned")
            ):
                result = captured["worker"](_FakeCancel())
            self.assertFalse(result["enabled"])
            self.assertEqual(result["added"], 0)

    def test_status_route_reports_counts_without_scanning(self):
        from handlers import media as media_mod

        state = _state(settings={"memories_import_enabled": True})
        state["games"].append({
            "game_id": "g1",
            "name": "Game",
            "memories": [{"path": "/m/a.png", "sha256": "x"}],
        })
        state["memories_unassigned"] = [{"path": "/m/u.png", "sha256": "y"}]
        handler = self._handler()
        with mock.patch.object(media_mod, "load_state_view", return_value=state), mock.patch.object(
            media_mod.JOB_MANAGER, "snapshot", return_value={"state": "done"}
        ), mock.patch.object(
            memories, "discover_memory_roots", side_effect=AssertionError("scanned")
        ):
            handler._api_get_api_v2_memories_status(self._parsed())
        kind, status, payload = handler.sent[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["memories"], 1)
        self.assertEqual(payload["unassigned"], 1)
        self.assertEqual(payload["games_with_memories"], 1)
        self.assertEqual(payload["job"], {"state": "done"})

    def _media_dir(self):
        """The real approved media root for this test process."""
        from webapp_state import DATA

        return Path(DATA).parent / "media"

    def test_list_route_returns_sanitized_memories(self):
        from handlers import media as media_mod

        media_file = _write(self._media_dir() / "memories" / "g1" / "a.png", PNG_A)
        outside = _write(self._media_dir().parent / "outside.png", PNG_B)
        state = _state()
        state["games"].append({
            "game_id": "g1",
            "name": "Game",
            "memories": [
                {"path": str(media_file), "sha256": "x", "source": "steam",
                 "taken_at": "", "imported_at": "", "bytes": 3},
                {"path": str(outside.resolve()), "sha256": "y"},
                "corrupt-entry",
            ],
        })
        handler = self._handler()
        with mock.patch.object(media_mod, "load_state_view", return_value=state):
            handler._api_get_api_v2_memories(self._parsed("game_id=g1"))
        kind, status, payload = handler.sent[-1]
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["memories"]), 1)
        self.assertEqual(payload["memories"][0]["sha256"], "x")

    def test_list_route_unassigned_bucket(self):
        from handlers import media as media_mod

        media_file = _write(
            self._media_dir() / "memories" / "_unassigned" / "u.png", PNG_A
        )
        state = _state()
        state["memories_unassigned"] = [
            {"path": str(media_file), "sha256": "u", "hint": "orphan"}
        ]
        handler = self._handler()
        with mock.patch.object(media_mod, "load_state_view", return_value=state):
            handler._api_get_api_v2_memories(self._parsed("unassigned=1"))
        kind, status, payload = handler.sent[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["memories"][0]["hint"], "orphan")

    def test_media_route_404s_for_bad_index(self):
        from api_errors import MediaNotFound
        from handlers import media as media_mod

        state = _state()
        state["games"].append({"game_id": "g1", "name": "Game", "memories": []})
        handler = self._handler()
        with mock.patch.object(media_mod, "load_state_view", return_value=state):
            with self.assertRaises(MediaNotFound):
                handler._api_get_api_v2_memories_media(self._parsed("game_id=g1&index=9"))

    def test_media_route_answers_416_on_range_error(self):
        from handlers import media as media_mod

        media_file = _write(self._media_dir() / "memories" / "g1" / "r.png", PNG_A)
        state = _state()
        state["games"].append({
            "game_id": "g1", "name": "Game",
            "memories": [{"path": str(media_file), "sha256": "x"}],
        })
        handler = self._handler()

        def bad_send_file(*args, **kwargs):
            raise ValueError("bad range")

        handler.send_file = bad_send_file
        with mock.patch.object(media_mod, "load_state_view", return_value=state):
            handler._api_get_api_v2_memories_media(self._parsed("game_id=g1&index=0"))
        statuses = [entry for entry in handler.sent if entry[0] == "status"]
        self.assertIn(("status", 416), statuses)


class LiveRouteTests(unittest.TestCase):
    """End-to-end coverage over the real dispatch + send_file stack."""

    @classmethod
    def setUpClass(cls):
        import web_app
        from openbox import save_state

        cls._media = Path(os.environ["OPENBOX_DATA_DIR"]) / "media"
        attached = _write(cls._media / "memories" / "g1" / "a.png", PNG_A)
        orphan = _write(cls._media / "memories" / "_unassigned" / "u.png", PNG_B)
        save_state({
            "games": [{
                "game_id": "g1", "name": "Game", "path": "/bin/true",
                "memories": [{
                    "path": str(attached), "sha256": _sha256(PNG_A), "bytes": len(PNG_A),
                    "source": "steam", "taken_at": "", "imported_at": "now",
                }],
            }],
            "profiles": {}, "history": [], "playlists": [],
            "ui_state": {}, "trash": [], "active_sessions": [],
            "memories_unassigned": [{
                "path": str(orphan), "sha256": _sha256(PNG_B), "bytes": len(PNG_B),
                "source": "custom", "taken_at": "", "imported_at": "now", "hint": "orphan",
            }],
            "settings": {"memories_import_enabled": False},
        })
        web_app.TOKEN = "memories-test-token"
        cls._server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._base = f"http://127.0.0.1:{cls._server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._server.server_close()

    def _get(self, path, method="GET", payload=None):
        url = f"{self._base}{path}{'&' if '?' in path else '?'}token=memories-test-token"
        request = urllib.request.Request(url, method=method)
        if payload is not None:
            request.data = json.dumps(payload).encode()
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_media_route_serves_attached_memory(self):
        status, body = self._get("/api/v2/memories/media?id=0&index=0")
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_A)

    def test_media_route_serves_unassigned_bucket(self):
        status, body = self._get("/api/v2/memories/media?bucket=unassigned&index=0")
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG_B)

    def test_media_route_404s_for_bad_index(self):
        status, _ = self._get("/api/v2/memories/media?id=0&index=9")
        self.assertEqual(status, 404)

    def test_list_and_status_routes_over_real_dispatch(self):
        status, body = self._get("/api/v2/memories?id=0")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["memories"][0]["sha256"], _sha256(PNG_A))
        status, body = self._get("/api/v2/memories?unassigned=1")
        payload = json.loads(body)
        self.assertEqual(payload["memories"][0]["hint"], "orphan")
        status, body = self._get("/api/v2/memories/status")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["memories"], 1)
        self.assertEqual(payload["unassigned"], 1)

    def test_import_route_disabled_noop_over_http(self):
        status, body = self._get("/api/v2/memories/import", method="POST", payload={})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["enabled"])


class RouteTableTests(unittest.TestCase):
    def test_v2_routes_registered_in_tables(self):
        import routes as routes_mod

        self.assertIn("/api/v2/memories", routes_mod.GET_TABLE)
        self.assertIn("/api/v2/memories/status", routes_mod.GET_TABLE)
        self.assertIn("/api/v2/memories/media", routes_mod.GET_TABLE)
        self.assertIn("/api/v2/memories/import", routes_mod.POST_TABLE)

    def test_routes_in_registry(self):
        from routes.registry import get_routes_for_method

        get_routes = get_routes_for_method("GET")
        post_routes = get_routes_for_method("POST")
        self.assertIn("/api/v2/memories", get_routes)
        self.assertIn("/api/v2/memories/status", get_routes)
        self.assertIn("/api/v2/memories/media", get_routes)
        self.assertIn("/api/v2/memories/import", post_routes)


if __name__ == "__main__":
    unittest.main()
