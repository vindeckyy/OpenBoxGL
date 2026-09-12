"""ES-DE gamelist parser and stale-safe import-plan contracts."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_esde_import import (  # noqa: E402
    ESDEImportError,
    StaleESDEPlan,
    apply_import_plan,
    build_import_plan,
    parse_gamelist,
)


XML = """<?xml version="1.0"?><gameList>
<game><path>./roms/quake.zip</path><name>Quake</name><system>pc</system>
<image>./media/quake.png</image><marquee>./media/quake-marquee.png</marquee>
<releasedate>19960522T000000</releasedate><rating>0.8</rating><favorite>true</favorite><id>q1</id></game>
<game><path>./roms/doom.zip</path><name>Doom</name><genre>FPS</genre></game>
</gameList>"""


class ESDEImportTests(unittest.TestCase):
    def test_parse_normalizes_metadata_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "gamelist.xml"
            source.write_text(XML)
            parsed = parse_gamelist(source)
        self.assertEqual(len(parsed["games"]), 2)
        first = parsed["games"][0]
        self.assertEqual(first["name"], "Quake")
        self.assertEqual(first["year"], "1996")
        self.assertEqual(first["rating"], 4.0)
        self.assertTrue(first["favorite"])
        self.assertTrue(first["path"].endswith("/roms/quake.zip"))
        self.assertTrue(first["cover"].endswith("/media/quake.png"))
        self.assertEqual(first["source_identity"], "esde:q1")

    def test_parse_rejects_bad_root_and_giant_input(self):
        with self.assertRaises(ESDEImportError):
            parse_gamelist("<launchbox/>")
        with self.assertRaises(ESDEImportError):
            parse_gamelist(XML, max_xml_bytes=8)

    def test_plan_adds_and_merges_by_source_identity(self):
        existing = {"games": [{"game_id": "openbox-q", "name": "Old Quake", "source_identity": "esde:q1", "path": "/old/quake.zip"}], "settings": {}}
        plan = build_import_plan({"games": parse_gamelist(XML)["games"], "source_digest": "source"}, existing)
        self.assertEqual(plan["counts"]["added"], 1)
        self.assertEqual(plan["counts"]["merged"], 1)
        self.assertIn("name", plan["operations"][0]["review_fields"])

    def test_apply_requires_matching_plan_and_preserves_input(self):
        source = parse_gamelist(XML)
        state = {"games": [], "settings": {}}
        plan = build_import_plan(source, state)
        before = copy.deepcopy(state)
        result = apply_import_plan(plan, state, preview_token=plan["preview_token"])
        self.assertEqual(len(result["games"]), 2)
        self.assertEqual(state, before)
        with self.assertRaises(StaleESDEPlan):
            apply_import_plan(plan, {"games": [{"game_id": "changed"}], "settings": {}})

    def test_apply_recomputes_against_source(self):
        state = {"games": [], "settings": {}}
        source = parse_gamelist(XML)
        plan = build_import_plan(source, state)
        changed = dict(source)
        changed["games"] = source["games"][:1]
        with self.assertRaises(StaleESDEPlan):
            apply_import_plan(plan, state, source=changed)


if __name__ == "__main__":
    unittest.main()
