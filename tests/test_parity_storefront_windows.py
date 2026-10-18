"""Tests for native Windows storefront discovery (GOG, EA app, Ubisoft Connect)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_storefront_windows import (  # noqa: E402
    _dir,
    _ea_content_ids,
    _explorer,
    _find_gog_info,
    _gog_install_dir,
    _gog_task,
    _read_json,
    import_ea,
    import_gog,
    import_ubisoft,
)


def fake_registry(tree):
    def read(root, path):
        return tree.get((root, path), {})
    return read


class GogImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "Game"
        self.install.mkdir()
        (self.install / "Game.exe").write_text("", encoding="utf-8")
        (self.install / "goggame-123.info").write_text(json.dumps({
            "gameId": "123",
            "name": "Test Game",
            "playTasks": [{"isPrimary": True, "path": "Game.exe", "arguments": "--safe"}],
        }), encoding="utf-8")
        self.tree = {
            ("HKCU", r"Software\GOG.com\Games"): {
                "123": {"gameName": "Test Game", "path": str(self.install), "exe": "Game.exe"},
            },
        }

    def test_imports_from_info_and_registry(self):
        games = import_gog(registry=fake_registry(self.tree))
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game["name"], "Test Game")
        self.assertEqual(game["gog_id"], "123")
        self.assertEqual(game["path"], str(self.install / "Game.exe"))
        self.assertIn("--safe", game["launch"])
        self.assertTrue(game["path"].startswith(str(self.install)))

    def test_missing_info_falls_back_to_registry_exe(self):
        (self.install / "goggame-123.info").unlink()
        games = import_gog(registry=fake_registry(self.tree))
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["path"], str(self.install / "Game.exe"))

    def test_escaping_task_is_refused(self):
        outside = self.base / "outside.exe"
        outside.write_text("", encoding="utf-8")
        (self.install / "goggame-123.info").write_text(json.dumps({
            "gameId": "123",
            "playTasks": [{"isPrimary": True, "path": r"..\outside.exe"}],
        }), encoding="utf-8")
        tree = {
            ("HKCU", r"Software\GOG.com\Games"): {
                "123": {"gameName": "Test Game", "path": str(self.install)},
            },
        }
        errors = []
        games = import_gog(registry=fake_registry(tree), errors=errors)
        self.assertEqual(games, [])
        self.assertTrue(errors)

    def test_non_numeric_id_is_skipped(self):
        tree = {
            ("HKCU", r"Software\GOG.com\Games"): {
                "not-an-id": {"gameName": "Nope", "path": str(self.install)},
            },
        }
        self.assertEqual(import_gog(registry=fake_registry(tree)), [])


class EaImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.game = self.base / "EA Games" / "Test Game"
        installer = self.game / "__Installer"
        installer.mkdir(parents=True)
        (installer / "installerdata.xml").write_text(
            "<Diploma><contentIDs><contentID>222</contentID>"
            "<contentID>111</contentID></contentIDs></Diploma>",
            encoding="utf-8",
        )
        self.opener = str(self.base / "explorer.exe")

    def test_imports_content_ids_and_launch_uri(self):
        games = import_ea(environ={"ProgramFiles": str(self.base)}, opener=self.opener)
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game["name"], "Test Game")
        self.assertEqual(game["ea_id"], "111")
        self.assertIn("origin2://game/launch?offerIds=111,222", game["launch"])
        self.assertEqual(game["install_dir"], str(self.game))

    def test_missing_manifest_is_skipped(self):
        (self.game / "__Installer" / "installerdata.xml").unlink()
        errors = []
        games = import_ea(
            environ={"ProgramFiles": str(self.base)}, opener=self.opener, errors=errors
        )
        self.assertEqual(games, [])
        self.assertTrue(errors)

    def test_malformed_manifest_is_skipped(self):
        (self.game / "__Installer" / "installerdata.xml").write_text("<broken", encoding="utf-8")
        self.assertEqual(import_ea(environ={"ProgramFiles": str(self.base)}, opener=self.opener), [])


class UbisoftImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.install = self.base / "Test Ubisoft Game"
        self.install.mkdir()
        self.opener = str(self.base / "explorer.exe")

    def test_imports_install_dir(self):
        tree = {
            ("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"): {
                "777": {"InstallDir": str(self.install)},
            },
        }
        games = import_ubisoft(registry=fake_registry(tree), opener=self.opener)
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game["ubisoft_id"], "777")
        self.assertEqual(game["name"], "Test Ubisoft Game")
        self.assertIn("uplay://launch/777/0", game["launch"])

    def test_missing_install_dir_is_skipped(self):
        tree = {
            ("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"): {
                "777": {"InstallDir": str(self.base / "gone")},
            },
        }
        errors = []
        self.assertEqual(import_ubisoft(registry=fake_registry(tree), opener=self.opener, errors=errors), [])
        self.assertTrue(errors)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_read_json_rejects_bad_oversize_and_non_object(self):
        bad = self.base / "bad.info"
        bad.write_text("{not json", encoding="utf-8")
        self.assertIsNone(_read_json(bad))
        self.assertIsNone(_read_json(self.base / "missing.info"))
        huge = self.base / "huge.info"
        huge.write_text(" " * 1_100_000, encoding="utf-8")
        self.assertIsNone(_read_json(huge))
        array = self.base / "array.info"
        array.write_text("[]", encoding="utf-8")
        self.assertIsNone(_read_json(array))

    def test_find_gog_info_reads_subdirectories_and_tolerates_a_file(self):
        install = self.base / "Game"
        sub = install / "game"
        sub.mkdir(parents=True)
        (install / "goggame-99.info").write_text("{}", encoding="utf-8")
        info = sub / "goggame-55.info"
        info.write_text("{}", encoding="utf-8")
        self.assertEqual(_find_gog_info(install, "55"), info)
        self.assertEqual(_find_gog_info(install, "77"), install / "goggame-99.info")
        self.assertIsNone(_find_gog_info(info, "55"))

    def test_gog_task_handles_missing_tasks_and_missing_path(self):
        install = self.base / "Game"
        install.mkdir()
        self.assertEqual(_gog_task(install, {}), ("", []))
        self.assertEqual(_gog_task(install, {"playTasks": [{}]}), ("", []))
        self.assertEqual(_gog_task(install, None), ("", []))

    def test_gog_install_dir_can_come_from_an_absolute_exe(self):
        exe = self.base / "Game.exe"
        exe.write_text("", encoding="utf-8")
        self.assertEqual(_gog_install_dir({"exe": str(exe)}), self.base)
        self.assertIsNone(_gog_install_dir({}))
        self.assertIsNone(_dir(""))

    def test_ea_oversize_manifest_is_refused(self):
        xml = self.base / "installerdata.xml"
        xml.write_text("<x>" + " " * 2_200_000 + "</x>", encoding="utf-8")
        self.assertEqual(_ea_content_ids(xml), [])

    def test_explorer_default_uses_systemroot(self):
        self.assertEqual(_explorer({"SYSTEMROOT": r"C:\Windows"}), str(Path(r"C:\Windows") / "explorer.exe"))
        self.assertEqual(_explorer({}, opener="X:/explorer.exe"), "X:/explorer.exe")


class NativeImportEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_gog_missing_install_dir_reports_and_skips(self):
        tree = {("HKCU", r"Software\GOG.com\Games"): {"123": {"path": ""}}}
        errors = []
        self.assertEqual(import_gog(registry=fake_registry(tree), errors=errors), [])
        self.assertTrue(errors)

    def test_gog_info_game_id_mismatch_falls_back_to_registry(self):
        install = self.base / "Game"
        install.mkdir()
        (install / "Game.exe").write_text("", encoding="utf-8")
        (install / "goggame-123.info").write_text(json.dumps({
            "gameId": "999", "playTasks": [{"isPrimary": True, "path": "Game.exe"}],
        }), encoding="utf-8")
        tree = {("HKCU", r"Software\GOG.com\Games"): {
            "123": {"gameName": "Registry Name", "path": str(install), "exe": "Game.exe"},
        }}
        games = import_gog(registry=fake_registry(tree))
        self.assertEqual(games[0]["name"], "Registry Name")

    def test_ea_registry_install_dir_and_non_object_values(self):
        game = self.base / "EA Games" / "From Registry"
        installer = game / "__Installer"
        installer.mkdir(parents=True)
        (installer / "installerdata.xml").write_text("<x><contentID>42</contentID></x>", encoding="utf-8")
        tree = {("HKLM", r"SOFTWARE\EA Games"): {
            "From Registry": {"Install Dir": str(self.base / "EA Games")},
            "junk": "not-a-dict",
        }}
        games = import_ea(environ={}, registry=fake_registry(tree), opener=str(self.base / "explorer.exe"))
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["ea_id"], "42")

    def test_ea_duplicate_content_id_is_imported_once(self):
        for name in ("One", "Two"):
            installer = self.base / "EA Games" / name / "__Installer"
            installer.mkdir(parents=True)
            (installer / "installerdata.xml").write_text("<x><contentID>42</contentID></x>", encoding="utf-8")
        games = import_ea(
            environ={"ProgramFiles": str(self.base)},
            opener=str(self.base / "explorer.exe"),
        )
        self.assertEqual(len(games), 1)

    def test_ea_registry_value_can_be_the_game_dir(self):
        game = self.base / "Custom Install" / "From Registry Direct"
        installer = game / "__Installer"
        installer.mkdir(parents=True)
        (installer / "installerdata.xml").write_text("<x><contentID>43</contentID></x>", encoding="utf-8")
        tree = {("HKLM", r"SOFTWARE\EA Games"): {
            "Direct": {"InstallDir": str(game)},
        }}
        games = import_ea(environ={}, registry=fake_registry(tree), opener=str(self.base / "explorer.exe"))
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["ea_id"], "43")

    def test_ubisoft_duplicate_and_non_object_values(self):
        install = self.base / "Game"
        install.mkdir()
        tree = {
            ("HKCU", r"SOFTWARE\Ubisoft\Launcher\Installs"): {
                "777": {"InstallDir": str(install)},
                "junk": "not-a-dict",
            },
            ("HKLM", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"): {
                "777": {"InstallDir": str(install)},
            },
        }
        games = import_ubisoft(registry=fake_registry(tree), opener=str(self.base / "explorer.exe"))
        self.assertEqual(len(games), 1)


@unittest.skipUnless(sys.platform == "win32", "the real registry is a Windows-only source")
class WindowsRegistryIntegrationTests(unittest.TestCase):
    """Exercise the real winreg paths, not a fake registry reader."""

    def setUp(self):
        import winreg

        self.winreg = winreg
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def _delete(self, path):
        try:
            self.winreg.DeleteKey(self.winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    def test_gog_registry_end_to_end(self):
        game_id = str(900000 + os.getpid() % 100000)
        install = self.base / "GOG Game"
        install.mkdir()
        (install / "Game.exe").write_text("", encoding="utf-8")
        (install / f"goggame-{game_id}.info").write_text(json.dumps({
            "gameId": game_id,
            "name": "Registry GOG",
            "playTasks": [{"isPrimary": True, "path": "Game.exe"}],
        }), encoding="utf-8")
        path = rf"Software\GOG.com\Games\{game_id}"
        self.addCleanup(self._delete, path)
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, path) as key:
            self.winreg.SetValueEx(key, "gameName", 0, self.winreg.REG_SZ, "Registry GOG")
            self.winreg.SetValueEx(key, "path", 0, self.winreg.REG_SZ, str(install))
        games = [game for game in import_gog() if game.get("gog_id") == game_id]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["path"], str(install / "Game.exe"))

    def test_ubisoft_registry_end_to_end(self):
        game_id = str(800000 + os.getpid() % 100000)
        install = self.base / "Ubisoft Game"
        install.mkdir()
        path = rf"SOFTWARE\Ubisoft\Launcher\Installs\{game_id}"
        self.addCleanup(self._delete, path)
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, path) as key:
            self.winreg.SetValueEx(key, "InstallDir", 0, self.winreg.REG_SZ, str(install))
        games = [game for game in import_ubisoft() if game.get("ubisoft_id") == game_id]
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["install_dir"], str(install))


if __name__ == "__main__":
    unittest.main()
