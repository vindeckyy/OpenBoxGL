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


if __name__ == "__main__":
    unittest.main()
