#!/usr/bin/env python3
"""Tests for the per-emulator settings model and launch-argument application."""

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkg.parity import parity_emulator_settings as settings  # noqa: E402
from pkg.parity.parity_emulator_defs import resolve_launch  # noqa: E402


def state_with(emulator_id, *, global_values=None, platforms=None, managed=None):
    entry = {}
    if global_values is not None:
        entry["global"] = global_values
    if platforms is not None:
        entry["platforms"] = platforms
    if managed is not None:
        entry["managed"] = managed
    return {"emulator_settings": {emulator_id: entry}}


class SchemaTests(unittest.TestCase):
    def test_schemas_match_the_plan(self):
        self.assertEqual(set(settings.SETTINGS_SCHEMAS), {"mame", "retroarch"})
        self.assertEqual(set(settings.SETTINGS_SCHEMAS["mame"]["fields"]), {
            "monitor", "borderless", "screen_effect", "lightgun", "trackball",
        })
        self.assertEqual(set(settings.SETTINGS_SCHEMAS["retroarch"]["fields"]), {
            "display_mode", "screen_effect", "run_ahead", "rewind", "upscale",
        })

    def test_normalize_rejects_unknown_and_invalid_values(self):
        with self.assertRaises(ValueError):
            settings.normalize_values("mame", {"unknown": 1})
        with self.assertRaises(ValueError):
            settings.normalize_values("unknown-emulator", {})
        with self.assertRaises(ValueError):
            settings.normalize_values("mame", {"monitor": 5})
        with self.assertRaises(ValueError):
            settings.normalize_values("mame", {"monitor": True})
        with self.assertRaises(ValueError):
            settings.normalize_values("mame", {"monitor": 1.5})
        with self.assertRaises(ValueError):
            settings.normalize_values("mame", {"screen_effect": "crt"})
        with self.assertRaises(ValueError):
            settings.normalize_values("retroarch", {"rewind": "perhaps"})
        with self.assertRaises(ValueError):
            settings.normalize_values("retroarch", [])

    def test_normalize_returns_only_provided_fields(self):
        cleaned = settings.normalize_values("retroarch", {"run_ahead": "2", "rewind": "true"})
        self.assertEqual(cleaned, {"run_ahead": 2, "rewind": True})

    def test_defaults_and_overlay(self):
        state = state_with(
            "mame",
            global_values={"monitor": 2, "borderless": True},
            platforms={"Arcade": {"monitor": 3, "screen_effect": "scanlines"}},
        )
        self.assertEqual(
            settings.effective_values(state, "mame", "Arcade"),
            {
                "monitor": 3,
                "borderless": True,
                "screen_effect": "scanlines",
                "lightgun": False,
                "trackball": False,
            },
        )
        self.assertEqual(settings.effective_values(state, "mame", "Other")["monitor"], 2)
        fresh = settings.effective_values({}, "org.mamedev.MAME", "Arcade")
        self.assertEqual(fresh["monitor"], 1)
        self.assertFalse(fresh["borderless"])

        by_registry_id = state_with("org.mamedev.MAME", global_values={"monitor": 4})
        self.assertEqual(settings.effective_values(by_registry_id, "mame", "")["monitor"], 4)

    def test_invalid_stored_values_are_ignored(self):
        state = state_with("retroarch", global_values={"run_ahead": 99, "rewind": "perhaps"})
        values = settings.effective_values(state, "retroarch", "")
        self.assertEqual(values["run_ahead"], 0)
        self.assertFalse(values["rewind"])

    def test_is_managed(self):
        self.assertFalse(settings.is_managed({}, "mame"))
        self.assertFalse(settings.is_managed(state_with("mame"), "mame"))
        self.assertTrue(settings.is_managed(state_with("mame", managed=True), "mame"))
        self.assertFalse(settings.is_managed(state_with("mame", managed="true"), "mame"))


