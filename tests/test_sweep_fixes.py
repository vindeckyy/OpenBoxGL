#!/usr/bin/env python3
"""Pins for the 2026-11 bug sweep. Each case is a trigger the sweep reproduced."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend_io import is_public_ip  # noqa: E402
from pkg.parity.parity_duplicates import normalize_title  # noqa: E402
from pkg.parity.parity_emulator_defs import _command_includes_target, build_adapter_argv  # noqa: E402
from pkg.parity.parity_emulator_settings import _clean_plugin_env  # noqa: E402
from pkg.parity.parity_identity import normalize_rom_name  # noqa: E402
from pkg.parity.parity_import import group_multi_disc  # noqa: E402
from pkg.parity.parity_launchbox_import import _map_path  # noqa: E402
from pkg.parity.parity_picker import _history_value  # noqa: E402
from pkg.parity.parity_redact import REDACTED_KEYS  # noqa: E402
from pkg.parity.parity_screenscraper import clean_media_url  # noqa: E402
from plugins import RESERVED_PLUGIN_IDS, read_manifest  # noqa: E402


class SweepFixTests(unittest.TestCase):
    def test_title_markers_do_not_eat_real_words(self):
        self.assertNotEqual(normalize_title("Assassin's Creed"), normalize_title("Assassin's Creed Revelations"))
        self.assertNotEqual(normalize_title("Super Mario"), normalize_title("Super Mario World"))
        self.assertEqual(normalize_title("Chrono Trigger (USA)"), normalize_title("chrono trigger"))

    def test_disc_grouping_keeps_hillside_and_groups_disc_of(self):
        hillside = group_multi_disc([Path("Hillside 1.iso"), Path("Hillside 2.iso")])
        self.assertEqual(sorted(len(group) for group in hillside), [1, 1])
        grouped = group_multi_disc([
            Path("Final Fantasy VII (Disc 1 of 3).cue"),
            Path("Final Fantasy VII (Disc 2 of 3).cue"),
            Path("Final Fantasy VII (Disc 3 of 3).cue"),
        ])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped[0]), 3)

    def test_rom_identity_keeps_disc_numbers(self):
        first = normalize_rom_name("Final Fantasy VII (Disc 1).cue")
        second = normalize_rom_name("Final Fantasy VII (Disc 2).cue")
        self.assertNotEqual(first, second)
        self.assertIn("disc 1", first)

    def test_private_and_nat64_addresses_are_rejected(self):
        self.assertFalse(is_public_ip("127.0.0.1"))
        self.assertFalse(is_public_ip("10.0.0.5"))
        self.assertFalse(is_public_ip("169.254.169.254"))
        self.assertFalse(is_public_ip("224.0.0.1"))
        self.assertFalse(is_public_ip("64:ff9b::a9fe:a9fe"))
        self.assertTrue(is_public_ip("1.1.1.1"))
        self.assertEqual(clean_media_url("https://127.0.0.1/cover.png"), "")
        self.assertEqual(clean_media_url("https://169.254.169.254/latest/meta-data/"), "")
        self.assertTrue(clean_media_url("https://media.example/cover.png").startswith("https://"))

    def test_launchbox_dot_source_does_not_eat_the_filename(self):
        mapped = _map_path(r"D:\Roms\Sonic.bin", {"path_mappings": {r"D:\Roms\.": "/library/roms"}})
        self.assertEqual(mapped, "/library/roms/Sonic.bin")

    def test_reserved_plugin_id_is_refused(self):
        self.assertIn("credentials", RESERVED_PLUGIN_IDS)
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "plugin.json").write_text(
                '{"id":"credentials","name":"Nope","version":"1","api_version":2,"entry":"plugin.py","hooks":[]}',
                encoding="utf-8",
            )
            (package / "plugin.py").write_text("print('no')\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_manifest(package)

    def test_loader_environment_variables_are_refused(self):
        with self.assertRaises(ValueError):
            _clean_plugin_env({"LD_AUDIT": "/tmp/pwn.so"})
        with self.assertRaises(ValueError):
            _clean_plugin_env({"DYLD_INSERT_LIBRARIES": "/tmp/pwn.dylib"})

    def test_storefront_tokens_count_as_the_launch_target(self):
        self.assertTrue(_command_includes_target("/usr/bin/open 'steam://rungameid/{app_id}'"))
        self.assertTrue(_command_includes_target("retroarch -L core.so {Path}"))
        self.assertFalse(_command_includes_target("retroarch -L core.so"))

    def test_picker_history_uses_the_stable_id(self):
        sessions = {"abc": [600.0]}
        self.assertEqual(_history_value(sessions, {"game_id": "abc"}), [600.0])
        self.assertIsNone(_history_value(sessions, {"id": 0}))

    def test_kiosk_pin_is_redacted(self):
        self.assertIn("museum_kiosk_pin_hash", REDACTED_KEYS)

    def test_mame_zip_becomes_a_driver_name(self):
        with tempfile.TemporaryDirectory() as directory:
            rom = Path(directory) / "pacman.zip"
            rom.write_bytes(b"PK")
            adapter = {
                "adapter_id": "mame-arcade",
                "emulator_id": "org.mamedev.MAME",
                "startup_args": ["{path}"],
            }
            args = build_adapter_argv(adapter, {"name": "Pac-Man"}, rom, prefix=["mame"])
        self.assertEqual(args[:1], ["mame"])
        self.assertIn("-rompath", args)
        self.assertIn("pacman", args)
        self.assertNotIn(str(rom), args)


if __name__ == "__main__":
    unittest.main()
