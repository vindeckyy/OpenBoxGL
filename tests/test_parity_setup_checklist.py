#!/usr/bin/env python3
"""F6: per-platform setup checklist projection tests."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_setup_checklist import build_checklists, checklist_for_platform  # noqa: E402


def _sha1(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


class SetupChecklistTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def _registry(self, **adapter_overrides):
        adapter = {
            "adapter_id": "test-adapter",
            "emulator_id": "test-emulator",
            "label": "Test Emulator",
            "platform": "TestStation",
            "bios_path": "",
            "bios_sha1": "",
        }
        adapter.update(adapter_overrides)
        return {"adapters": [adapter], "errors": []}

    def test_all_green_when_bios_emulator_launch_and_artwork_are_ready(self):
        rom = self.root / "game.rom"
        rom.write_bytes(b"rom")
        bios = self.root / "bios.bin"
        bios.write_bytes(b"bios")
        state = {
            "games": [{
                "game_id": "g1",
                "name": "Game",
                "platform": "TestStation",
                "path": str(rom),
                "emulator_adapter_id": "test-adapter",
                "cover": "/media/cover.png",
            }]
        }
        registry = self._registry(bios_path=str(bios), bios_sha1=_sha1(bios))
        payload = build_checklists(state, registry=registry)
        card = payload["platforms"][0]
        self.assertEqual(card["status"], "green")
        self.assertEqual(payload["summary"], {"green": 1, "yellow": 0, "red": 0})
        self.assertEqual(card["checks"]["bios"]["state"], "ok")
        self.assertFalse(card["checks"]["bios"]["sha1_drift"])
        self.assertEqual(card["checks"]["artwork"]["state"], "ok")
        self.assertEqual(card["ready"], 4)

    def test_bios_sha1_drift_is_flagged_separately_from_missing(self):
        bios = self.root / "bios.bin"
        bios.write_bytes(b"corrupted")
        state = {"games": [{"game_id": "g1", "platform": "TestStation", "path": str(self.root / "missing.rom"), "cover": "x"}]}
        registry = self._registry(bios_path=str(bios), bios_sha1="0" * 40)
        card = checklist_for_platform(state, "TestStation", registry=registry)
        self.assertEqual(card["checks"]["bios"]["state"], "fail")
        self.assertTrue(card["checks"]["bios"]["sha1_drift"])
        self.assertIn("drift", card["checks"]["bios"]["detail"].casefold())
        self.assertEqual(card["status"], "red")

        missing = checklist_for_platform(state, "TestStation", registry=self._registry(bios_path=str(self.root / "nope.bin")))
        self.assertEqual(missing["checks"]["bios"]["state"], "fail")
        self.assertIsNone(missing["checks"]["bios"].get("sha1_drift"))

    def test_custom_launch_counts_as_resolved_emulator_and_launchable(self):
        rom = self.root / "arcade.bin"
        rom.write_bytes(b"x")
        state = {"games": [{"game_id": "g1", "platform": "Arcade", "path": str(rom), "launch": "mame {path}", "cover": "x"}]}
        card = checklist_for_platform(state, "Arcade", registry={"adapters": [], "errors": []})
        self.assertEqual(card["checks"]["emulator"]["state"], "ok")
        self.assertEqual(card["checks"]["launch"]["state"], "ok")
        self.assertEqual(card["status"], "green")

    def test_bios_hint_fallback_reports_unknown_hash_as_warning(self):
        hint_dir = self.root / "system"
        hint_dir.mkdir()
        (hint_dir / "bios.bin").write_bytes(b"bios")
        rom = self.root / "ps2.rom"
        rom.write_bytes(b"rom")
        state = {"games": [{"game_id": "g1", "platform": "PlayStation 2", "path": str(rom), "launch": "pcsx2 {path}", "cover": "x"}]}
        hints = {"PCSX2": [("PS2 BIOS folder", hint_dir)]}
        card = checklist_for_platform(
            state, "PlayStation 2",
            registry={"adapters": [], "errors": []}, hint_loader=lambda: hints,
        )
        self.assertEqual(card["checks"]["bios"]["state"], "warn")
        self.assertEqual(card["status"], "yellow")
        empty = self.root / "empty"
        empty.mkdir()
        card = checklist_for_platform(
            state, "PlayStation 2",
            registry={"adapters": [], "errors": []},
            hint_loader=lambda: {"PCSX2": [("PS2 BIOS folder", empty)]},
        )
        self.assertEqual(card["checks"]["bios"]["state"], "fail")

    def test_platforms_without_bios_requirement_skip_the_check(self):
        state = {"games": [{"game_id": "g1", "platform": "NES", "path": "missing.nes", "cover": "x"}]}
        card = checklist_for_platform(state, "NES", registry={"adapters": [], "errors": []})
        self.assertEqual(card["checks"]["bios"]["state"], "skip")
        # Artwork-only readiness still leaves launch red.
        self.assertEqual(card["status"], "red")

    def test_empty_library_returns_empty_cards(self):
        payload = build_checklists({"games": []}, registry={"adapters": [], "errors": []})
        self.assertEqual(payload["platforms"], [])
        self.assertEqual(payload["summary"], {"green": 0, "yellow": 0, "red": 0})
        self.assertIn("generated_at", payload)


if __name__ == "__main__":
    unittest.main()