class MameCommandTests(unittest.TestCase):
    def _apply(self, values, argv=None):
        state = state_with("mame", global_values=values)
        return settings.apply_to_command("mame", "Arcade", argv or ["mame", "rom.zip"], state, "")

    def test_flags_are_inserted_before_the_rom(self):
        argv, env, meta = self._apply({
            "monitor": 2,
            "borderless": True,
            "screen_effect": "scanlines",
            "lightgun": True,
            "trackball": False,
        })
        effect = str(settings.APP_ROOT / "assets" / "mame" / "scanlines.png")
        self.assertEqual(
            argv,
            ["mame", "-screen", "2", "-borderless", "-effect", effect,
             "-lightgun", "-notrackball", "rom.zip"],
        )
        self.assertEqual(env, {})
        self.assertEqual(meta["warnings"], [])
        self.assertEqual(meta["created_files"], [])

    def test_defaults_emit_the_disabled_flags(self):
        argv, _env, _meta = self._apply({})
        self.assertEqual(
            argv, ["mame", "-screen", "1", "-nolightgun", "-notrackball", "rom.zip"]
        )

    def test_missing_effect_asset_is_a_warning_not_a_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(settings, "APP_ROOT", Path(temporary)):
                argv, _env, meta = self._apply({"screen_effect": "scanlines"})
        self.assertNotIn("-effect", argv)
        self.assertTrue(any("scanlines" in warning for warning in meta["warnings"]))
        self.assertEqual(argv[-1], "rom.zip")

    def test_unknown_emulator_returns_argv_unchanged(self):
        argv, env, meta = settings.apply_to_command("nope", "", ["x", "game"], {}, "")
        self.assertEqual(argv, ["x", "game"])
        self.assertEqual(env, {})
        self.assertTrue(meta["warnings"])


