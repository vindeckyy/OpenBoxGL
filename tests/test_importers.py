#!/usr/bin/env python3
"""Comprehensive unit and integration tests for OpenBox importers and parallel scanner."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

import importers
from arcade import import_arcade, parse_catalog, zip_members
from importers import (
    import_heroic,
    import_lutris,
    import_steam,
    json_records,
    steam_command,
    steam_libraries,
    steam_roots,
    vdf_values,
)
from parity_import import (
    _disc_base,
    _parallel_scandir,
    dedupe_ranked_imports,
    detect_dependencies,
    generate_m3u,
    group_multi_disc,
    import_multi_platform,
    import_rpcs3_hdd,
    import_scummvm,
    import_vita3k,
    parse_cue,
    parse_m3u,
)


class ImportersUnitTests(unittest.TestCase):
    def test_vdf_values(self):
        self.assertEqual(vdf_values('"appid" "42"\n"name" "Real Game"'), {"appid": "42", "name": "Real Game"})
        self.assertEqual(vdf_values(""), {})

    def test_steam_flatpak_fallback(self):
        failed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        def which(name):
            if name == "flatpak":
                return "/usr/bin/flatpak"
            if name == "xdg-open":
                return "/usr/bin/xdg-open"
            return None

        with mock.patch.object(importers.shutil, "which", side_effect=which), \
             mock.patch.object(importers.subprocess, "run", return_value=failed):
            binary, command = steam_command()
        self.assertIn("xdg-open", command)
        self.assertEqual(binary, "/usr/bin/xdg-open")

    def test_lutris_flatpak_fallback(self):
        failed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        def which(name):
            if name == "flatpak":
                return "/usr/bin/flatpak"
            return None

        with mock.patch.object(importers.shutil, "which", side_effect=which), \
             mock.patch.object(importers.subprocess, "run", return_value=failed):
            with self.assertRaises(FileNotFoundError):
                import_lutris(Path("/tmp"), run=lambda *args, **kwargs: failed, which=which)

    def test_steam_heroic_lutris_import_e2e(self):
        with tempfile.TemporaryDirectory() as directory:
            dir_path = Path(directory)
            steamapps = dir_path / ".local/share/Steam/steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_42.acf").write_text(
                '"AppState"\n{\n"appid" "42"\n"name" "Real Game"\n"installdir" "RealGame"\n}'
            )
            games = import_steam(dir_path)
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["steam_app_id"], "42")
            self.assertTrue(games[0]["launch"])

            heroic = dir_path / ".config/heroic"
            (heroic / "legendaryConfig/legendary").mkdir(parents=True)
            (heroic / "gog_store").mkdir()
            (heroic / "nile_config").mkdir()
            (heroic / "legendaryConfig/legendary/installed.json").write_text(
                '{"epic-id":{"title":"Epic Game","install_path":"/games/epic"}}'
            )
            (heroic / "gog_store/installed.json").write_text(
                '{"gog-id":{"title":"GOG Game","install_path":"/games/gog"}}'
            )
            (heroic / "nile_config/installed.json").write_text(
                '{"amazon-id":{"title":"Amazon Game","install_path":"/games/amazon"}}'
            )
            heroic_games = import_heroic(dir_path)
            self.assertEqual({game["source"] for game in heroic_games}, {"Epic", "GOG", "Amazon"})
            self.assertTrue(all("heroic://launch/" in game["launch"] for game in heroic_games))

            class Result:
                stdout = '[{"id":7,"name":"EA Game","installed":true,"service":"ea app","runner":"wine"}]'

            lutris_games = import_lutris(
                dir_path,
                run=lambda *args, **kwargs: Result(),
                which=lambda name: "/usr/bin/lutris" if name == "lutris" else None,
            )
            self.assertEqual(len(lutris_games), 1)
            self.assertEqual(lutris_games[0]["source"], "EA")
            self.assertIn("lutris:rungameid/{lutris_id}", lutris_games[0]["launch"])

    def test_steam_libraries_and_json_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            steam_root = root / ".local/share/Steam"
            (steam_root / "steamapps").mkdir(parents=True)
            (steam_root / "steamapps/libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n"0"\n{{\n"path" "{steam_root}"\n}}\n}}'
            )
            roots = steam_roots(root)
            self.assertEqual(len(roots), 1)
            libs = steam_libraries(steam_root)
            self.assertEqual(len(libs), 1)

            json_file = root / "test.json"
            json_file.write_text('{"g1": {"name": "Test"}}')
            records = json_records(json_file)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0][1]["name"], "Test")

            # Nonexistent json returns empty
            self.assertEqual(json_records(root / "missing.json"), [])


class ParallelScannerAndDiscTests(unittest.TestCase):
    def test_parallel_scanner_correctness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Create nested directory tree
            (root / "nes").mkdir()
            (root / "snes" / "rpg").mkdir(parents=True)
            (root / "psx" / "multi").mkdir(parents=True)
            (root / "ignored").mkdir()

            # Create files
            (root / "nes" / "mario.nes").write_bytes(b"NES\x1a")
            (root / "nes" / "zelda.NES").write_bytes(b"NES\x1a")
            (root / "snes" / "rpg" / "chrono.smc").write_bytes(b"SNES")
            (root / "psx" / "multi" / "ff7 (Disc 1).chd").write_bytes(b"CHD")
            (root / "psx" / "multi" / "ff7 (Disc 2).chd").write_bytes(b"CHD")
            (root / "ignored" / "readme.txt").write_text("readme")
            (root / "ignored" / "game.bin").write_bytes(b"BIN")

            exts = {".nes", ".smc", ".chd"}
            scanned = _parallel_scandir(root, exts)

            # Sequential comparison
            seq_found = sorted(
                path for path in root.rglob("*")
                if path.is_file() and path.suffix.casefold() in exts
            )
            self.assertEqual(len(scanned), len(seq_found))
            self.assertEqual([p.name.casefold() for p in scanned], [p.name.casefold() for p in seq_found])

    def test_parallel_scanner_resilience(self):
        with self.assertRaises(ValueError):
            _parallel_scandir("/nonexistent/directory/path/here", {".nes"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "valid.nes").write_bytes(b"NES")
            # Create broken symlink
            broken = root / "broken.nes"
            try:
                broken.symlink_to(root / "does_not_exist.nes")
            except OSError:
                pass
            scanned = _parallel_scandir(root, {".nes"})
            # Should safely skip broken symlinks
            self.assertTrue(any(p.name == "valid.nes" for p in scanned))

    def test_group_multi_disc_formats(self):
        formats = [
            ("Final Fantasy VII (Disc 1).chd", "Final Fantasy VII (Disc 2).chd", "Final Fantasy VII"),
            ("Metal Gear Solid [Disk 1].iso", "Metal Gear Solid [Disk 2].iso", "Metal Gear Solid"),
            ("Shenmue_CD1.cue", "Shenmue_CD2.cue", "Shenmue"),
            ("Xenogears - DVD1.iso", "Xenogears - DVD2.iso", "Xenogears"),
            ("Chrono Cross (Side A).bin", "Chrono Cross (Side B).bin", "Chrono Cross"),
            ("Resident Evil 2 Disc 1.gcm", "Resident Evil 2 Disc 2.gcm", "Resident Evil 2"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            all_paths = []
            for d1, d2, _ in formats:
                p1, p2 = root / d1, root / d2
                p1.write_bytes(b"1")
                p2.write_bytes(b"2")
                all_paths.extend([p1, p2])
            single = root / "Super Mario.nes"
            single.write_bytes(b"NES")
            all_paths.append(single)

            groups = group_multi_disc(all_paths)
            multi_groups = [g for g in groups if len(g) > 1]
            single_groups = [g for g in groups if len(g) == 1]
            self.assertEqual(len(multi_groups), len(formats))
            self.assertEqual(len(single_groups), 1)
            self.assertEqual(single_groups[0][0].name, "Super Mario.nes")

    def test_disc_base_and_multi_platform_import(self):
        self.assertEqual(_disc_base(Path("Metal Gear Solid (Disc 1).iso")), "Metal Gear Solid")
        self.assertEqual(_disc_base(Path("Standalone.iso")), "Standalone")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Game (Disc 1).iso").write_bytes(b"ISO1")
            (root / "Game (Disc 2).iso").write_bytes(b"ISO2")
            (root / "Single.iso").write_bytes(b"ISO")

            imported = import_multi_platform(root, {".iso"}, {".iso": "Disc image"})
            self.assertEqual(len(imported), 2)
            multi = next(g for g in imported if g["name"] == "Game")
            self.assertEqual(len(multi["discs"]), 2)

    def test_m3u_cue_parsing_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            d1 = root / "Game (Disc 1).cue"
            d2 = root / "Game (Disc 2).cue"
            d1.write_bytes(b"FILE \"Game (Disc 1).bin\" BINARY\n  TRACK 01 MODE1/2352\n")
            d2.write_bytes(b"FILE \"Game (Disc 2).bin\" BINARY\n  TRACK 01 MODE1/2352\n")
            (root / "Game (Disc 1).bin").write_bytes(b"BIN1")
            (root / "Game (Disc 2).bin").write_bytes(b"BIN2")

            m3u_path = generate_m3u([d1, d2], root / "Game.m3u")
            self.assertTrue(m3u_path.is_file())
            parsed = parse_m3u(m3u_path)
            self.assertEqual(len(parsed), 2)
            self.assertEqual(parsed[0].name, "Game (Disc 1).cue")

            cue_parsed = parse_cue(d1)
            self.assertEqual(len(cue_parsed), 1)
            self.assertEqual(cue_parsed[0].name, "Game (Disc 1).bin")

            # Malformed M3U handling
            bad_m3u = root / "bad.m3u"
            bad_m3u.write_bytes(b"# comment\n\xff\xfe\xfa\nrelative_game.chd\n")
            entries = parse_m3u(bad_m3u)
            self.assertTrue(any("relative_game.chd" in str(e) for e in entries))

    def test_dedupe_ranked_imports_fast(self):
        additions = [
            {"name": "Chrono Trigger (USA)", "platform": "SNES", "path": "/roms/ct_usa.smc"},
            {"name": "Chrono Trigger (Japan)", "platform": "SNES", "path": "/roms/ct_jap.smc"},
            {"name": "Super Mario World", "platform": "SNES", "path": "/roms/smw.smc"},
        ]
        with mock.patch("parity_premium.rank_rom_group", side_effect=lambda paths: paths):
            deduped = dedupe_ranked_imports(additions)
        self.assertEqual(len(deduped), 2)
        ct = next(item for item in deduped if "chrono" in item["name"].casefold())
        self.assertIn("version_candidates", ct)

    def test_scummvm_rpcs3_vita3k_importers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scumm_dir = root / ".config/scummvm"
            scumm_dir.mkdir(parents=True)
            (scumm_dir / "scummvm.ini").write_text(
                "[monkey]\ndescription=The Secret of Monkey Island\npath=/games/monkey\n"
            )
            scumm_games = import_scummvm(root)
            self.assertEqual(len(scumm_games), 1)
            self.assertEqual(scumm_games[0]["name"], "The Secret of Monkey Island")

            # RPCS3 HDD import
            rpcs3_dir = root / ".config/rpcs3/dev_hdd0/game/NPUB30001"
            rpcs3_dir.mkdir(parents=True)
            (rpcs3_dir / "PARAM.SFO").write_bytes(b"\x00PSF" + b"\x00" * 30)
            rpcs3_games = import_rpcs3_hdd(root)
            self.assertEqual(len(rpcs3_games), 1)

            # Vita3K import
            vita_dir = root / ".config/Vita3K/ux0/app/PCSB00001/sce_sys"
            vita_dir.mkdir(parents=True)
            (vita_dir / "param.sfo").write_bytes(b"\x00PSF" + b"\x00" * 30)
            vita_games = import_vita3k(root)
            self.assertEqual(len(vita_games), 1)

            # BIOS dependencies
            deps = detect_dependencies("DuckStation", root)
            self.assertTrue("required" in deps)

    def test_arcade_fast_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pacman.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
            xml_data = io.BytesIO(b"""<mame>
                <machine name="pacman" runnable="yes">
                    <description>Pac-Man (Midway)</description>
                </machine>
            </mame>""")
            catalog = parse_catalog(xml_data)
            self.assertIn("pacman", catalog)

            games = import_arcade(root, command="mame {path}", catalog=catalog)
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["name"], "Pac-Man (Midway)")
            self.assertEqual(games[0]["set_type"], "parent")

            members = zip_members(root / "pacman.zip")
            self.assertIsInstance(members, set)

    def test_importer_edge_cases_and_error_handling(self):
        # generate_m3u across drives / roots (ValueError on relative_to)
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            p1 = Path(d1) / "disc1.chd"
            p1.write_bytes(b"1")
            m3u = Path(d2) / "game.m3u"
            out = generate_m3u([p1], m3u)
            self.assertTrue(out.is_file())
            content = out.read_text()
            self.assertIn(str(p1), content)

        # parse_m3u nonexistent and OSError
        self.assertEqual(parse_m3u("/nonexistent/file.m3u"), [])
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("Read error")):
            self.assertEqual(parse_m3u(Path(__file__)), [])

        # parse_cue nonexistent and OSError
        self.assertEqual(parse_cue("/nonexistent/file.cue"), [])
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("Read error")):
            self.assertEqual(parse_cue(Path(__file__)), [])

        # _parallel_scandir invalid root
        with self.assertRaises(ValueError):
            _parallel_scandir("/nonexistent/directory/123", [".nes"])

        # _parallel_scandir progress callback exception resilience
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "game.nes").write_bytes(b"NES")
            def failing_callback(**kwargs):
                raise RuntimeError("callback explosion")
            found = _parallel_scandir(root, [".nes"], progress_callback=failing_callback)
            self.assertEqual(len(found), 1)

        # steam_roots and libraries with corrupt/missing files
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            libs = steam_libraries(root)
            self.assertEqual(libs, [root])

            # Corrupt manifest in steam
            steamapps = root / ".local/share/Steam/steamapps"
            steamapps.mkdir(parents=True)
            (steamapps / "appmanifest_bad.acf").write_bytes(b"\x00\xff")
            games = import_steam(root)
            self.assertEqual(len(games), 0)

        # json_records nonexistent and corrupt
        self.assertEqual(json_records("/nonexistent/file.json"), [])
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "corrupt.json"
            corrupt.write_text("{not valid json")
            self.assertEqual(json_records(corrupt), [])

        # arcade catalog error and zip_members bad zip
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_zip = root / "bad.zip"
            bad_zip.write_bytes(b"not a zip file")
            self.assertEqual(zip_members(bad_zip), set())

            with self.assertRaises(FileNotFoundError):
                import_arcade(root / "nonexistent")


if __name__ == "__main__":
    unittest.main()
