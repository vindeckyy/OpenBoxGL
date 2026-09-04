#!/usr/bin/env python3
"""Tests for LaunchBox XML migration import (parity_launchbox_import)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder
from pkg.parity.parity_launchbox_import import (  # noqa: E402
    parse_launchbox_xml,
    preview_import,
    apply_import,
)

_SAMPLE_XML = """<?xml version="1.0" encoding="utf-8"?>
<LaunchBox>
  <Game>
    <ID>1001</ID>
    <Title>Quake</Title>
    <ApplicationPath>C:\\Games\\Quake\\quake.exe</ApplicationPath>
    <Platform>PC</Platform>
    <Genre>Shooter</Genre>
    <Developer>id Software</Developer>
    <Publisher>id Software</Publisher>
    <ReleaseDate>1996-06-22</ReleaseDate>
    <Rating>4.5</Rating>
    <EmulatorId>emu-1</EmulatorId>
    <Notes>The original Quake.</Notes>
  </Game>
  <Game>
    <ID>1002</ID>
    <Title>Doom</Title>
    <ApplicationPath>C:\\Games\\Doom\\doom.exe</ApplicationPath>
    <Platform>PC</Platform>
    <Genre>Shooter</Genre>
    <EmulatorId>emu-2</EmulatorId>
  </Game>
  <Game>
    <ID></ID>
    <Title>No ID Game</Title>
  </Game>
  <Game>
    <ID>1003</ID>
    <Title></Title>
  </Game>