class RetroArchCommandTests(unittest.TestCase):
    def test_config_contents_and_appendconfig(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = state_with(
                "retroarch",
                global_values={
                    "display_mode": "windowed",
                    "screen_effect": "scanlines",
                    "run_ahead": 2,
                    "rewind": True,
                    "upscale": 3,
                },
            )
            argv, env, meta = settings.apply_to_command(
                "retroarch", "Super Nintendo", ["retroarch", "rom.sfc"], state, temporary
            )
            path = settings.retroarch_config_path(temporary, "Super Nintendo")
            self.assertEqual(path.name, "super-nintendo.cfg")
            self.assertEqual(meta["created_files"], [str(path)])
            self.assertEqual(argv, ["retroarch", "--appendconfig", str(path), "rom.sfc"])
            self.assertEqual(env, {})
            text = path.read_text(encoding="utf-8")
            self.assertIn('video_fullscreen = "false"', text)
            self.assertIn('video_shader_enable = "true"', text)
            self.assertIn(str(settings.APP_ROOT / "assets" / "shaders" / "scanlines.slangp"), text)
            self.assertIn('run_ahead_enabled = "true"', text)
            self.assertIn('run_ahead_frames = "2"', text)
            self.assertIn('rewind_enable = "true"', text)
            self.assertIn('video_scale = "3"', text)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_borderless_and_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = state_with("retroarch", global_values={"display_mode": "borderless"})
            _argv, _env, _meta = settings.apply_to_command(
                "retroarch", "Arcade", ["retroarch", "rom.zip"], state, temporary
            )
            text = settings.retroarch_config_path(temporary, "Arcade").read_text()
        self.assertIn('video_windowed_fullscreen = "true"', text)
        self.assertIn('video_shader_enable = "false"', text)
        self.assertIn('run_ahead_enabled = "false"', text)
        self.assertIn('rewind_enable = "false"', text)
        self.assertIn('video_scale = "1"', text)

    def test_existing_appendconfig_is_merged(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = state_with("retroarch")
            path = settings.retroarch_config_path(temporary, "Arcade")
            for argv, expected in (
                (["retroarch", "--appendconfig", "/tmp/old.cfg", "rom.zip"],
                 ["retroarch", "--appendconfig", f"/tmp/old.cfg,{path}", "rom.zip"]),
                (["retroarch", "--appendconfig=/tmp/old.cfg", "rom.zip"],
                 ["retroarch", f"--appendconfig=/tmp/old.cfg,{path}", "rom.zip"]),
            ):
                result, _env, _meta = settings.apply_to_command(
                    "retroarch", "Arcade", argv, state, temporary
                )
                self.assertEqual(result, expected)

    def test_io_failure_returns_original_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "not-a-directory"
            blocker.write_text("x")
            state = state_with("retroarch", global_values={"run_ahead": 1})
            argv, env, meta = settings.apply_to_command(
                "retroarch", "Arcade", ["retroarch", "rom.zip"], state, blocker
            )
        self.assertEqual(argv, ["retroarch", "rom.zip"])
        self.assertEqual(env, {})
        self.assertEqual(meta["created_files"], [])
        self.assertTrue(any("could not be written" in warning for warning in meta["warnings"]))

    def test_missing_shader_asset_is_a_warning(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as root:
            with mock.patch.object(settings, "APP_ROOT", Path(root)):
                state = state_with("retroarch", global_values={"screen_effect": "scanlines"})
                _argv, _env, meta = settings.apply_to_command(
                    "retroarch", "Arcade", ["retroarch", "rom.zip"], state, temporary
                )
            text = settings.retroarch_config_path(temporary, "Arcade").read_text()
        self.assertNotIn("video_shader_enable", text)
        self.assertTrue(any("shader is missing" in warning for warning in meta["warnings"]))


class ResolveLaunchSettingsTests(unittest.TestCase):
    def test_retroarch_settings_are_applied_when_managed(self):
        with tempfile.TemporaryDirectory() as temporary:
            rom = Path(temporary) / "game.rom"
            rom.write_bytes(b"rom")
            state = state_with(
                "retroarch",
                global_values={"run_ahead": 2, "rewind": True},
                managed=True,
            )
            game = {
                "name": "Game",
                "path": str(rom),
                "platform": "Arcade",
                "launch": "retroarch {path}",
            }
            result = resolve_launch(game, {}, data_dir=temporary, state=state)
            self.assertIn("--appendconfig", result["args"])
            config = Path(result["args"][result["args"].index("--appendconfig") + 1])
            text = config.read_text(encoding="utf-8")
        self.assertIn('run_ahead_enabled = "true"', text)
        self.assertIn('run_ahead_frames = "2"', text)
        self.assertIn('rewind_enable = "true"', text)
        self.assertEqual(result["warnings"], [])

    def test_mame_flags_are_applied_when_managed(self):
        with tempfile.TemporaryDirectory() as temporary:
            rom = Path(temporary) / "game.zip"
            rom.write_bytes(b"zip")
            state = state_with(
                "mame",
                global_values={"borderless": True, "lightgun": True},
                managed=True,
            )
            game = {
                "name": "Game",
                "path": str(rom),
                "platform": "Arcade",
                "launch": "mame {path}",
            }
            result = resolve_launch(game, {}, data_dir=temporary, state=state)
        self.assertEqual(
            result["args"],
            ["mame", "-screen", "1", "-borderless", "-lightgun", "-notrackball", str(rom)],
        )

    def test_unmanaged_emulator_keeps_the_original_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            rom = Path(temporary) / "game.zip"
            rom.write_bytes(b"zip")
            state = state_with("mame", global_values={"borderless": True}, managed=False)
            game = {
                "name": "Game",
                "path": str(rom),
                "platform": "Arcade",
                "launch": "mame {path}",
            }
            result = resolve_launch(game, {}, data_dir=temporary, state=state)
        self.assertEqual(result["args"], ["mame", str(rom)])
        self.assertEqual(result["warnings"], [])


class AssetTests(unittest.TestCase):
    def test_bundled_assets_exist_and_are_valid(self):
        png = ROOT / "assets" / "mame" / "scanlines.png"
        data = png.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IHDR", data)
        self.assertIn(b"IDAT", data)
        self.assertTrue(data.endswith(b"IEND\xaeB`\x82"))

        preset = (ROOT / "assets" / "shaders" / "scanlines.slangp").read_text(encoding="utf-8")
        self.assertIn("shaders = 1", preset)
        self.assertIn("shader0 = scanlines.slang", preset)


if __name__ == "__main__":
    unittest.main()
