"""Tests for setup preview/commit parity helpers."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import (  # noqa: E402
    BadRequest,
    PreviewEntryLimitExceeded,
    PreviewExpired,
    PreviewLibraryChanged,
    PreviewLimitExceeded,
    PreviewNotFound,
    PreviewStale,
    UnresolvedCandidates,
)
from pkg.parity.parity_import import generated_m3u_dir, import_multi_platform  # noqa: E402
from pkg.parity.parity_setup_preview import (  # noqa: E402
    MAX_DECISION_BATCH,
    MAX_ENTRIES,
    MAX_PREVIEWS,
    _adapter_installed,
    _encode_cursor,
    _flatpak_installed,
    apply_decisions,
    classify_candidates,
    classify_emulator_readiness,
    commit_preview,
    compute_summary,
    create_preview_record,
    emulator_choices_for_platform,
    file_fingerprint,
    folder_fingerprint,
    library_signature,
    list_preview_items,
    load_preview,
    preview_document,
    revalidate_preview_record,
    run_scan_job,
    previews_dir,
    save_preview,
    scan_sources,
)
from pkg.state.operations import get_operation_service  # noqa: E402


class ParitySetupPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.data_dir)
        import openbox
        from state_store import JsonStateStore

        openbox.APP_DIR = self.data_dir
        openbox.DATA = self.data_dir / "library.json"
        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        openbox.DATA.write_text(json.dumps({"schema_version": 6, "games": [], "settings": {}}))

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _preview_with_items(self, items):
        preview = create_preview_record(sources=[{"type": "files", "id": "f1", "paths": ["/x"]}], options={})
        preview["items"] = items
        preview["counts"] = {"additions": len(items), "merges": 0, "duplicates": 0, "ambiguities": 0, "exclusions": 0, "unsupported": 0, "errors": 0}
        preview["scanned_entries"] = len(items)
        save_preview(preview)
        return preview

    def test_import_multi_platform_write_m3u_false_creates_no_beside_m3u(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Game (Disc 1).iso").write_bytes(b"1")
            (root / "Game (Disc 2).iso").write_bytes(b"2")
            imported = import_multi_platform(root, {".iso"}, {".iso": "Disc image"}, write_m3u=False)
            self.assertFalse(any(path.suffix == ".m3u" for path in root.iterdir()))
            self.assertEqual(len(imported[0]["discs"]), 2)

    def test_import_multi_platform_generated_dir_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Game (Disc 1).iso").write_bytes(b"1")
            (root / "Game (Disc 2).iso").write_bytes(b"2")
            target = generated_m3u_dir()
            import_multi_platform(root, {".iso"}, {".iso": "Disc image"}, m3u_dir=target)
            self.assertFalse(any(path.suffix == ".m3u" for path in root.iterdir()))
            self.assertTrue(any(path.suffix == ".m3u" for path in target.iterdir()))

    def test_default_import_multi_platform_still_writes_beside_discs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Game (Disc 1).iso").write_bytes(b"1")
            (root / "Game (Disc 2).iso").write_bytes(b"2")
            import_multi_platform(root, {".iso"}, {".iso": "Disc image"})
            self.assertTrue(any(path.name.endswith(".m3u") for path in root.iterdir()))

    def test_import_multi_platform_m3u_filter_and_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disc1 = root / "Game (Disc 1).iso"
            disc2 = root / "Game (Disc 2).iso"
            disc1.write_bytes(b"1")
            disc2.write_bytes(b"2")
            (root / "Game.m3u").write_text(f"{disc1.name}\n{disc2.name}\n", encoding="utf-8")
            calls = []

            def progress(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise RuntimeError("progress boom")

            imported = import_multi_platform(
                root,
                {".iso", ".m3u"},
                {".iso": "Disc image", ".m3u": "Disc image"},
                write_m3u=False,
                progress_callback=progress,
            )
            self.assertTrue(imported)
            self.assertTrue(calls)

    def test_import_multi_platform_missing_folder(self):
        with self.assertRaises(ValueError):
            import_multi_platform("/path/does/not/exist", {".nes"}, {".nes": "NES"})

    def test_import_multi_platform_m3u_resolve_oserror_and_progress_swallow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disc = root / "disc.iso"
            disc.write_bytes(b"1")
            (root / "bundle.m3u").write_text("disc.iso\n", encoding="utf-8")
            with mock.patch.object(Path, "resolve", side_effect=OSError("resolve failed")):
                import_multi_platform(root, {".iso", ".m3u"}, {".iso": "Disc image", ".m3u": "Disc image"}, write_m3u=False)
            (root / "solo.nes").write_bytes(b"NES")
            import_multi_platform(
                root,
                {".nes"},
                {".nes": "NES"},
                progress_callback=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
            )

    def test_preview_retention_cap_rejects_eleventh_preview(self):
        for index in range(MAX_PREVIEWS):
            create_preview_record(
                sources=[{"type": "files", "id": f"s{index}", "paths": [f"/rom{index}.nes"]}],
                options={},
            )
        with self.assertRaises(PreviewLimitExceeded):
            create_preview_record(sources=[{"type": "files", "id": "extra", "paths": ["/x.nes"]}], options={})

    def test_expired_preview_returns_preview_expired(self):
        preview = create_preview_record(sources=[{"type": "files", "id": "f1", "paths": ["/x"]}], options={})
        preview["expires_at"] = "2000-01-01T00:00:00+00:00"
        save_preview(preview)
        with self.assertRaises(PreviewExpired):
            load_preview(preview["preview_id"])

    def test_entry_limit_errors_on_overflow(self):
        games = [
            {
                "name": f"Game {index}",
                "platform": "NES",
                "path": f"/roms/{index}.nes",
                "_source": {"type": "files", "id": str(index), "label": str(index), "path": f"/roms/{index}.nes"},
            }
            for index in range(MAX_ENTRIES + 1)
        ]
        with self.assertRaises(PreviewEntryLimitExceeded):
            classify_candidates(games)

    def test_cursor_pagination_bound_to_preview_revision(self):
        items = []
        for index in range(5):
            items.append(
                {
                    "candidate_id": f"c{index}",
                    "group": "additions",
                    "source": {"type": "files", "id": str(index), "label": str(index), "path": f"/g{index}.nes"},
                    "detected_title": f"Game {index}",
                    "detected_platform": "NES",
                    "intended_action": "import",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": f"Game {index}", "platform": "NES", "path": f"/g{index}.nes"},
                    "_identity": f"path:/g{index}.nes",
                }
            )
        preview = self._preview_with_items(items)
        first = list_preview_items(preview["preview_id"], limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = list_preview_items(preview["preview_id"], cursor=first["next_cursor"], limit=2)
        self.assertEqual(len(second["items"]), 2)
        preview["revision"] = 2
        save_preview(preview)
        with self.assertRaises(PreviewStale):
            list_preview_items(preview["preview_id"], cursor=first["cursor"] or _encode_cursor(preview["preview_id"], 1, 0), limit=2)

    def test_revalidate_detects_stale_fingerprints(self):
        preview = create_preview_record(
            sources=[{"type": "folder", "id": "roms", "path": str(self.data_dir / "roms")}],
            options={},
            source_fingerprints={"0:folder:roms": "old"},
            items=[],
        )
        (self.data_dir / "roms").mkdir()
        with mock.patch("pkg.parity.parity_setup_preview.scan_sources", return_value=([], {"0:folder:roms": "new"})):
            with self.assertRaises(PreviewStale):
                revalidate_preview_record(preview["preview_id"])

    def test_commit_idempotent_twice(self):
        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        item = {
            "candidate_id": "c1",
            "group": "additions",
            "source": {"type": "files", "id": "1", "label": "game.nes", "path": str(rom)},
            "detected_title": "game",
            "detected_platform": "NES",
            "intended_action": "import",
            "existing_game_target": None,
            "warnings": [],
            "emulator_choices": [],
            "selected_emulator_id": None,
            "selected_adapter_id": None,
            "launch_setup": None,
            "merge_diff": None,
            "_game": {"name": "game", "platform": "NES", "path": str(rom)},
            "_identity": f"path:{rom.resolve()}",
        }
        preview = self._preview_with_items([item])
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            first = commit_preview(preview["preview_id"], revision=1, emulator_choices=[])
            second = commit_preview(preview["preview_id"], revision=1, emulator_choices=[])
        self.assertEqual(first["added"], 1)
        self.assertEqual(second["added"], 1)
        state = json.loads((self.data_dir / "library.json").read_text())
        self.assertEqual(len(state["games"]), 1)

    def test_decisions_persist_selected_fields(self):
        preview = self._preview_with_items(
            [
                {
                    "candidate_id": "c1",
                    "group": "additions",
                    "source": {"type": "files", "id": "1", "label": "g", "path": "/g.nes"},
                    "detected_title": "g",
                    "detected_platform": "NES",
                    "intended_action": "import",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [{"adapter_id": "retroarch-nes", "emulator_id": "retroarch", "label": "RA", "recommended": True, "flatpak_app_id": None}],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": "g", "platform": "NES", "path": "/g.nes"},
                    "_identity": "path:/g.nes",
                }
            ]
        )
        apply_decisions(
            preview["preview_id"],
            [
                {
                    "candidate_id": "c1",
                    "action": "import",
                    "merge_target": None,
                    "emulator_id": "retroarch",
                    "adapter_id": "retroarch-nes",
                    "launch_setup": "adapter",
                }
            ],
        )
        page = list_preview_items(preview["preview_id"], limit=10)
        row = page["items"][0]
        self.assertEqual(row["selected_emulator_id"], "retroarch")
        self.assertEqual(row["selected_adapter_id"], "retroarch-nes")
        self.assertEqual(row["launch_setup"], "adapter")

    def test_preview_document_has_no_items_key(self):
        preview = create_preview_record(sources=[{"type": "files", "id": "f1", "paths": ["/x"]}], options={})
        doc = preview_document(preview)
        self.assertNotIn("items", doc)
        self.assertIn("counts", doc)
        self.assertIn("revision", doc)

    def test_library_signature_changes_when_library_changes(self):
        before = library_signature()
        state = json.loads((self.data_dir / "library.json").read_text())
        state["games"] = [{"game_id": "1", "name": "Game", "platform": "NES", "path": "/g.nes"}]
        (self.data_dir / "library.json").write_text(json.dumps(state))
        import openbox
        from state_store import JsonStateStore

        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        after = library_signature()
        self.assertNotEqual(before, after)

    def test_emulator_readiness_buckets_without_preflight(self):
        from pkg.parity.parity_setup_preview import classify_emulator_readiness

        ready_game = {
            "game_id": "r1",
            "platform": "NES",
            "path": str(self.data_dir / "ready.nes"),
            "emulator_adapter_id": "retroarch-nes",
            "emulator_id": "retroarch",
        }
        warn_game = {
            "game_id": "w1",
            "platform": "NES",
            "path": str(self.data_dir / "warn.nes"),
            "emulator_adapter_id": "retroarch-nes",
            "emulator_id": "retroarch",
        }
        blocked_game = {"game_id": "b1", "platform": "NES", "path": str(self.data_dir / "block.bin")}
        unknown_game = {
            "game_id": "u1",
            "platform": "NES",
            "path": str(self.data_dir / "custom.nes"),
            "launch": "/bin/sh {path}",
        }
        (self.data_dir / "ready.nes").write_bytes(b"NES")
        (self.data_dir / "warn.nes").write_bytes(b"NES")
        (self.data_dir / "block.bin").write_bytes(b"X")
        (self.data_dir / "custom.nes").write_bytes(b"NES")

        adapter = {
            "adapter_id": "retroarch-nes",
            "emulator_id": "retroarch",
            "native_exe": "retroarch",
            "flatpak_app_id": None,
            "executable_patterns": [],
        }

        with mock.patch("pkg.parity.parity_setup_preview.find_adapter", return_value=adapter), mock.patch(
            "pkg.parity.parity_setup_preview._adapter_installed",
            return_value=True,
        ):
            self.assertEqual(classify_emulator_readiness(ready_game), "ready")
        with mock.patch("pkg.parity.parity_setup_preview.find_adapter", return_value=adapter), mock.patch(
            "pkg.parity.parity_setup_preview._adapter_installed",
            return_value=False,
        ):
            self.assertEqual(classify_emulator_readiness(warn_game), "warning")
        self.assertEqual(classify_emulator_readiness(blocked_game), "blocked")
        self.assertEqual(classify_emulator_readiness(unknown_game), "unknown")

    def test_merge_fixture_has_merge_diff(self):
        existing = {"game_id": "g1", "name": "Halo", "platform": "Windows", "path": "/old/path", "steam_app_id": "123"}
        proposed = {
            "name": "Halo",
            "platform": "Windows",
            "path": "/new/path",
            "steam_app_id": "123",
            "_source": {"type": "steam", "id": "123", "label": "steam", "path": "/new/path"},
        }
        items = classify_candidates([proposed], state={"games": [existing], "settings": {}})
        merge_item = next(item for item in items if item["group"] == "merges")
        self.assertIsNotNone(merge_item["merge_diff"])
        self.assertTrue(any(row.get("effect") for row in merge_item["merge_diff"]))

    def test_preview_item_contains_required_keys(self):
        item = {
            "candidate_id": "c1",
            "group": "additions",
            "source": {"type": "steam", "id": "570", "label": "Steam", "path": "/steam"},
            "detected_title": "Dota",
            "detected_platform": "Windows",
            "intended_action": "import",
            "existing_game_target": None,
            "warnings": [],
            "emulator_choices": [{"adapter_id": "wine", "emulator_id": "wine", "label": "Wine", "recommended": True, "flatpak_app_id": "org.wine.Wine"}],
            "selected_emulator_id": None,
            "selected_adapter_id": None,
            "launch_setup": None,
            "merge_diff": None,
            "_game": {"name": "Dota"},
            "_identity": "steam:570",
        }
        preview = self._preview_with_items([item])
        row = list_preview_items(preview["preview_id"], limit=1)["items"][0]
        for key in (
            "candidate_id",
            "group",
            "source",
            "detected_title",
            "detected_platform",
            "intended_action",
            "existing_game_target",
            "warnings",
            "emulator_choices",
            "selected_emulator_id",
            "selected_adapter_id",
            "launch_setup",
            "merge_diff",
        ):
            self.assertIn(key, row)
        self.assertIn("flatpak_app_id", row["emulator_choices"][0])

    def test_commit_writes_emulator_fields_for_adapter_launch_setup(self):
        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        item = {
            "candidate_id": "c1",
            "group": "additions",
            "source": {"type": "files", "id": "1", "label": "game.nes", "path": str(rom)},
            "detected_title": "game",
            "detected_platform": "NES",
            "intended_action": "import",
            "existing_game_target": None,
            "warnings": [],
            "emulator_choices": [],
            "selected_emulator_id": "retroarch",
            "selected_adapter_id": "retroarch-nes",
            "launch_setup": "adapter",
            "merge_diff": None,
            "_game": {"name": "game", "platform": "NES", "path": str(rom)},
            "_identity": f"path:{rom.resolve()}",
        }
        preview = self._preview_with_items([item])
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            commit_preview(
                preview["preview_id"],
                revision=1,
                emulator_choices=[
                    {
                        "candidate_id": "c1",
                        "emulator_id": "retroarch",
                        "adapter_id": "retroarch-nes",
                        "launch_setup": "adapter",
                    }
                ],
            )
        state = json.loads((self.data_dir / "library.json").read_text())
        game = state["games"][0]
        self.assertEqual(game["emulator_id"], "retroarch")
        self.assertEqual(game["emulator_adapter_id"], "retroarch-nes")

    def test_commit_incomplete_does_not_write_emulator_fields(self):
        rom = self.data_dir / "game2.nes"
        rom.write_bytes(b"NES")
        item = {
            "candidate_id": "c2",
            "group": "additions",
            "source": {"type": "files", "id": "2", "label": "game2.nes", "path": str(rom)},
            "detected_title": "game2",
            "detected_platform": "NES",
            "intended_action": "import",
            "existing_game_target": None,
            "warnings": [],
            "emulator_choices": [],
            "selected_emulator_id": None,
            "selected_adapter_id": None,
            "launch_setup": "incomplete",
            "merge_diff": None,
            "_game": {"name": "game2", "platform": "NES", "path": str(rom)},
            "_identity": f"path:{rom.resolve()}",
        }
        preview = self._preview_with_items([item])
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            commit_preview(
                preview["preview_id"],
                revision=1,
                emulator_choices=[
                    {
                        "candidate_id": "c2",
                        "emulator_id": None,
                        "adapter_id": None,
                        "launch_setup": "incomplete",
                    }
                ],
            )
        state = json.loads((self.data_dir / "library.json").read_text())
        game = state["games"][0]
        self.assertNotIn("emulator_id", game)
        self.assertNotIn("emulator_adapter_id", game)

    def test_pinned_preview_not_counted_against_cap(self):
        service = get_operation_service()
        for index in range(MAX_PREVIEWS):
            preview = create_preview_record(
                sources=[{"type": "files", "id": f"s{index}", "paths": [f"/rom{index}.nes"]}],
                options={},
            )
            operation = service.create(operation_type="setup.scan", title=f"scan-{index}")
            preview["job_id"] = operation["job_id"]
            save_preview(preview)
        extra = create_preview_record(sources=[{"type": "files", "id": "extra", "paths": ["/x.nes"]}], options={})
        self.assertTrue(extra["preview_id"])

    def test_compute_summary_required_keys_and_bucket_sum(self):
        rom = self.data_dir / "nes.nes"
        rom.write_bytes(b"NES")
        state = {
            "games": [
                {
                    "game_id": "r1",
                    "name": "Ready",
                    "platform": "NES",
                    "path": str(rom),
                    "emulator_adapter_id": "retroarch-nes",
                    "emulator_id": "retroarch",
                }
            ],
            "settings": {},
        }
        adapter = {
            "adapter_id": "retroarch-nes",
            "emulator_id": "retroarch",
            "native_exe": "retroarch",
            "flatpak_app_id": None,
            "executable_patterns": [],
        }
        with mock.patch("pkg.parity.parity_setup_preview.find_adapter", return_value=adapter), mock.patch(
            "pkg.parity.parity_setup_preview._adapter_installed",
            return_value=True,
        ):
            summary = compute_summary(state=state)
        for key in (
            "library_count",
            "source_coverage",
            "metadata_match_percent",
            "media_gaps",
            "duplicate_count",
            "missing_paths",
            "emulator_readiness",
            "active_operations",
            "next_action",
        ):
            self.assertIn(key, summary)
        readiness = summary["emulator_readiness"]
        total = readiness["ready"] + readiness["warning"] + readiness["blocked"] + readiness["unknown"]
        self.assertEqual(total, summary["library_count"])

    def test_fingerprints_and_adapter_detection_helpers(self):
        path = self.data_dir / "fp.bin"
        path.write_bytes(b"x")
        self.assertIn(str(path.resolve()), file_fingerprint(path))
        folder = self.data_dir / "folder"
        folder.mkdir()
        self.assertTrue(folder_fingerprint(folder))
        self.assertTrue(folder_fingerprint("/missing/path").startswith("missing:"))
        with mock.patch("shutil.which", return_value="/usr/bin/flatpak"), mock.patch(
            "subprocess.run",
            return_value=type("R", (), {"returncode": 0})(),
        ):
            self.assertTrue(_flatpak_installed("org.test.App"))
        adapter = {"native_exe": "retroarch", "flatpak_app_id": None, "executable_patterns": []}
        with mock.patch("shutil.which", return_value="/usr/bin/retroarch"):
            self.assertTrue(_adapter_installed(adapter))

    def test_emulator_choices_for_platform_and_readiness_branches(self):
        self.assertEqual(emulator_choices_for_platform(None), [])
        exe = self.data_dir / "game.sh"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        self.assertEqual(classify_emulator_readiness({"platform": "Linux", "path": str(exe)}), "ready")
        self.assertEqual(
            classify_emulator_readiness({"platform": "NES", "path": str(self.data_dir / "x.bin"), "launch": "custom"}),
            "unknown",
        )

    def test_scan_sources_files_and_folder(self):
        rom = self.data_dir / "scan.nes"
        rom.write_bytes(b"NES")
        folder = self.data_dir / "roms"
        folder.mkdir()
        (folder / "a.nes").write_bytes(b"NES")
        scanned, fps = scan_sources(
            [
                {"type": "files", "paths": [str(rom)]},
                {"type": "folder", "path": str(folder)},
            ]
        )
        self.assertGreaterEqual(len(scanned), 2)
        self.assertEqual(len(fps), 2)

    def test_scan_sources_importer_wrappers(self):
        game = {"name": "Steam Game", "platform": "Windows", "steam_app_id": "42", "path": "/steam"}
        patches = {
            "steam": ("importers.import_steam", game),
            "heroic": ("importers.import_heroic", {**game, "heroic_app_id": "h1", "source": "heroic"}),
            "lutris": ("importers.import_lutris", {**game, "lutris_id": "l1"}),
            "faugus": ("parity_faugus.scan_faugus_games", game),
            "scummvm": ("parity_import.import_scummvm", {**game, "scummvm_id": "sc1"}),
            "rpcs3": ("parity_import.import_rpcs3_hdd", game),
            "vita3k": ("parity_import.import_vita3k", game),
        }
        for source_type, (module_path, row) in patches.items():
            with mock.patch(module_path, return_value=[row]):
                scanned, fps = scan_sources([{"type": source_type, "id": source_type}])
                self.assertEqual(len(scanned), 1)
                self.assertIn(f"0:{source_type}:{source_type}", fps)

    def test_scan_sources_xbox360_arcade_and_storefront(self):
        folder = self.data_dir / "xbox"
        folder.mkdir()
        game = {"name": "360", "platform": "Xbox 360", "path": str(folder / "g.iso")}
        with mock.patch("parity_premium.import_xbox360_folder", return_value=[game]):
            scanned, fps = scan_sources([{"type": "xbox360", "path": str(folder)}])
            self.assertEqual(len(scanned), 1)
        arcade_dir = self.data_dir / "arcade"
        arcade_dir.mkdir()
        arcade_game = {"name": "Pac", "platform": "Arcade", "path": str(arcade_dir / "p.zip")}
        with mock.patch("arcade.import_arcade", return_value=[arcade_game]):
            scanned, fps = scan_sources(
                [{"type": "arcade", "path": str(arcade_dir), "dat_path": None, "set_type": "split", "adapter_id": "mame"}]
            )
            self.assertEqual(len(scanned), 1)
        catalog = [{"name": "Catalog", "platform": "Windows", "steam_app_id": "9", "installed": True}]
        with mock.patch("parity_storefront.storefront_catalog", return_value=catalog), mock.patch(
            "parity_storefront.catalog_entries_to_games",
            return_value=[{"name": "Catalog", "platform": "Windows", "gameyfin_id": "9", "path": "/p"}],
        ):
            scanned, fps = scan_sources(
                [{"type": "gameyfin", "include_uninstalled": True}],
                {"include_owned_uninstalled": True},
            )
            self.assertEqual(len(scanned), 1)

    def test_scan_sources_bad_requests(self):
        with self.assertRaises(BadRequest):
            scan_sources([{"type": "folder", "path": ""}])
        with self.assertRaises(BadRequest):
            scan_sources([{"type": "files", "paths": []}])
        with self.assertRaises(BadRequest):
            scan_sources([{"type": "bogus"}])

    def test_run_scan_job_success_and_error(self):
        rom = self.data_dir / "job.nes"
        rom.write_bytes(b"NES")
        preview = create_preview_record(
            sources=[{"type": "files", "id": "j", "paths": [str(rom)]}],
            options={},
        )
        preview["_request_sources"] = [{"type": "files", "paths": [str(rom)]}]
        save_preview(preview)
        result = run_scan_job(preview["preview_id"])
        self.assertGreaterEqual(result["scanned_entries"], 1)
        updated = load_preview(preview["preview_id"])
        self.assertEqual(updated["state"], "ready")
        bad = create_preview_record(sources=[{"type": "files", "id": "b", "paths": ["/x"]}], options={})
        bad["_request_sources"] = [{"type": "unsupported"}]
        save_preview(bad)
        with self.assertRaises(BadRequest):
            run_scan_job(bad["preview_id"])
        errored = load_preview(bad["preview_id"], allow_expired=True)
        self.assertEqual(errored["state"], "error")

    def test_classify_candidates_groups(self):
        existing_path = self.data_dir / "dup.nes"
        existing_path.write_bytes(b"NES")
        other_path = self.data_dir / "other.nes"
        other_path.write_bytes(b"NES")
        state = {
            "games": [{"game_id": "g1", "name": "Dup", "platform": "NES", "path": str(existing_path)}],
            "settings": {"import_exclusions": [{"source": "steam", "external_id": "99"}]},
        }
        games = [
            {
                "name": "Dup",
                "platform": "NES",
                "path": str(existing_path),
                "_source": {"type": "files", "id": "dup", "label": "dup", "path": str(existing_path)},
            },
            {
                "name": "Steam Excluded",
                "platform": "Windows",
                "path": str(other_path),
                "steam_app_id": "99",
                "_source": {"type": "steam", "id": "99", "label": "steam", "path": str(other_path)},
            },
            {
                "name": "No platform",
                "path": str(self.data_dir / "raw.bin"),
                "_source": {"type": "files", "id": "1", "label": "raw", "path": "/raw.bin"},
            },
            {
                "name": "New",
                "platform": "MysteryNoAdapters",
                "path": str(self.data_dir / "new.bin"),
                "_source": {"type": "files", "id": "2", "label": "new", "path": "/new.bin"},
            },
        ]
        (self.data_dir / "raw.bin").write_bytes(b"x")
        (self.data_dir / "new.bin").write_bytes(b"x")
        with mock.patch("pkg.parity.parity_setup_preview._registry", return_value={"by_platform": {}}):
            items = classify_candidates(games, state=state)
        groups = {item["group"] for item in items}
        self.assertIn("duplicates", groups)
        self.assertIn("ambiguities", groups)
        self.assertIn("unsupported", groups)
        excluded_only = [
            {
                "name": "Steam Excluded",
                "platform": "Windows",
                "path": str(other_path),
                "steam_app_id": "99",
                "_source": {"type": "steam", "id": "99", "label": "steam", "path": str(other_path)},
            }
        ]
        with mock.patch("pkg.parity.parity_setup_preview.filter_imported", side_effect=lambda rows, _state: rows):
            exclusion_items = classify_candidates(excluded_only, state=state)
        self.assertEqual(exclusion_items[0]["group"], "exclusions")

    def test_apply_decisions_validation_paths(self):
        preview = self._preview_with_items(
            [
                {
                    "candidate_id": "c1",
                    "group": "additions",
                    "source": {"type": "files", "id": "1", "label": "g", "path": "/g.nes"},
                    "detected_title": "g",
                    "detected_platform": "NES",
                    "intended_action": "import",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": "g", "platform": "NES", "path": "/g.nes"},
                    "_identity": "path:/g.nes",
                }
            ]
        )
        with self.assertRaises(BadRequest):
            apply_decisions(preview["preview_id"], [{"candidate_id": "missing", "action": "import"}])
        with self.assertRaises(BadRequest):
            apply_decisions(preview["preview_id"], [{"candidate_id": "c1", "action": "bogus"}])
        with self.assertRaises(BadRequest):
            apply_decisions(
                preview["preview_id"],
                [{"candidate_id": "c1", "action": "merge", "merge_target": None}],
            )
        with self.assertRaises(BadRequest):
            apply_decisions(
                preview["preview_id"],
                [{"candidate_id": "c1", "action": "import", "launch_setup": "adapter"}],
            )
        oversized = [
            {
                "candidate_id": "c1",
                "action": "import",
                "merge_target": None,
                "emulator_id": None,
                "adapter_id": None,
                "launch_setup": None,
            }
            for _ in range(MAX_DECISION_BATCH + 1)
        ]
        with self.assertRaises(BadRequest):
            apply_decisions(preview["preview_id"], oversized)

    def test_revalidate_library_changed(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x"]}],
            options={},
            source_fingerprints={"0:files:/x": "fp"},
            items=[],
        )
        with mock.patch("pkg.parity.parity_setup_preview.scan_sources", return_value=([], {"0:files:/x": "fp"})), mock.patch(
            "pkg.parity.parity_setup_preview.library_signature",
            return_value="changed",
        ):
            with self.assertRaises(PreviewLibraryChanged):
                revalidate_preview_record(preview["preview_id"])

    def test_revalidate_success_sets_flag(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x"]}],
            options={},
            source_fingerprints={"0:files:/x": "fp"},
            items=[],
        )
        with mock.patch("pkg.parity.parity_setup_preview.scan_sources", return_value=([], {"0:files:/x": "fp"})):
            doc = revalidate_preview_record(preview["preview_id"])
            self.assertTrue(doc.get("revalidated"))

    def test_commit_stale_revision_and_unresolved(self):
        preview = self._preview_with_items(
            [
                {
                    "candidate_id": "amb",
                    "group": "ambiguities",
                    "source": {"type": "files", "id": "1", "label": "g", "path": "/g.bin"},
                    "detected_title": "g",
                    "detected_platform": None,
                    "intended_action": "review",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": "g", "path": "/g.bin"},
                    "_identity": "path:/g.bin",
                }
            ]
        )
        with self.assertRaises(PreviewStale):
            commit_preview(preview["preview_id"], revision=99, emulator_choices=[])
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            with self.assertRaises(UnresolvedCandidates):
                commit_preview(preview["preview_id"], revision=1, emulator_choices=[])

    def test_commit_merge_skip_exclude_and_disc_staging(self):
        existing = {"game_id": "eg1", "name": "Merge Me", "platform": "NES", "path": "/old.nes"}
        merge_rom = self.data_dir / "merge.nes"
        merge_rom.write_bytes(b"NES")
        skip_rom = self.data_dir / "skip.nes"
        skip_rom.write_bytes(b"NES")
        disc1 = self.data_dir / "Game (Disc 1).iso"
        disc2 = self.data_dir / "Game (Disc 2).iso"
        disc1.write_bytes(b"1")
        disc2.write_bytes(b"2")
        items = [
            {
                "candidate_id": "merge",
                "group": "merges",
                "source": {"type": "files", "id": "m", "label": "m", "path": str(merge_rom)},
                "detected_title": "Merge Me",
                "detected_platform": "NES",
                "intended_action": "merge",
                "existing_game_target": {"game_id": "eg1", "title": "Merge Me", "platform": "NES"},
                "warnings": [],
                "emulator_choices": [],
                "selected_emulator_id": None,
                "selected_adapter_id": None,
                "launch_setup": None,
                "merge_diff": [{"field": "path", "current": "/old.nes", "proposed": str(merge_rom), "effect": "fill"}],
                "_game": {"name": "Merge Me", "platform": "NES", "path": str(merge_rom)},
                "_identity": f"path:{merge_rom.resolve()}",
            },
            {
                "candidate_id": "skip",
                "group": "duplicates",
                "source": {"type": "files", "id": "s", "label": "s", "path": str(skip_rom)},
                "detected_title": "skip",
                "detected_platform": "NES",
                "intended_action": "skip",
                "existing_game_target": None,
                "warnings": [],
                "emulator_choices": [],
                "selected_emulator_id": None,
                "selected_adapter_id": None,
                "launch_setup": None,
                "merge_diff": None,
                "_game": {"name": "skip", "platform": "NES", "path": str(skip_rom)},
                "_identity": f"path:{skip_rom.resolve()}",
            },
            {
                "candidate_id": "disc",
                "group": "additions",
                "source": {"type": "files", "id": "d", "label": "d", "path": str(disc1)},
                "detected_title": "Game",
                "detected_platform": "Disc image",
                "intended_action": "import",
                "existing_game_target": None,
                "warnings": [],
                "emulator_choices": [],
                "selected_emulator_id": None,
                "selected_adapter_id": None,
                "launch_setup": "install_flatpak",
                "merge_diff": None,
                "_game": {"name": "Game", "platform": "Disc image", "discs": [str(disc1), str(disc2)]},
                "_identity": "path:" + str(disc1.resolve()),
            },
        ]
        preview = self._preview_with_items(items)
        state_path = self.data_dir / "library.json"
        state = json.loads(state_path.read_text())
        state["games"] = [existing]
        state_path.write_text(json.dumps(state))
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            result = commit_preview(preview["preview_id"], revision=1, emulator_choices=[])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["added"], 1)
        final = json.loads(state_path.read_text())
        self.assertEqual(len(final["games"]), 2)
        self.assertTrue(any(path.suffix == ".m3u" for path in generated_m3u_dir().iterdir()))

    def test_load_preview_not_found_and_invalid_cursor(self):
        with self.assertRaises(PreviewNotFound):
            load_preview("missing-id")
        preview = self._preview_with_items([])
        with self.assertRaises(PreviewStale):
            list_preview_items(preview["preview_id"], cursor="not-a-cursor", limit=1)

    def test_expired_pinned_preview_loadable(self):
        service = get_operation_service()
        preview = create_preview_record(sources=[{"type": "files", "id": "p", "paths": ["/x"]}], options={})
        operation = service.create(operation_type="setup.scan", title="pin")
        service.mark_running(operation["job_id"])
        preview["job_id"] = operation["job_id"]
        preview["expires_at"] = "2000-01-01T00:00:00+00:00"
        save_preview(preview)
        loaded = load_preview(preview["preview_id"])
        self.assertEqual(loaded["preview_id"], preview["preview_id"])

    def test_helper_edge_paths(self):
        from pkg.parity.parity_setup_preview import (
            _candidate_id,
            _data_path,
            _identity_for_game,
            _next_action,
            _preview_job_pinned,
            previews_dir,
        )

        self.assertEqual(_data_path(self.data_dir), self.data_dir / "library.json")
        self.assertTrue(previews_dir().is_dir() or previews_dir().parent.exists())
        self.assertTrue(_candidate_id("files", "1", "id", "/p"))
        self.assertTrue(_identity_for_game({"scummvm_id": "s1"}).startswith("scummvm:"))
        self.assertTrue(_identity_for_game({"path": str(self.data_dir / "g.nes")}).startswith("path:"))
        self.assertTrue(_identity_for_game({"name": "N", "platform": "NES"}).startswith("name:"))
        self.assertTrue(file_fingerprint("/missing/file").startswith("missing:"))
        folder = self.data_dir / "fp2"
        folder.mkdir()
        original_path_stat = Path.stat

        def selective_stat(self_path, *args, **kwargs):
            if self_path == folder:
                raise OSError("nope")
            return original_path_stat(self_path, *args, **kwargs)

        with mock.patch.object(Path, "stat", selective_stat):
            self.assertTrue(folder_fingerprint(folder))
        with mock.patch("shutil.which", return_value=None):
            self.assertFalse(_flatpak_installed("org.test.App"))
        adapter = {"flatpak_app_id": "org.test.App", "native_exe": "", "executable_patterns": []}
        with mock.patch("shutil.which", return_value="/usr/bin/flatpak"), mock.patch(
            "pkg.parity.parity_setup_preview._flatpak_installed",
            return_value=True,
        ):
            self.assertTrue(_adapter_installed(adapter))
        with mock.patch("pkg.parity.parity_setup_preview._registry", return_value={"by_platform": {"MysteryNoAdapters": []}}):
            self.assertEqual(
                classify_emulator_readiness({"platform": "MysteryNoAdapters", "path": "/x.bin"}),
                "unknown",
            )
        with mock.patch(
            "pkg.parity.parity_setup_preview._registry",
            return_value={"by_platform": {"NES": [{"adapter_id": "a1"}]}},
        ):
            self.assertEqual(classify_emulator_readiness({"platform": "NES", "path": "/x.bin"}), "blocked")
        summary_state = {
            "games": [
                {"game_id": "1", "name": "A", "platform": "NES", "path": "/missing.nes"},
                {"game_id": "2", "name": "B", "platform": "NES", "path": "/dup.nes"},
                {"game_id": "3", "name": "C", "platform": "NES", "path": "/dup.nes"},
            ],
            "settings": {},
        }
        with mock.patch("pkg.parity.parity_setup_preview.classify_emulator_readiness", return_value="ready"):
            summary = compute_summary(state=summary_state)
        self.assertGreater(summary["missing_paths"], 0)
        self.assertGreater(summary["duplicate_count"], 0)
        self.assertEqual(_next_action(library_count=0, readiness={}, metadata_match_percent=0, media_gaps=0, active_operations=0)["id"], "add_sources")
        self.assertEqual(
            _next_action(library_count=1, readiness={}, metadata_match_percent=0, media_gaps=0, active_operations=1)["id"],
            "health",
        )
        self.assertEqual(
            _next_action(library_count=1, readiness={"blocked": 1}, metadata_match_percent=100, media_gaps=0, active_operations=0)["id"],
            "fix_launch",
        )
        self.assertEqual(
            _next_action(library_count=1, readiness={}, metadata_match_percent=50, media_gaps=0, active_operations=0)["id"],
            "review_metadata",
        )
        self.assertEqual(
            _next_action(library_count=1, readiness={}, metadata_match_percent=100, media_gaps=2, active_operations=0)["id"],
            "download_media",
        )
        preview = create_preview_record(sources=[{"type": "files", "id": "p", "paths": ["/x"]}], options={})
        self.assertFalse(_preview_job_pinned(preview))
        service = get_operation_service()
        operation = service.create(operation_type="other.type", title="other")
        preview["job_id"] = operation["job_id"]
        self.assertFalse(_preview_job_pinned(preview))

    def test_scan_sources_skips_missing_files_and_bad_paths(self):
        with self.assertRaises(BadRequest):
            scan_sources([{"type": "xbox360", "path": ""}])
        with self.assertRaises(BadRequest):
            scan_sources([{"type": "arcade", "path": ""}])
        scanned, _fps = scan_sources([{"type": "files", "paths": ["/missing/file.nes", str(self.data_dir / "ok.nes")]}])
        (self.data_dir / "ok.nes").write_bytes(b"NES")
        scanned, _fps = scan_sources([{"type": "files", "paths": ["/missing/file.nes", str(self.data_dir / "ok.nes")]}])
        self.assertEqual(len(scanned), 1)
        catalog = [{"name": "Installed", "platform": "Windows", "gameyfin_id": "1", "installed": True}]
        with mock.patch("parity_storefront.storefront_catalog", return_value=catalog), mock.patch(
            "parity_storefront.catalog_entries_to_games",
            return_value=[{"name": "Installed", "platform": "Windows", "gameyfin_id": "1", "path": "/p"}],
        ):
            scanned, _fps = scan_sources([{"type": "gameyfin"}], {})
            self.assertEqual(len(scanned), 1)

    def test_classify_merge_decision_sets_merge_target(self):
        preview = self._preview_with_items(
            [
                {
                    "candidate_id": "merge2",
                    "group": "merges",
                    "source": {"type": "files", "id": "1", "label": "m", "path": "/m.nes"},
                    "detected_title": "m",
                    "detected_platform": "NES",
                    "intended_action": "import",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": "m", "platform": "NES", "path": "/m.nes"},
                    "_identity": "path:/m.nes",
                }
            ]
        )
        apply_decisions(
            preview["preview_id"],
            [{"candidate_id": "merge2", "action": "merge", "merge_target": "target-id", "emulator_id": None, "adapter_id": None, "launch_setup": None}],
        )
        updated = load_preview(preview["preview_id"])
        self.assertEqual(updated["items"][0]["existing_game_target"]["game_id"], "target-id")

    def test_commit_keep_custom_and_duplicate_skip(self):
        rom = self.data_dir / "keep.nes"
        rom.write_bytes(b"NES")
        state_path = self.data_dir / "library.json"
        state = json.loads(state_path.read_text())
        state["games"] = [{"game_id": "exist", "name": "exist", "platform": "NES", "path": str(rom), "steam_app_id": "7"}]
        state_path.write_text(json.dumps(state))
        item = {
            "candidate_id": "keep",
            "group": "additions",
            "source": {"type": "files", "id": "1", "label": "keep.nes", "path": str(rom)},
            "detected_title": "keep",
            "detected_platform": "NES",
            "intended_action": "import",
            "existing_game_target": None,
            "warnings": [],
            "emulator_choices": [],
            "selected_emulator_id": None,
            "selected_adapter_id": None,
            "launch_setup": "keep_custom",
            "merge_diff": None,
            "_game": {"name": "keep", "platform": "NES", "path": str(rom), "steam_app_id": "7"},
            "_identity": "steam:7",
        }
        preview = self._preview_with_items([item])
        with mock.patch("pkg.parity.parity_setup_preview.revalidate_preview_record", return_value=preview_document(preview)):
            result = commit_preview(
                preview["preview_id"],
                revision=1,
                emulator_choices=[{"candidate_id": "keep", "emulator_id": None, "adapter_id": None, "launch_setup": "keep_custom"}],
            )
        self.assertEqual(result["skipped"], 1)
        final = json.loads(state_path.read_text())
        self.assertEqual(len(final["games"]), 1)

    def test_load_preview_corrupt_file(self):
        previews_dir().mkdir(parents=True, exist_ok=True)
        bad = previews_dir() / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PreviewNotFound):
            load_preview("bad")


if __name__ == "__main__":
    unittest.main()
