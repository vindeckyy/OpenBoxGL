#!/usr/bin/env python3
"""Tests for the PICO-8, MSU-1, WHDLoad, and RPCS3 external-folder importers."""

import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_import import import_rpcs3_hdd
from parity_legacy_import import import_msu1, import_pico8, import_whdload

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def linux_flatpak_registry():
    """Flatpak is a Linux install path (ADR 0086): pin Linux and rebuild the emulator registry."""
    import pkg.parity.parity_emulator_defs as defs

    patcher = mock.patch.object(defs, "IS_LINUX", True)
    patcher.start()
    defs._reset_registry_cache()
    try:
        yield
    finally:
        patcher.stop()
        defs._reset_registry_cache()


class LegacyImporterTests(unittest.TestCase):
    def test_pico8_imports_carts_recursively_and_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            carts = root / "carts"
            nested = carts / "nested"
            nested.mkdir(parents=True)
            (carts / "celeste.p8").write_bytes(b"pico-8 cartridge")
            (nested / "Hack.p8.png").write_bytes(b"png cart")
            (nested / "notes.txt").write_text("not a cart", encoding="utf-8")
            (nested / "screenshot.png").write_bytes(b"png")
            os.symlink(carts / "celeste.p8", carts / "celeste-link.p8")
            os.symlink(carts / "missing-cart.p8", carts / "dangling.p8")

            errors = []
            games = import_pico8(root, errors=errors)

            by_name = {game["name"]: game for game in games}
            self.assertEqual(set(by_name), {"celeste", "Hack"})
            self.assertNotIn("screenshot", by_name)
            for game in games:
                self.assertEqual(game["platform"], "PICO-8")
                self.assertEqual(game["source"], "PICO-8")
                self.assertTrue(Path(game["path"]).is_file())
            self.assertTrue(by_name["Hack"]["path"].endswith("Hack.p8.png"))
            self.assertEqual(len(games), 2, "symlinked cart must import once")
            self.assertEqual(len(errors), 1)
            self.assertIn("dangling.p8", errors[0])

    def test_pico8_rejects_missing_folder(self):
        with self.assertRaises(FileNotFoundError):
            import_pico8(Path(tempfile.gettempdir()) / "openbox-no-such-folder")

    def test_msu1_pairs_sibling_rom_by_case_insensitive_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_dir = root / "Game.msu"
            game_dir.mkdir()
            (game_dir / "Game-1.pcm").write_bytes(b"audio")
            (root / "game.SFC").write_bytes(b"rom")
            album = root / "album"
            album.mkdir()
            (album / "Other.msu").mkdir()
            (album / "OTHER.smc").write_bytes(b"rom")
            (root / "Orphan.msu").mkdir()

            errors = []
            games = import_msu1(root, errors=errors)

            by_name = {game["name"]: game for game in games}
            self.assertEqual(set(by_name), {"game", "OTHER"})
            self.assertEqual(by_name["game"]["platform"], "SNES")
            self.assertTrue(by_name["game"]["msu1"])
            self.assertEqual(Path(by_name["game"]["msu_path"]), game_dir)
            self.assertTrue(by_name["OTHER"]["path"].endswith("OTHER.smc"))
            self.assertEqual(len(errors), 1)
            self.assertIn("Orphan.msu", errors[0])

    def test_whdload_skips_system_drawers_and_dedupes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for drawer in ("C", "Libs", "Devs"):
                (root / drawer).mkdir()
            (root / "C" / "Assign.slave").write_bytes(b"system")
            (root / "Libs" / "tracker.hdf").write_bytes(b"system")
            (root / "Devs" / "keyboard.lha").write_bytes(b"system")
            (root / "Turrican.lha").write_bytes(b"game")
            nested = root / "demos"
            nested.mkdir()
            (nested / "Chaos.hdf").write_bytes(b"game")
            (root / "ignored.zip").write_bytes(b"not a whdload game")
            os.symlink(root / "Turrican.lha", root / "Dupe.lha")

            errors = []
            games = import_whdload(root, errors=errors)

            names = {game["name"] for game in games}
            self.assertEqual(names, {"Turrican", "Chaos"})
            self.assertEqual(errors, [])
            for game in games:
                self.assertEqual(game["platform"], "Amiga")
                self.assertEqual(game["source"], "WHDLoad")
            self.assertEqual(len(games), 2, "symlinked install must import once")

    def test_rpcs3_reads_external_games_and_isos(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            external_root = Path(directory) / "external"
            hdd = home / ".config/rpcs3/dev_hdd0/game/NPUB30001"
            hdd.mkdir(parents=True)
            (hdd / "PARAM.SFO").write_bytes(b"\x00PSF" + b"\x00" * 30)
            external = external_root / "My External Game"
            external.mkdir(parents=True)
            (external / "PARAM.SFO").write_bytes(b"\x00PSF" + b"\x00" * 30)
            external_iso = external_root / "External Disc.iso"
            external_iso.write_bytes(b"iso")
            config_iso = external_root / "Config Disc.iso"
            config_iso.write_bytes(b"iso")

            config_dir = home / ".config/rpcs3"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "games.yml").write_text(
                f"{external}:\n"
                "  title: My External Game\n"
                f"{external_iso}:\n"
                "  title: External Disc\n",
                encoding="utf-8",
            )
            (config_dir / "config.yml").write_text(
                "games:\n"
                f"  - {config_iso}\n"
                f"  - {external}\n"
                f"  - {external_root / 'missing.iso'}\n",
                encoding="utf-8",
            )
            flatpak_dir = home / ".var/app/net.rpcs3.RPCS3/config/rpcs3"
            flatpak_dir.mkdir(parents=True)
            flatpak_game = external_root / "Flatpak Game"
            flatpak_game.mkdir()
            (flatpak_game / "PARAM.SFO").write_bytes(b"\x00PSF" + b"\x00" * 30)
            (flatpak_dir / "games.yml").write_text(
                f"{external_iso}:\n  title: Duplicate\n"
                f"{flatpak_game}:\n  title: Flatpak Game\n",
                encoding="utf-8",
            )

            errors = []
            games = import_rpcs3_hdd(home, errors=errors)

            paths = {Path(game["path"]).name for game in games}
            self.assertEqual(
                paths,
                {
                    "NPUB30001",
                    "My External Game",
                    "External Disc.iso",
                    "Config Disc.iso",
                    "Flatpak Game",
                },
            )
            self.assertEqual(len(games), 5, "external entries must be deduplicated")
            for game in games:
                self.assertEqual(game["platform"], "PlayStation 3")
                self.assertEqual(game["source"], "rpcs3")
            self.assertTrue(any(str(external) in game["path"] for game in games))
            self.assertTrue(any("missing.iso" in error for error in errors))

    def test_rpcs3_survives_malformed_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config_dir = home / ".config/rpcs3"
            config_dir.mkdir(parents=True)
            (config_dir / "games.yml").write_text("not: [valid", encoding="utf-8")
            (config_dir / "config.yml").write_text("games: {", encoding="utf-8")
            errors = []
            # The fallback parser (no PyYAML on a runtime-only host) accepts
            # shapes it does not understand; a malformed file must still never
            # abort the scan.
            self.assertEqual(import_rpcs3_hdd(home, errors=errors), [])
            self.assertLessEqual(len(errors), 2)

    @linux_flatpak_registry()
    def test_new_emulator_definitions_launch_correctly(self):
        from pkg.parity.parity_emulator_defs import load_adapters

        adapters = {adapter["adapter_id"]: adapter for adapter in load_adapters(ROOT / "emulator_defs")}
        pico8 = adapters["pico8-pico8"]
        self.assertEqual(pico8["platform"], "PICO-8")
        self.assertEqual(pico8["startup_args"], ["-run", "{path}"])
        self.assertEqual(pico8["native_exe"], "pico8")
        self.assertEqual(pico8["native_exe_windows"], "pico8.exe")
        whdload = adapters["amiga-whdload"]
        self.assertEqual(whdload["platform"], "Amiga")
        self.assertEqual(whdload["native_exe"], "fs-uae")
        self.assertEqual(whdload["native_exe_windows"], "fs-uae.exe")
        self.assertEqual(whdload["flatpak_app_id"], "net.fsuae.FS-UAE")
        self.assertEqual(whdload["startup_args"], ["{path}"])


if __name__ == "__main__":
    unittest.main()
