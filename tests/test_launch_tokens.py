"""Tests for pkg.parity.launch_tokens — centralized placeholder replacement."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pkg.parity.launch_tokens import PLACEHOLDERS, apply_tokens, build_launch_args


class TestPlaceholders(unittest.TestCase):
    """PLACEHOLDERS dict contains every expected token."""

    def test_known_tokens_present(self):
        expected = {
            "{path}", "{ImagePath}", "{name}", "{Name}",
            "{dir}", "{Dir}", "{file}", "{File}",
            "{stem}", "{FileNameWithoutExtension}",
            "{platform}", "{Platform}",
            "{app_id}", "{heroic_app_id}", "{lutris_id}",
            "{rom_name}", "{DataDir}", "{EmulatorDir}",
        }
        self.assertEqual(set(PLACEHOLDERS.keys()), expected)


class TestApplyTokens(unittest.TestCase):
    """apply_tokens substitutes all known placeholders."""

    GAME = {
        "path": "/games/roms/Sonic.bin",
        "name": "Sonic",
        "platform": "Genesis",
        "steam_app_id": "1234",
        "heroic_app_id": "hero56",
        "lutris_id": "lut78",
        "rom_name": "sonic.bin",
    }

    def test_path_tokens(self):
        result = apply_tokens("{path}", self.GAME)
        self.assertEqual(result, "/games/roms/Sonic.bin")

    def test_image_path_token(self):
        result = apply_tokens("{ImagePath}", self.GAME)
        self.assertEqual(result, "/games/roms/Sonic.bin")

    def test_name_tokens(self):
        self.assertEqual(apply_tokens("{name}", self.GAME), "Sonic")
        self.assertEqual(apply_tokens("{Name}", self.GAME), "Sonic")

    def test_dir_tokens(self):
        self.assertEqual(apply_tokens("{dir}", self.GAME), "/games/roms")
        self.assertEqual(apply_tokens("{Dir}", self.GAME), "/games/roms")

    def test_file_tokens(self):
        self.assertEqual(apply_tokens("{file}", self.GAME), "Sonic.bin")
        self.assertEqual(apply_tokens("{File}", self.GAME), "Sonic.bin")

    def test_stem_tokens(self):
        self.assertEqual(apply_tokens("{stem}", self.GAME), "Sonic")
        self.assertEqual(apply_tokens("{FileNameWithoutExtension}", self.GAME), "Sonic")

    def test_platform_tokens(self):
        self.assertEqual(apply_tokens("{platform}", self.GAME), "Genesis")
        self.assertEqual(apply_tokens("{Platform}", self.GAME), "Genesis")

    def test_store_id_tokens(self):
        self.assertEqual(apply_tokens("{app_id}", self.GAME), "1234")
        self.assertEqual(apply_tokens("{heroic_app_id}", self.GAME), "hero56")
        self.assertEqual(apply_tokens("{lutris_id}", self.GAME), "lut78")

    def test_rom_name_token(self):
        self.assertEqual(apply_tokens("{rom_name}", self.GAME), "sonic.bin")

    def test_data_dir_token(self):
        result = apply_tokens("{DataDir}", self.GAME, data_dir="/home/user/.local/share")
        self.assertEqual(result, "/home/user/.local/share")

    def test_emulator_dir_token(self):
        result = apply_tokens("{EmulatorDir}", self.GAME, emulator_dir="/usr/bin")
        self.assertEqual(result, "/usr/bin")

    def test_path_override(self):
        result = apply_tokens("{path}", self.GAME, path="/extracted/rom.bin")
        self.assertEqual(result, "/extracted/rom.bin")

    def test_compound_template(self):
        template = "{platform} - {name} ({stem}) @ {dir}"
        result = apply_tokens(template, self.GAME)
        self.assertEqual(result, "Genesis - Sonic (Sonic) @ /games/roms")

    def test_full_command_template(self):
        template = "retroarch -L core.so {path}"
        result = apply_tokens(template, self.GAME)
        self.assertEqual(result, "retroarch -L core.so /games/roms/Sonic.bin")


class TestMinimalGameDict(unittest.TestCase):
    """apply_tokens works with minimal game dicts (not all fields present)."""

    def test_empty_game_dict(self):
        result = apply_tokens("{name} {path}", {})
        self.assertEqual(result, " ")

    def test_only_path(self):
        result = apply_tokens("{path} {name}", {"path": "/rom.nes"})
        self.assertEqual(result, "/rom.nes ")

    def test_only_name(self):
        result = apply_tokens("{name}", {"name": "Mario"})
        self.assertEqual(result, "Mario")

    def test_missing_steam_id(self):
        result = apply_tokens("{app_id}", {})
        self.assertEqual(result, "")


class TestUnknownTokens(unittest.TestCase):
    """Unknown tokens are left as-is."""

    def test_unknown_token_preserved(self):
        result = apply_tokens("{unknown_token}", {})
        self.assertEqual(result, "{unknown_token}")

    def test_no_tokens(self):
        result = apply_tokens("no tokens here", {})
        # Just a string with no tokens — should return unchanged.
        self.assertEqual(result, "no tokens here")


class TestBuildLaunchArgs(unittest.TestCase):
    """build_launch_args splits and substitutes correctly."""

    GAME = {
        "path": "/games/roms/Sonic.bin",
        "name": "Sonic",
        "platform": "Genesis",
    }

    def test_basic_split(self):
        args = build_launch_args("retroarch -L core.so {path}", self.GAME)
        self.assertEqual(args, ["retroarch", "-L", "core.so", "/games/roms/Sonic.bin"])

    def test_path_override(self):
        args = build_launch_args("emulator {path}", self.GAME, path="/extracted.bin")
        self.assertEqual(args, ["emulator", "/extracted.bin"])

    def test_emulator_dir(self):
        args = build_launch_args("{EmulatorDir}/emu {path}", self.GAME, emulator_dir="/usr/bin")
        self.assertEqual(args, ["/usr/bin/emu", "/games/roms/Sonic.bin"])

    def test_data_dir(self):
        args = build_launch_args("emu --data {DataDir} {path}", self.GAME, data_dir="/home/user/.local/share")
        self.assertEqual(args, ["emu", "--data", "/home/user/.local/share", "/games/roms/Sonic.bin"])

    def test_empty_template(self):
        args = build_launch_args("", self.GAME)
        self.assertEqual(args, [])

    def test_path_with_spaces_stays_one_argument(self):
        game = {"path": "/tmp/my game.iso", "name": "Spaced"}
        args = build_launch_args("emu {path}", game)
        self.assertEqual(args, ["emu", "/tmp/my game.iso"])

    def test_flag_after_path_stays_separate(self):
        game = {"path": "/tmp/my game.iso", "name": "Spaced"}
        args = build_launch_args("emu {path} --flag", game)
        self.assertEqual(args, ["emu", "/tmp/my game.iso", "--flag"])

    def test_quoted_path_template(self):
        game = {"path": "/tmp/my game.iso", "name": "Spaced"}
        args = build_launch_args('emu "{path}" --flag', game)
        self.assertEqual(args, ["emu", "/tmp/my game.iso", "--flag"])

    def test_unmatched_quotes_falls_back(self):
        args = build_launch_args('emu "unclosed', {"path": "/rom.iso"})
        self.assertEqual(args, ['emu', '"unclosed'])


class TestLaunchExtra(unittest.TestCase):
    """launch_extra uses split-then-substitute for command templates."""

    def test_quoted_path_command_splits_before_substitute(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from handlers.sessions import SessionHandlers

        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "my game.iso"
            spaced.write_text("x")
            handler = SessionHandlers()
            handler.send_json = mock.Mock()
            state = {
                "games": [{
                    "name": "Test",
                    "applications": [{
                        "path": str(spaced),
                        "command": 'emu "{path}" --flag',
                    }],
                }],
            }
            with mock.patch("handlers.sessions.load_state", return_value=state), \
                 mock.patch("handlers.sessions.subprocess.Popen") as popen:
                handler.launch_extra({
                    "id": 0,
                    "kind": "applications",
                    "index": 0,
                })
            popen.assert_called_once()
            self.assertEqual(popen.call_args[0][0], ["emu", str(spaced), "--flag"])

    def test_direct_path_without_command(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from handlers.sessions import SessionHandlers

        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "game.bin"
            exe.write_text("x")
            handler = SessionHandlers()
            handler.send_json = mock.Mock()
            state = {
                "games": [{
                    "name": "Test",
                    "versions": [{"path": str(exe)}],
                }],
            }
            with mock.patch("handlers.sessions.load_state", return_value=state), \
                 mock.patch("handlers.sessions.subprocess.Popen") as popen:
                handler.launch_extra({
                    "id": 0,
                    "kind": "versions",
                    "index": 0,
                })
            self.assertEqual(popen.call_args[0][0], [str(exe)])

    def test_documents_use_xdg_open(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from handlers.sessions import SessionHandlers

        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "manual.pdf"
            doc.write_text("x")
            handler = SessionHandlers()
            handler.send_json = mock.Mock()
            state = {
                "games": [{
                    "name": "Test",
                    "documents": [{"path": str(doc)}],
                }],
            }
            with mock.patch("handlers.sessions.load_state", return_value=state), \
                 mock.patch("handlers.sessions.shutil.which", return_value="/usr/bin/xdg-open"), \
                 mock.patch("handlers.sessions.subprocess.Popen") as popen:
                handler.launch_extra({
                    "id": 0,
                    "kind": "documents",
                    "index": 0,
                })
            self.assertEqual(popen.call_args[0][0], ["/usr/bin/xdg-open", str(doc)])

    def test_unknown_kind_raises(self):
        from unittest import mock

        from handlers.sessions import SessionHandlers

        handler = SessionHandlers()
        with mock.patch("handlers.sessions.load_state", return_value={"games": [{"name": "Test"}]}):
            with self.assertRaises(ValueError):
                handler.launch_extra({"id": 0, "kind": "bad", "index": 0})

    def test_missing_extra_file_raises(self):
        from unittest import mock

        from handlers.sessions import SessionHandlers

        handler = SessionHandlers()
        state = {"games": [{"name": "Test", "applications": [{"path": "/missing/file.bin"}]}]}
        with mock.patch("handlers.sessions.load_state", return_value=state):
            with self.assertRaises(FileNotFoundError):
                handler.launch_extra({"id": 0, "kind": "applications", "index": 0})


class TestFilledLaunchCommand(unittest.TestCase):
    """_filled_launch_command splits templates before substituting paths."""

    def test_spaced_path_and_trailing_flag(self):
        import shlex

        from pkg.state.imports import _filled_launch_command

        game = {"launch": "emu {path} --flag", "path": "/tmp/my game.iso"}
        args = shlex.split(_filled_launch_command(game))
        self.assertEqual(args, ["emu", "/tmp/my game.iso", "--flag"])

    def test_empty_command(self):
        from pkg.state.imports import _filled_launch_command

        self.assertEqual(_filled_launch_command({}), "")
        self.assertEqual(_filled_launch_command({"launch": "   "}), "")


if __name__ == "__main__":
    unittest.main()