</LaunchBox>
"""


def _write_sample(tmpdir: Path) -> Path:
    p = tmpdir / "platform.xml"
    p.write_text(_SAMPLE_XML, encoding="utf-8")
    return p


class ParseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.xml = _write_sample(self.tmpdir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_extracts_valid_games(self):
        result = parse_launchbox_xml(self.xml)
        self.assertEqual(len(result["games"]), 2)
        names = {g["name"] for g in result["games"]}
        self.assertEqual(names, {"Quake", "Doom"})

    def test_parse_skips_malformed(self):
        result = parse_launchbox_xml(self.xml)
        self.assertEqual(result["skipped"], 2)

    def test_parse_maps_fields(self):
        result = parse_launchbox_xml(self.xml)
        quake = next(g for g in result["games"] if g["name"] == "Quake")
        self.assertEqual(quake["launchbox_db_id"], "1001")
        self.assertEqual(quake["platform"], "PC")
        self.assertEqual(quake["developer"], "id Software")
        self.assertEqual(quake["rating"], 4.5)
        self.assertEqual(quake["emulator_id"], "emu-1")
        self.assertEqual(quake["description"], "The original Quake.")

    def test_parse_collects_emulator_ids(self):
        result = parse_launchbox_xml(self.xml)
        self.assertEqual(result["emulator_ids"], ["emu-1", "emu-2"])


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.xml = _write_sample(self.tmpdir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_preview_reports_new_and_duplicates(self):
        existing = [{"name": "Quake", "launchbox_db_id": "1001"}]
        report = preview_import(self.xml, existing)
        self.assertEqual(report["total_in_xml"], 2)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["would_import"], 1)
        self.assertEqual(report["emulator_ids"], ["emu-1", "emu-2"])

    def test_preview_all_new_when_empty_library(self):
        report = preview_import(self.xml, [])
        self.assertEqual(report["would_import"], 2)
        self.assertEqual(report["duplicates"], 0)

    def test_preview_dedup_by_name(self):
        existing = [{"name": "Doom"}]
        report = preview_import(self.xml, existing)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["would_import"], 1)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.xml = _write_sample(self.tmpdir)
        # Set up a minimal state store for merge_imported_games
        self._old_data = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = str(self.tmpdir / "data")
        (self.tmpdir / "data").mkdir(exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()
        if self._old_data is not None:
            os.environ["OPENBOX_DATA_DIR"] = self._old_data
        else:
            os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_apply_returns_emulator_report(self):
        from pkg.state.imports import merge_imported_games

        result = apply_import(self.xml, [], merge_imported_games)
        self.assertIn("emulator_ids", result)
        self.assertEqual(result["emulator_ids"], ["emu-1", "emu-2"])
        self.assertIn("skipped_malformed", result)
        self.assertEqual(result["skipped_malformed"], 2)


class RouteRegistrationTest(unittest.TestCase):
    def test_routes_in_post_table(self):
        from routes import POST_TABLE

        self.assertIn("/api/v2/import/launchbox/preview", POST_TABLE)
        self.assertIn("/api/v2/import/launchbox/apply", POST_TABLE)

    def test_resolve_route_registered(self):
        from routes import POST_TABLE

        self.assertIn("/api/v2/import/launchbox/resolve", POST_TABLE)

    def test_resolve_handler_method_exists(self):
        from handlers.imports import ImportsHandlers

        self.assertTrue(hasattr(ImportsHandlers, "_api_post_api_v2_import_launchbox_resolve"))
        from routes.registry import _REGISTRY

        self.assertIn(("POST", "/api/v2/import/launchbox/resolve"), _REGISTRY)


class PathRemapTest(unittest.TestCase):
    def test_remap_converts_windows_prefix(self):
        from pkg.parity.parity_launchbox_import import remap_windows_path

        out = remap_windows_path(
            "C:\\Games\\Quake\\quake.exe",
            {"from_prefix": "C:\\Games", "to_dir": "/mnt/games"},
        )
        self.assertEqual(out, "/mnt/games/Quake/quake.exe")

    def test_remap_case_insensitive(self):
        from pkg.parity.parity_launchbox_import import remap_windows_path

        out = remap_windows_path(
            "c:\\games\\Doom\\doom.exe",
            {"from_prefix": "C:\\Games", "to_dir": "/mnt/games"},
        )
        self.assertEqual(out, "/mnt/games/Doom/doom.exe")

    def test_remap_none_passthrough(self):
        from pkg.parity.parity_launchbox_import import remap_windows_path

        self.assertEqual(remap_windows_path("/mnt/games/x.exe", None), "/mnt/games/x.exe")
        self.assertEqual(remap_windows_path("C:\\Games\\x.exe", None), "C:\\Games\\x.exe")

    def test_is_windows_path(self):
        from pkg.parity.parity_launchbox_import import is_windows_path

        self.assertTrue(is_windows_path("C:\\Games\\x.exe"))
        self.assertTrue(is_windows_path("D:/Games/x.exe"))
        self.assertFalse(is_windows_path("/mnt/games/x.exe"))
        self.assertFalse(is_windows_path(""))


class EmulatorMappingTest(unittest.TestCase):
    def test_known_adapter_resolves_against_registry(self):
        from pkg.parity.parity_launchbox_import import validate_emulator_mappings

        result = validate_emulator_mappings({"emu-1": "mame-arcade"})
        self.assertIn("emu-1", result["resolved"])
        self.assertEqual(result["resolved"]["emu-1"], "mame-arcade")
        self.assertEqual(result["unresolved"], [])

    def test_unknown_emulator_reported_not_silently_applied(self):
        from pkg.parity.parity_launchbox_import import validate_emulator_mappings

        result = validate_emulator_mappings({"emu-9": "nope-adapter-xyz"})
        self.assertEqual(result["resolved"], {})
        self.assertIn("emu-9", result["unresolved"])

    def test_resolve_recounts_without_mutating(self):
        import copy

        from pkg.parity.parity_launchbox_import import resolve_games

        games = [
            {"name": "Quake", "launchbox_db_id": "1001", "path": "C:\\Games\\Quake\\quake.exe", "emulator_id": "emu-1"},
            {"name": "Doom", "launchbox_db_id": "1002", "path": "C:\\Games\\Doom\\doom.exe", "emulator_id": "emu-9"},
        ]
        snapshot = copy.deepcopy(games)
        first = resolve_games(
            games,
            mappings={"emu-1": "mame-arcade"},
            path_remap={"from_prefix": "C:\\Games", "to_dir": "/mnt/games"},
        )
        self.assertEqual(games, snapshot)
        second = resolve_games(
            games,
            mappings={"emu-1": "mame-arcade"},
            path_remap={"from_prefix": "C:\\Games", "to_dir": "/mnt/games"},
        )
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(first["counts"]["total"], 2)
        self.assertEqual(first["counts"]["resolved"], 1)
        self.assertEqual(first["counts"]["remapped"], 2)


class ShelfRowTest(unittest.TestCase):
    def test_unmapped_windows_path_becomes_needs_path(self):
        from pkg.parity.parity_launchbox_import import resolve_games

        games = [
            {"name": "Doom", "launchbox_db_id": "1002", "path": "C:\\Games\\Doom\\doom.exe", "emulator_id": "emu-9"},
        ]
        result = resolve_games(games, mappings={}, path_remap=None)
        row = result["rows"][0]
        self.assertTrue(row.get("needs_path"))
        self.assertEqual(row.get("path"), "")
        self.assertEqual(result["counts"]["needs_path"], 1)

    def test_no_shell_synthesis_tokenized_only(self):
        from pkg.parity.launch_tokens import find_invalid_tokens
        from pkg.parity.parity_launchbox_import import resolve_games

        games = [
            {"name": "Doom", "launchbox_db_id": "1002", "path": "C:\\Games\\Doom\\doom.exe", "emulator_id": "emu-9"},
        ]
        result = resolve_games(games, mappings={}, path_remap=None)
        row = result["rows"][0]
        launch = str(row.get("launch") or "")
        self.assertNotIn("\\", launch)
        self.assertNotIn("C:", launch)
        self.assertEqual(find_invalid_tokens(launch), [])
        # original windows path must not leak into a shell command
        self.assertNotIn("C:\\", launch)

    def test_remapped_posix_clears_needs_path(self):
        from pkg.parity.parity_launchbox_import import resolve_games

        games = [
            {"name": "Quake", "launchbox_db_id": "1001", "path": "C:\\Games\\Quake\\quake.exe"},
        ]
        result = resolve_games(
            games,
            mappings={},
            path_remap={"from_prefix": "C:\\Games", "to_dir": "/mnt/games"},
        )
        row = result["rows"][0]
        self.assertFalse(row.get("needs_path"))
        self.assertEqual(row.get("path"), "/mnt/games/Quake/quake.exe")


class StalePreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.xml = _write_sample(self.tmpdir)
        self.data_dir = self.tmpdir / "data"
        self.data_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_ok_then_stale_library_rejected(self):
        from api_errors import PreviewStale
        from pkg.parity.parity_launchbox_import import (
            create_launchbox_preview,
            resolve_launchbox_preview,
        )

        preview = create_launchbox_preview(self.xml, [], data_dir=self.data_dir)
        preview_id = preview["preview_id"]
        ok_result = resolve_launchbox_preview(preview_id, {}, None, [], data_dir=self.data_dir)
        self.assertIn("counts", ok_result)
        with self.assertRaises(PreviewStale):
            resolve_launchbox_preview(
                preview_id, {}, None,
                [{"name": "Quake", "launchbox_db_id": "1001"}],
                data_dir=self.data_dir,
            )

    def test_stale_xml_rejected(self):
        import time

        from api_errors import PreviewStale
        from pkg.parity.parity_launchbox_import import (
            create_launchbox_preview,
            resolve_launchbox_preview,
        )

        preview = create_launchbox_preview(self.xml, [], data_dir=self.data_dir)
        preview_id = preview["preview_id"]
        # mutate the XML file so its fingerprint changes
        time.sleep(0.01)
        with open(self.xml, "a", encoding="utf-8") as handle:
            handle.write("\n<!-- touch -->\n")
        with self.assertRaises(PreviewStale):
            resolve_launchbox_preview(preview_id, {}, None, [], data_dir=self.data_dir)

    def test_unknown_preview_not_found(self):
        from api_errors import PreviewNotFound
        from pkg.parity.parity_launchbox_import import resolve_launchbox_preview

        with self.assertRaises(PreviewNotFound):
            resolve_launchbox_preview("missing-id", {}, None, [], data_dir=self.data_dir)


class StreamingScaleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_big_xml(self, count):
        path = self.tmpdir / "big.xml"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0" encoding="utf-8"?>\n<LaunchBox>\n')
            for index in range(count):
                handle.write(
                    f"  <Game><ID>{50000 + index}</ID><Title>Game {index}</Title>"
                    f"<ApplicationPath>C:\\Games\\Game{index}\\game.exe</ApplicationPath>"
                    f"<Platform>PC</Platform></Game>\n"
                )
            handle.write("</LaunchBox>\n")
        return path

    def test_20k_streams_with_5k_paginated_preview(self):
        from pkg.parity.parity_launchbox_import import PREVIEW_PAGE_SIZE, preview_import

        self.assertEqual(PREVIEW_PAGE_SIZE, 5000)
        path = self._write_big_xml(20000)
        report = preview_import(path, [])
        self.assertEqual(report["total_in_xml"], 20000)
        self.assertEqual(report["would_import"], 20000)
        self.assertLessEqual(len(report["preview_games"]), 5000)
        self.assertEqual(len(report["preview_games"]), 5000)

    def test_preview_uses_iterparse_not_dom_parse(self):
        import xml.etree.ElementTree as element_tree

        from pkg.parity.parity_launchbox_import import preview_import

        path = self._write_big_xml(200)
        original_parse = element_tree.parse

        def _boom(*args, **kwargs):
            raise AssertionError("ET.parse must not be used for streaming preview")

        element_tree.parse = _boom
        try:
            report = preview_import(path, [])
        finally:
            element_tree.parse = original_parse
        self.assertEqual(report["total_in_xml"], 200)
        self.assertEqual(report["would_import"], 200)

    def test_iterparse_generator_streams_without_materializing(self):
        from pkg.parity.parity_launchbox_import import iter_parsed_games

        path = self._write_big_xml(2000)
        count = 0
        for entry in iter_parsed_games(path):
            count += 1
            self.assertIn("name", entry)
            if count >= 5:
                break
        self.assertEqual(count, 5)
        # full pass still counts everything
        full = sum(1 for _ in iter_parsed_games(path))
        self.assertEqual(full, 2000)

    def test_preview_pagination_limit_offset(self):
        from pkg.parity.parity_launchbox_import import preview_import

        path = self._write_big_xml(10)
        first = preview_import(path, [], limit=3, offset=0)
        second = preview_import(path, [], limit=3, offset=3)
        self.assertEqual(len(first["preview_games"]), 3)
        self.assertEqual(len(second["preview_games"]), 3)
        first_names = [g["name"] for g in first["preview_games"]]
        second_names = [g["name"] for g in second["preview_games"]]
        self.assertFalse(set(first_names) & set(second_names))


class ValidationEdgeTest(unittest.TestCase):
    def test_mappings_must_be_object(self):
        from api_errors import BadRequest
        from pkg.parity.parity_launchbox_import import validate_emulator_mappings

        with self.assertRaises(BadRequest):
            validate_emulator_mappings(["not-a-dict"])

    def test_remap_missing_keys_passthrough(self):
        from pkg.parity.parity_launchbox_import import remap_windows_path

        self.assertEqual(remap_windows_path("C:\\Games\\x.exe", {}), "C:\\Games\\x.exe")
        self.assertEqual(
            remap_windows_path("C:\\Games\\x.exe", {"from_prefix": "", "to_dir": ""}),
            "C:\\Games\\x.exe",
        )

    def test_remap_non_matching_prefix_passthrough(self):
        from pkg.parity.parity_launchbox_import import remap_windows_path

        self.assertEqual(
            remap_windows_path("C:\\Games\\x.exe", {"from_prefix": "D:\\Other", "to_dir": "/mnt/other"}),
            "C:\\Games\\x.exe",
        )

    def test_validate_with_explicit_adapters(self):
        from pkg.parity.parity_launchbox_import import validate_emulator_mappings

        adapters = [
            {"adapter_id": "test-adapter", "emulator_id": "test.emu", "label": "Test"},
        ]
        result = validate_emulator_mappings({"lb-1": "test-adapter"}, adapters=adapters)
        self.assertEqual(result["resolved"], {"lb-1": "test-adapter"})
        missing = validate_emulator_mappings({"lb-2": "unknown"}, adapters=adapters)
        self.assertIn("lb-2", missing["unresolved"])

    def test_validate_matches_by_emulator_id(self):
        from pkg.parity.parity_launchbox_import import validate_emulator_mappings

        adapters = [
            {"adapter_id": "test-adapter", "emulator_id": "test.emu", "label": "Test"},
        ]
        result = validate_emulator_mappings({"lb-3": "test.emu"}, adapters=adapters)
        self.assertEqual(result["resolved"], {"lb-3": "test-adapter"})

    def test_resolve_rejects_bad_shapes(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        from api_errors import BadRequest
        from pkg.parity.parity_launchbox_import import (
            create_launchbox_preview,
            resolve_launchbox_preview,
        )

        with _tempfile.TemporaryDirectory() as tmp:
            tmpdir = _Path(tmp)
            xml = tmpdir / "p.xml"
            xml.write_text(
                '<?xml version="1.0"?><LaunchBox><Game><ID>1</ID><Title>A</Title></Game></LaunchBox>',
                encoding="utf-8",
            )
            data_dir = tmpdir / "data"
            data_dir.mkdir()
            preview = create_launchbox_preview(xml, [], data_dir=data_dir)
            with self.assertRaises(BadRequest):
                resolve_launchbox_preview(preview["preview_id"], ["bad"], None, [], data_dir=data_dir)
            with self.assertRaises(BadRequest):
                resolve_launchbox_preview(
                    preview["preview_id"], {}, "bad-remap", [], data_dir=data_dir
                )


class HandlerResolveTest(unittest.TestCase):
    def _fake_self(self, store):
        class _Fake:
            def send_json(self, status, payload):
                store["status"] = status
                store["payload"] = payload

        return _Fake()

    def test_resolve_requires_preview_id(self):
        from api_errors import BadRequest
        from handlers.imports import ImportsHandlers

        store = {}
        fake = self._fake_self(store)
        with self.assertRaises(BadRequest):
            ImportsHandlers._api_post_api_v2_import_launchbox_resolve(fake, {})

    def test_resolve_rejects_bad_mappings_shape(self):
        from api_errors import BadRequest
        from handlers.imports import ImportsHandlers

        store = {}
        fake = self._fake_self(store)
        with self.assertRaises(BadRequest):
            ImportsHandlers._api_post_api_v2_import_launchbox_resolve(
                fake, {"preview_id": "abc", "mappings": ["bad"]}
            )

    def test_resolve_rejects_bad_path_remap_shape(self):
        from api_errors import BadRequest
        from handlers.imports import ImportsHandlers

        fake = self._fake_self({})
        with self.assertRaises(BadRequest):
            ImportsHandlers._api_post_api_v2_import_launchbox_resolve(
                fake, {"preview_id": "abc", "path_remap": "bad"}
            )

    def test_resolve_success_delegates_without_mutating(self):
        import handlers.imports as imports_mod
        from handlers.imports import ImportsHandlers

        store = {}
        fake = self._fake_self(store)
        original_load = imports_mod.load_state_view
        import pkg.parity.parity_launchbox_import as lb_mod

        original_resolve = lb_mod.resolve_launchbox_preview
        calls = {}

        def _fake_load():
            return {"games": []}

        def _fake_resolve(preview_id, mappings, path_remap, games):
            calls["preview_id"] = preview_id
            calls["mappings"] = dict(mappings or {})
            calls["games"] = list(games or [])
            return {"preview_id": preview_id, "counts": {"total": 0}, "rows": []}

        imports_mod.load_state_view = _fake_load
        lb_mod.resolve_launchbox_preview = _fake_resolve
        try:
            ImportsHandlers._api_post_api_v2_import_launchbox_resolve(
                fake,
                {"preview_id": "pid-1", "mappings": {"emu-1": "mame-arcade"}},
            )
        finally:
            imports_mod.load_state_view = original_load
            lb_mod.resolve_launchbox_preview = original_resolve
        self.assertEqual(store["status"], 200)
        self.assertEqual(calls["preview_id"], "pid-1")
        self.assertEqual(calls["mappings"], {"emu-1": "mame-arcade"})

    def test_preview_handler_persists_preview_id(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path

        import handlers.imports as imports_mod
        from handlers.imports import ImportsHandlers

        with _tempfile.TemporaryDirectory() as tmp:
            tmpdir = _Path(tmp)
            xml = tmpdir / "platform.xml"
            xml.write_text(_SAMPLE_XML, encoding="utf-8")
            store = {}

            class _Fake:
                def send_json(self, status, payload):
                    store["status"] = status
                    store["payload"] = payload

            original_load = imports_mod.load_state_view
            imports_mod.load_state_view = lambda: {"games": []}
            # point preview storage at the temp dir by patching the dir helper
            import pkg.parity.parity_launchbox_import as lb_mod

            original_dir = lb_mod.launchbox_previews_dir
            lb_mod.launchbox_previews_dir = lambda data_dir=None: tmpdir / "lb_prev"
            try:
                ImportsHandlers._api_post_api_v2_import_launchbox_preview(
                    _Fake(), {"xml_path": str(xml)}
                )
            finally:
                imports_mod.load_state_view = original_load
                lb_mod.launchbox_previews_dir = original_dir
            self.assertEqual(store["status"], 200)
            self.assertIn("preview_id", store["payload"])
            self.assertIn("would_import", store["payload"])

    def test_preview_handler_requires_xml_path(self):
        from api_errors import BadRequest
        from handlers.imports import ImportsHandlers

        class _Fake:
            def send_json(self, status, payload):
                raise AssertionError("must not send on error")

        with self.assertRaises(BadRequest):
            ImportsHandlers._api_post_api_v2_import_launchbox_preview(_Fake(), {})
        with self.assertRaises(BadRequest):
            ImportsHandlers._api_post_api_v2_import_launchbox_preview(
                _Fake(), {"xml_path": "/nonexistent/platform.xml"}
            )


if __name__ == "__main__":
    unittest.main()
