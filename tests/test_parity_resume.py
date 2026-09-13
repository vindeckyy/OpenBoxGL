"""Quick Resume state schema, capture, and argv-injection tests (T1-core)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webapp_state  # noqa: E402  # registers the parity_* flat finder + dep registry
from pkg.parity import parity_emulator_defs as defs  # noqa: E402
from pkg.parity import parity_resume  # noqa: E402
from pkg.parity import launch_tokens  # noqa: E402
from pkg.state import _deps  # noqa: E402


def _game(**extra):
    game = {
        "game_id": "stable-1",
        "name": "Fixture Game",
        "platform": "SNES",
        "path": "/bin/true",
        "emulator_adapter_id": "retroarch-snes",
    }
    game.update(extra)
    return game


def _write(path, data=b"state-bytes"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class StateSchemaTests(unittest.TestCase):
    """emulator_defs `state:` blocks normalize per the M0a matrix."""

    def test_state_keys_are_safe_and_collision_resistant(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        self.assertNotEqual(
            parity_resume.state_key({"game_id": "a/b"}),
            parity_resume.state_key({"game_id": "a_b"}),
        )
        safe = parity_resume.state_dir_for({"game_id": "safe-id"}, root)
        self.assertEqual(safe.resolve().parent, parity_resume.resume_root(root).resolve())

        outside = root / "outside"
        outside.mkdir()
        link = parity_resume.resume_root(root)
        link.mkdir(parents=True)
        try:
            (link / "safe-link").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable in this environment")
        with self.assertRaises(ValueError):
            parity_resume.state_dir_for({"game_id": "safe-link"}, root)

    def test_every_def_carries_a_state_block(self):
        adapters = defs.load_adapters()
        self.assertEqual(len(adapters), 24)
        for adapter in adapters:
            self.assertIn(adapter["state"]["kind"], parity_resume.STATE_KINDS, adapter["adapter_id"])

    def test_m0a_matrix_kinds(self):
        by_id = {a["adapter_id"]: a for a in defs.load_adapters()}
        retroarch_ids = [aid for aid in by_id if aid.startswith("retroarch-")]
        self.assertEqual(len(retroarch_ids), 8)
        for aid in retroarch_ids:
            self.assertEqual(by_id[aid]["state"]["kind"], "retroarch")
        self.assertEqual(by_id["mame-arcade"]["state"]["kind"], "adapter-cli")
        for aid in ("duckstation-psx", "pcsx2-ps2", "ppsspp-psp", "rpcs3-ps3", "scummvm-scummvm"):
            self.assertEqual(by_id[aid]["state"]["kind"], "adapter-cli", aid)
        for aid in ("dolphin-gamecube", "dolphin-wii", "dolphin-wiiware", "dolphin-iso"):
            self.assertEqual(by_id[aid]["state"]["kind"], "adapter-cli", aid)
        for aid in ("melonds-nds", "cemu-wiiu", "eden-switch", "vita3k-vita", "xemu-xbox", "xenia-xbox360"):
            self.assertEqual(by_id[aid]["state"]["kind"], "none", aid)

    def test_full_capture_defs_have_templates_and_capture(self):
        by_id = {a["adapter_id"]: a for a in defs.load_adapters()}
        for aid, adapter in by_id.items():
            state = adapter["state"]
            if state["kind"] == "none":
                self.assertFalse(state["template"], aid)
                self.assertFalse(state["capture"], aid)
            else:
                self.assertTrue(state["template"], f"{aid} needs a resume template")
            if aid.startswith("retroarch-") or aid == "mame-arcade":
                self.assertTrue(state["capture"], f"{aid} must arm capture")

    def test_fallback_parser_reads_nested_state(self):
        text = """adapter_id: demo
emulator_id: demo.emu
platform: Demo
state:
  kind: adapter-cli
  template: "--state={state_path}"
  capture: "--savepath={state_dir}"
  glob: "*.s00"
"""
        with patch.object(defs, "yaml", None):
            raw = defs._parse_yaml(text)
        self.assertIsInstance(raw["state"], dict)
        self.assertEqual(raw["state"]["kind"], "adapter-cli")
        self.assertEqual(raw["state"]["template"], "--state={state_path}")

    def test_normalize_adapter_state_defaults_and_validation(self):
        bare = {"adapter_id": "x", "emulator_id": "e", "platform": "P"}
        self.assertEqual(defs._normalize_adapter(bare)["state"]["kind"], "none")
        bad = dict(bare, state={"kind": "bogus"})
        with self.assertRaises(ValueError):
            defs._normalize_adapter(bad)
        bad_token = dict(bare, state={"kind": "adapter-cli", "template": "--s {bogus_token}"})
        with self.assertRaises(ValueError):
            defs._normalize_adapter(bad_token)
        listed = dict(bare, state={
            "kind": "adapter-cli",
            "template": ["-s", "{state_path}"],
            "capture": ["--savepath={state_dir}"],
            "glob": ["*.s00", "*.s01"],
        })
        norm = defs._normalize_adapter(listed)["state"]
        self.assertEqual(norm["template"], ["-s", "{state_path}"])
        self.assertEqual(norm["capture"], ["--savepath={state_dir}"])
        self.assertEqual(norm["glob"], ["*.s00", "*.s01"])

    def test_registry_and_definitions_expose_state(self):
        registry = defs.load_registry()
        ra = next(a for a in registry["adapters"] if a["adapter_id"] == "retroarch-snes")
        self.assertEqual(ra["state"]["kind"], "retroarch")
        definitions = defs.load_definitions()
        self.assertIn("state", definitions[0])


class LaunchTokenTests(unittest.TestCase):
    def test_state_tokens_resolve(self):
        out = launch_tokens.apply_tokens(
            "--flag {state_path} {state_dir} {state_name} {state_config}",
            {},
            state_path="/s/resume.state",
            state_dir="/s",
            state_name="slot0",
            state_config="/s/session.cfg",
        )
        self.assertEqual(out, "--flag /s/resume.state /s slot0 /s/session.cfg")

    def test_state_tokens_validate_as_known(self):
        self.assertEqual(launch_tokens.find_invalid_tokens("-s {state_path}"), [])
        self.assertEqual(
            launch_tokens.validate_startup_args(["-s", "{state_path}", "{state_dir}", "{state_name}", "{state_config}"]),
            [],
        )
        self.assertEqual(launch_tokens.find_invalid_tokens("--x {state_typo}"), ["{state_typo}"])

    def test_existing_tokens_unaffected(self):
        out = launch_tokens.apply_tokens("{path} {name} {DataDir}", {"name": "G"}, path="/rom/a.nes", data_dir="/d")
        self.assertEqual(out, "/rom/a.nes G /d")


class AdapterResolutionTests(unittest.TestCase):
    """adapter_for_launch mirrors resolve_launch precedence."""

    def setUp(self):
        self.adapter = {
            "adapter_id": "t1-fake",
            "emulator_id": "fake.emu",
            "label": "Fake",
            "platform": "Test",
            "extensions": ["fake"],
            "native_exe": "fakeemu",
            "flatpak_app_id": "",
            "startup_args": ["{path}"],
            "executable_patterns": [],
            "state": {"kind": "adapter-cli", "template": ["--state={state_path}"], "capture": [], "glob": ["*"]},
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-fake"] = self.adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-fake", None)

    def test_game_launch_command_bypasses_adapter(self):
        game = _game(launch="/bin/true {path}", emulator_adapter_id="t1-fake")
        adapter, precedence = parity_resume.adapter_for_launch(game, {})
        self.assertIsNone(adapter)
        self.assertEqual(precedence, "game_launch")

    def test_bound_adapter_wins_when_detected(self):
        game = _game(emulator_adapter_id="t1-fake")
        adapter, precedence = parity_resume.adapter_for_launch(game, {}, which=lambda name: "/usr/bin/fakeemu" if name == "fakeemu" else None)
        self.assertEqual(adapter["adapter_id"], "t1-fake")
        self.assertEqual(precedence, "game_adapter")

    def test_profile_command_disables_injection(self):
        game = _game(emulator_adapter_id="t1-fake")
        adapter, precedence = parity_resume.adapter_for_launch(
            game, {"SNES": "custom {path}"}, which=lambda name: None,
        )
        self.assertIsNone(adapter)
        self.assertEqual(precedence, "platform_profile")

    def test_registry_fallback_when_unbound(self):
        game = _game(emulator_adapter_id="", emulator_id="", platform="Test")
        registry = defs._registry()
        registry["by_platform"].setdefault("Test", []).append(self.adapter)
        self.addCleanup(registry["by_platform"]["Test"].remove, self.adapter)
        adapter, precedence = parity_resume.adapter_for_launch(
            game, {}, which=lambda name: "/usr/bin/fakeemu" if name == "fakeemu" else None,
        )
        self.assertIsNotNone(adapter)
        self.assertEqual(precedence, "registry_adapter")

    def test_direct_exe_when_nothing_resolves(self):
        game = _game(emulator_adapter_id="", emulator_id="", platform="Nope")
        adapter, precedence = parity_resume.adapter_for_launch(game, {}, which=lambda name: None)
        self.assertIsNone(adapter)
        self.assertEqual(precedence, "direct_exe")


class InjectArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_parent = Path(self.tmp.name)
        self.adapter = {
            "adapter_id": "t1-ra",
            "emulator_id": "fake.ra",
            "label": "FakeRA",
            "platform": "SNES",
            "extensions": ["sfc"],
            "native_exe": "/bin/true",
            "flatpak_app_id": "",
            "startup_args": ["{path}"],
            "executable_patterns": [],
            "state": {
                "kind": "retroarch",
                "template": ["-s", "{state_path}", "--appendconfig", "{state_config}"],
                "capture": ["--appendconfig", "{state_config}"],
                "glob": ["*.state*"],
            },
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-ra"] = self.adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-ra", None)
        self.game = _game(emulator_adapter_id="t1-ra")

    def _sdir(self):
        return parity_resume.state_dir_for(self.game, self.data_parent)

    def _meta(self):
        sdir = self._sdir()
        state_file = _write(sdir / "resume.state")
        meta = {
            "adapter_id": "t1-ra",
            "emulator_version": parity_resume.emulator_fingerprint(self.adapter),
            "kind": "retroarch",
            "file": "resume.state",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        }
        (sdir / "state.json").write_text(json.dumps(meta))
        return state_file

    def test_normal_launch_arms_capture_and_writes_cfg(self):
        args = parity_resume.inject_resume_args(
            self.game, ["/bin/true", "/rom.sfc"],
            profiles={}, settings={"quick_resume_enabled": True},
            resume=False, data_parent=self.data_parent,
        )
        self.assertEqual(args[:2], ["/bin/true", "/rom.sfc"])
        self.assertIn("--appendconfig", args)
        cfg = self._sdir() / parity_resume.RA_CONFIG_NAME
        self.assertTrue(cfg.is_file())
        text = cfg.read_text()
        self.assertIn('savestate_auto_save = "true"', text)
        self.assertIn('savestate_auto_load = "false"', text)
        self.assertIn(f'savestate_directory = "{self._sdir()}"', text)
        self.assertIn('network_cmd_enable = "true"', text)
        self.assertIn(str(cfg), args)

    def test_resume_launch_injects_state_path(self):
        state_file = self._meta()
        args = parity_resume.inject_resume_args(
            self.game, ["/bin/true", "/rom.sfc"],
            profiles={}, settings={}, resume=True, data_parent=self.data_parent,
        )
        self.assertIn("-s", args)
        self.assertIn(str(state_file), args)
        self.assertIn("--appendconfig", args)
        # resumed sessions re-arm capture via the same template (appendconfig present)

    def test_session_config_symlink_does_not_overwrite_external_file(self):
        outside = self.data_parent / "outside.cfg"
        outside.write_text("keep me", encoding="utf-8")
        config = self._sdir() / parity_resume.RA_CONFIG_NAME
        try:
            config.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable in this environment")
        args = parity_resume.inject_resume_args(
            self.game, ["/bin/true"], profiles={}, settings={}, resume=False,
            data_parent=self.data_parent,
        )
        self.assertEqual(args, ["/bin/true"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me")

    def test_resume_missing_state_raises(self):
        with self.assertRaises(FileNotFoundError):
            parity_resume.inject_resume_args(
                self.game, ["/bin/true"], profiles={}, settings={}, resume=True,
                data_parent=self.data_parent,
            )

    def test_resume_on_unsupported_adapter_raises(self):
        game = _game(emulator_adapter_id="", emulator_id="", platform="Nope")
        with self.assertRaises(ValueError):
            parity_resume.inject_resume_args(
                game, ["/bin/true"], profiles={}, settings={}, resume=True,
                data_parent=self.data_parent, which=lambda name: None,
            )

    def test_disabled_setting_no_capture_and_resume_refused(self):
        args = parity_resume.inject_resume_args(
            self.game, ["/bin/true"], profiles={},
            settings={"quick_resume_enabled": False}, resume=False,
            data_parent=self.data_parent,
        )
        self.assertEqual(args, ["/bin/true"])
        with self.assertRaises(ValueError):
            parity_resume.inject_resume_args(
                self.game, ["/bin/true"], profiles={},
                settings={"quick_resume_enabled": False}, resume=True,
                data_parent=self.data_parent,
            )

    def test_no_adapter_normal_launch_unchanged(self):
        game = _game(emulator_adapter_id="", emulator_id="", platform="Nope")
        args = parity_resume.inject_resume_args(
            game, ["/bin/true"], profiles={}, settings={}, resume=False,
            data_parent=self.data_parent, which=lambda name: None,
        )
        self.assertEqual(args, ["/bin/true"])

    def test_none_kind_adapter_is_noop(self):
        none_adapter = dict(self.adapter)
        none_adapter["adapter_id"] = "t1-none"
        none_adapter["state"] = {"kind": "none", "template": [], "capture": [], "glob": []}
        registry = defs._registry()
        registry["by_adapter_id"]["t1-none"] = none_adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-none", None)
        game = _game(emulator_adapter_id="t1-none")
        args = parity_resume.inject_resume_args(
            game, ["/bin/true"], profiles={}, settings={}, resume=False,
            data_parent=self.data_parent,
        )
        self.assertEqual(args, ["/bin/true"])

    def test_stale_state_requires_allow_stale(self):
        self._meta()
        meta_path = self._sdir() / "state.json"
        meta = json.loads(meta_path.read_text())
        meta["emulator_version"] = "old-version"
        meta_path.write_text(json.dumps(meta))
        with self.assertRaises(ValueError):
            parity_resume.inject_resume_args(
                self.game, ["/bin/true"], profiles={}, settings={}, resume=True,
                data_parent=self.data_parent,
            )
        args = parity_resume.inject_resume_args(
            self.game, ["/bin/true"], profiles={}, settings={}, resume=True,
            data_parent=self.data_parent, allow_stale=True,
        )
        self.assertIn(str(self._sdir() / "resume.state"), args)


class CollectStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_parent = Path(self.tmp.name)
        self.adapter = {
            "adapter_id": "t1-collect",
            "emulator_id": "fake.emu",
            "label": "Fake",
            "platform": "SNES",
            "extensions": ["sfc"],
            "native_exe": "/bin/true",
            "flatpak_app_id": "",
            "startup_args": ["{path}"],
            "executable_patterns": [],
            "state": {
                "kind": "retroarch",
                "template": ["-s", "{state_path}"],
                "capture": ["--appendconfig", "{state_config}"],
                "glob": ["*.state*"],
            },
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-collect"] = self.adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-collect", None)
        self.game = _game(emulator_adapter_id="t1-collect")
        self.sdir = parity_resume.state_dir_for(self.game, self.data_parent)

    def test_collect_moves_newest_to_canonical_and_writes_meta(self):
        _write(self.sdir / "game.sfc.state.auto", b"auto-state")
        meta = parity_resume.collect_resume_state(
            self.game, {"quick_resume_enabled": True},
            profiles={}, data_parent=self.data_parent, launch_id="L1",
        )
        self.assertIsNotNone(meta)
        canonical = self.sdir / "resume.state"
        self.assertTrue(canonical.is_file())
        self.assertEqual(canonical.read_bytes(), b"auto-state")
        self.assertEqual(meta["file"], "resume.state")
        self.assertEqual(meta["adapter_id"], "t1-collect")
        self.assertEqual(meta["emulator_version"], parity_resume.emulator_fingerprint(self.adapter))
        self.assertEqual(meta["launch_id"], "L1")
        self.assertTrue(meta["capture_id"].startswith("capture-"))
        on_disk = json.loads((self.sdir / "state.json").read_text())
        self.assertEqual(on_disk["file"], "resume.state")

    def test_collect_picks_up_state_thumbnail(self):
        _write(self.sdir / "game.state", b"state")
        _write(self.sdir / "game.png", b"\x89png")
        meta = parity_resume.collect_resume_state(
            self.game, {}, profiles={}, data_parent=self.data_parent,
        )
        self.assertEqual(meta["thumbnail"], "game.png")

    def test_collect_falls_back_to_game_media_thumbnail(self):
        shot = _write(self.data_parent / "media" / "shot.png", b"\x89png")
        _write(self.sdir / "game.state", b"state")
        meta = parity_resume.collect_resume_state(
            _game(emulator_adapter_id="t1-collect", screenshot=str(shot)),
            {}, profiles={}, data_parent=self.data_parent,
        )
        self.assertEqual(meta["thumbnail"], "thumb.png")
        self.assertTrue((self.sdir / "thumb.png").is_file())

    def test_collect_retention_bounds_archives(self):
        _write(self.sdir / "first.state", b"one")
        parity_resume.collect_resume_state(self.game, {}, profiles={}, data_parent=self.data_parent)
        time.sleep(0.01)
        _write(self.sdir / "second.state", b"two")
        meta = parity_resume.collect_resume_state(
            self.game, {"state_retention": 2}, profiles={}, data_parent=self.data_parent,
        )
        self.assertEqual((self.sdir / "resume.state").read_bytes(), b"two")
        archive = sorted((self.sdir / "archive").glob("*"))
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0].read_bytes(), b"one")
        # retention=1 keeps only the canonical file
        time.sleep(0.01)
        _write(self.sdir / "third.state", b"three")
        parity_resume.collect_resume_state(
            self.game, {"state_retention": 1}, profiles={}, data_parent=self.data_parent,
        )
        self.assertEqual(sorted((self.sdir / "archive").glob("*")), [])
        self.assertEqual(meta["adapter_id"], "t1-collect")

    def test_collect_does_not_archive_or_prune_through_symlink(self):
        outside = self.data_parent / "outside"
        outside.mkdir()
        external_file = _write(outside / "keep.state", b"must remain untouched")
        archive = self.sdir / parity_resume.ARCHIVE_DIRNAME
        try:
            archive.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable in this environment")

        _write(self.sdir / "resume.state", b"old")
        time.sleep(0.01)
        _write(self.sdir / "new.state", b"new")
        before = sorted((path.name, path.read_bytes()) for path in outside.iterdir())
        parity_resume.collect_resume_state(
            self.game, {"state_retention": 1}, profiles={}, data_parent=self.data_parent,
        )

        self.assertEqual(
            sorted((path.name, path.read_bytes()) for path in outside.iterdir()), before,
        )
        self.assertEqual(external_file.read_bytes(), b"must remain untouched")
        self.assertEqual((self.sdir / "resume.state").read_bytes(), b"new")

    def test_collect_does_not_overwrite_external_meta_symlink(self):
        outside = self.data_parent / "outside-meta.json"
        outside.write_text("keep me", encoding="utf-8")
        meta_path = self.sdir / parity_resume.STATE_META
        try:
            meta_path.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable in this environment")
        _write(self.sdir / "new.state", b"new")
        self.assertIsNone(parity_resume.collect_resume_state(
            self.game, {}, profiles={}, data_parent=self.data_parent,
        ))
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me")

    def test_collect_preserves_both_states_when_archive_cannot_be_created(self):
        _write(self.sdir / "resume.state", b"old")
        archive = self.sdir / parity_resume.ARCHIVE_DIRNAME
        _write(archive, b"not a directory")
        time.sleep(0.01)
        _write(self.sdir / "new.state", b"new")
        meta = parity_resume.collect_resume_state(
            self.game, {"state_retention": 2}, profiles={}, data_parent=self.data_parent,
        )
        self.assertIsNotNone(meta)
        self.assertEqual(meta["file"], "new.state")
        self.assertEqual((self.sdir / "resume.state").read_bytes(), b"old")
        self.assertEqual((self.sdir / "new.state").read_bytes(), b"new")
        self.assertEqual(archive.read_bytes(), b"not a directory")

    def test_collect_disabled_or_no_files(self):
        self.assertIsNone(parity_resume.collect_resume_state(
            self.game, {"quick_resume_enabled": False}, profiles={}, data_parent=self.data_parent))
        self.assertIsNone(parity_resume.collect_resume_state(
            self.game, {}, profiles={}, data_parent=self.data_parent))
        none_game = _game(emulator_adapter_id="", emulator_id="", platform="Nope")
        self.assertIsNone(parity_resume.collect_resume_state(
            none_game, {}, profiles={}, data_parent=self.data_parent, which=lambda name: None))

    def test_collect_mame_slot_canonicalization(self):
        mame_adapter = dict(self.adapter)
        mame_adapter["adapter_id"] = "t1-mame"
        mame_adapter["state"] = {
            "kind": "adapter-cli",
            "template": ["-autosave", "-state_directory", "{state_dir}", "-state", "{state_name}"],
            "capture": ["-autosave", "-state_directory", "{state_dir}", "-state", "{state_name}"],
            "glob": ["**/*.sta"],
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-mame"] = mame_adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-mame", None)
        game = _game(emulator_adapter_id="t1-mame")
        sdir = parity_resume.state_dir_for(game, self.data_parent)
        _write(sdir / "pacman" / "openbox-session.sta", b"mame-state")
        meta = parity_resume.collect_resume_state(game, {}, profiles={}, data_parent=self.data_parent)
        self.assertEqual(meta["file"], "pacman/openbox-resume.sta")
        self.assertTrue((sdir / "pacman" / "openbox-resume.sta").is_file())
        self.assertFalse((sdir / "pacman" / "openbox-session.sta").exists())

    def test_scummvm_style_files_stay_in_place(self):
        scumm_adapter = dict(self.adapter)
        scumm_adapter["adapter_id"] = "t1-scumm"
        scumm_adapter["state"] = {
            "kind": "adapter-cli",
            "template": ["--savepath={state_dir}", "--save-slot=0"],
            "capture": ["--savepath={state_dir}"],
            "glob": ["*.s00"],
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-scumm"] = scumm_adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-scumm", None)
        game = _game(emulator_adapter_id="t1-scumm")
        sdir = parity_resume.state_dir_for(game, self.data_parent)
        _write(sdir / "sky.s00", b"scumm")
        meta = parity_resume.collect_resume_state(game, {}, profiles={}, data_parent=self.data_parent)
        self.assertEqual(meta["file"], "sky.s00")
        self.assertTrue((sdir / "sky.s00").is_file())


class StatusAndStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_parent = Path(self.tmp.name)
        self.adapter = {
            "adapter_id": "t1-status",
            "emulator_id": "fake.emu",
            "label": "Fake",
            "platform": "SNES",
            "extensions": ["sfc"],
            "native_exe": "/bin/true",
            "flatpak_app_id": "",
            "startup_args": ["{path}"],
            "executable_patterns": [],
            "state": {"kind": "adapter-cli", "template": ["--state={state_path}"], "capture": [], "glob": ["*"]},
        }
        registry = defs._registry()
        registry["by_adapter_id"]["t1-status"] = self.adapter
        self.addCleanup(registry["by_adapter_id"].pop, "t1-status", None)
        self.game = _game(emulator_adapter_id="t1-status")
        self.sdir = parity_resume.state_dir_for(self.game, self.data_parent)

    def _write_meta(self, version=None):
        _write(self.sdir / "resume.bin")
        meta = {
            "adapter_id": "t1-status",
            "emulator_version": version or parity_resume.emulator_fingerprint(self.adapter),
            "kind": "adapter-cli",
            "file": "resume.bin",
            "captured_at": "2026-09-12T00:00:00",
            "capture_id": "capture-test",
        }
        (self.sdir / "state.json").write_text(json.dumps(meta))
        return meta

    def test_status_available_fresh(self):
        self._write_meta()
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertTrue(status["capable"])
        self.assertTrue(status["available"])
        self.assertFalse(status["stale"])
        self.assertEqual(status["state"]["file"], "resume.bin")
        self.assertEqual(status["state"]["capture_id"], "capture-test")
        self.assertEqual(status["kind"], "adapter-cli")

    def test_status_no_state(self):
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertTrue(status["capable"])
        self.assertFalse(status["available"])
        self.assertIsNone(status["state"])

    def test_status_stale_on_version_or_adapter_drift(self):
        self._write_meta(version="old")
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertTrue(status["available"])
        self.assertTrue(status["stale"])
        self.assertTrue(status["adapter_changed"] is False)
        meta = json.loads((self.sdir / "state.json").read_text())
        meta["adapter_id"] = "other-adapter"
        (self.sdir / "state.json").write_text(json.dumps(meta))
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertTrue(status["stale"])
        self.assertTrue(status["adapter_changed"])

    def test_status_vanished_file_not_available(self):
        self._write_meta()
        (self.sdir / "resume.bin").unlink()
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertFalse(status["available"])
        self.assertFalse(status["stale"])

    def test_status_and_resume_reject_traversal_and_symlink_files(self):
        outside = self.data_parent / "outside.bin"
        outside.write_bytes(b"must remain untouched")
        meta = self._write_meta()
        meta["file"] = "../outside.bin"
        (self.sdir / "state.json").write_text(json.dumps(meta))
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertFalse(status["available"])
        with self.assertRaises(FileNotFoundError):
            parity_resume.inject_resume_args(
                self.game, ["/bin/true"], profiles={}, settings={}, resume=True,
                data_parent=self.data_parent,
            )

        link = self.sdir / "linked.bin"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable in this environment")
        meta["file"] = "linked.bin"
        (self.sdir / "state.json").write_text(json.dumps(meta))
        status = parity_resume.resume_status(self.game, {}, profiles={}, data_parent=self.data_parent)
        self.assertFalse(status["available"])
        self.assertTrue(parity_resume.discard_resume_state(self.game, self.data_parent))
        self.assertTrue(outside.is_file())

    def test_status_unsupported_and_disabled(self):
        game = _game(emulator_adapter_id="", emulator_id="", platform="Nope", path="/missing/rom.nes")
        status = parity_resume.resume_status(game, {}, profiles={}, data_parent=self.data_parent, which=lambda name: None)
        self.assertFalse(status["capable"])
        self.assertEqual(status["reason"], "missing-path")
        game2 = _game(emulator_adapter_id="", emulator_id="", platform="Nope")
        status = parity_resume.resume_status(game2, {}, profiles={}, data_parent=self.data_parent, which=lambda name: None)
        self.assertFalse(status["capable"])
        self.assertEqual(status["reason"], "unsupported-adapter")
        status = parity_resume.resume_status(
            self.game, {"quick_resume_enabled": False}, profiles={}, data_parent=self.data_parent)
        self.assertTrue(status["capable"])
        self.assertFalse(status["enabled"])

    def test_discard_removes_state_and_meta(self):
        self._write_meta()
        self.assertTrue(parity_resume.discard_resume_state(self.game, self.data_parent))
        self.assertFalse((self.sdir / "state.json").exists())
        self.assertFalse((self.sdir / "resume.bin").exists())
        self.assertFalse(parity_resume.discard_resume_state(self.game, self.data_parent))


class LaunchPipelineTests(unittest.TestCase):
    """launch.py call-site threading (start path + finish_session capture)."""

    def setUp(self):
        from pkg.state.registry import PENDING_LAUNCHES, PROCESSES, RUNNING
        RUNNING.clear()
        PROCESSES.clear()
        PENDING_LAUNCHES.clear()
        self.addCleanup(RUNNING.clear)
        self.addCleanup(PROCESSES.clear)
        self.addCleanup(PENDING_LAUNCHES.clear)
        self.state = {
            "games": [{"game_id": "g1", "name": "Game", "path": "/bin/true"}],
            "profiles": {},
            "settings": {},
            "history": [],
            "active_sessions": [],
        }
        self.process = MagicMock(pid=4321)
        self.process.poll.return_value = None

    def test_start_game_threads_resume_flag(self):
        spy = MagicMock(side_effect=lambda game, args, **kwargs: list(args))
        _deps.register("inject_resume_args", spy)
        self.addCleanup(_deps._REGISTRY.pop, "inject_resume_args", None)
        with patch.multiple(
            webapp_state,
            load_state=MagicMock(return_value=self.state),
            _resolve_start_game=MagicMock(return_value=(self.state["games"][0], 0)),
            _start_launch_command=MagicMock(return_value=(["/bin/true"], "/tmp")),
            _apply_start_plugins=MagicMock(side_effect=lambda game, args, cwd: (args, cwd)),
            _validate_start_command=MagicMock(),
            apply_perf_profile=MagicMock(return_value=MagicMock(restore=MagicMock())),
            update_state=MagicMock(side_effect=lambda mutator: mutator(self.state)),
            _annotate_gamescope_start=MagicMock(),
            _publish_start_events=MagicMock(),
            finish_session=MagicMock(),
            subprocess=MagicMock(Popen=MagicMock(return_value=self.process)),
        ), patch("pkg.state.launch.threading.Thread"):
            entry = webapp_state.start_game(0, resume=True)
        self.assertTrue(entry["launch_id"])
        self.assertTrue(entry["resumed_from_state"])
        self.assertEqual(spy.call_args.kwargs.get("resume"), True)

    def test_start_game_clean_by_default(self):
        spy = MagicMock(side_effect=lambda game, args, **kwargs: list(args))
        _deps.register("inject_resume_args", spy)
        self.addCleanup(_deps._REGISTRY.pop, "inject_resume_args", None)
        with patch.multiple(
            webapp_state,
            load_state=MagicMock(return_value=self.state),
            _resolve_start_game=MagicMock(return_value=(self.state["games"][0], 0)),
            _start_launch_command=MagicMock(return_value=(["/bin/true"], "/tmp")),
            _apply_start_plugins=MagicMock(side_effect=lambda game, args, cwd: (args, cwd)),
            _validate_start_command=MagicMock(),
            apply_perf_profile=MagicMock(return_value=MagicMock(restore=MagicMock())),
            update_state=MagicMock(side_effect=lambda mutator: mutator(self.state)),
            _annotate_gamescope_start=MagicMock(),
            _publish_start_events=MagicMock(),
            finish_session=MagicMock(),
            subprocess=MagicMock(Popen=MagicMock(return_value=self.process)),
        ), patch("pkg.state.launch.threading.Thread"):
            entry = webapp_state.start_game(0)
        self.assertFalse(entry["resumed_from_state"])
        self.assertEqual(spy.call_args.kwargs.get("resume"), False)

    def test_finish_session_invokes_collect(self):
        spy = MagicMock(return_value=None)
        _deps.register("collect_resume_state", spy)
        self.addCleanup(_deps._REGISTRY.pop, "collect_resume_state", None)
        # webapp_state re-exports only part of the launch deps; the remaining
        # _ns fallbacks resolve through the _deps registry instead.
        for name in ("backup_saves", "enforce_backup_limit",
                     "auto_attach_obs_recording", "close_store_client"):
            _deps.register(name, MagicMock())
            self.addCleanup(_deps._REGISTRY.pop, name, None)
        started = datetime.now()
        lease = MagicMock()
        with patch.multiple(
            webapp_state,
            load_state=MagicMock(return_value=self.state),
            update_state=MagicMock(side_effect=lambda mutator: mutator(self.state)),
            wait_for_exit=MagicMock(return_value=0),
            run_plugins=MagicMock(),
            session_event=MagicMock(),
            _publish_session_event=MagicMock(),
        ):
            webapp_state.RUNNING["L9"] = {"stable_game_id": "g1", "game": "Game"}
            webapp_state.PROCESSES["L9"] = self.process
            self.process.poll.return_value = 0
            webapp_state.finish_session("L9", 0, started, self.process, lease)
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[0]["name"], "Game")


class DeeplinkTests(unittest.TestCase):
    def test_parse_resume_moment_clip(self):
        from pkg.parity import parity_deeplinks
        self.assertEqual(parity_deeplinks.parse_uri("openbox://resume/g1"), {"action": "resume", "id": "g1"})
        self.assertEqual(parity_deeplinks.parse_uri("openbox://moment/g2"), {"action": "moment", "id": "g2"})
        self.assertEqual(parity_deeplinks.parse_uri("openbox://clip/g3"), {"action": "clip", "id": "g3"})
        self.assertEqual(parity_deeplinks.parse_uri("openbox://resume"), {"action": "resume", "id": ""})
        self.assertEqual(parity_deeplinks.parse_uri("openbox://bogus/x"), {"action": "bogus"})

    def test_dispatch_resume_posts_to_v2_route(self):
        from pkg.parity import parity_deeplinks
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "server.token").write_text("tok")
            with patch.object(parity_deeplinks, "api_request", side_effect=lambda *a, **k: calls.append(a) or {"ok": True}):
                rc = parity_deeplinks.dispatch_uri("openbox://resume/g1", tmp, port=8080)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][3], "/api/v2/resume")
        self.assertEqual(calls[0][4], "POST")
        self.assertEqual(calls[0][5], {"game_id": "g1"})

    def test_dispatch_resume_numeric_id(self):
        from pkg.parity import parity_deeplinks
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "server.token").write_text("tok")
            with patch.object(parity_deeplinks, "api_request", side_effect=lambda *a, **k: calls.append(a) or {"ok": True}):
                rc = parity_deeplinks.dispatch_uri("openbox://resume/7", tmp, port=8080)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][5], {"id": 7})

    def test_dispatch_moment_clip_open_shell_url(self):
        from pkg.parity import parity_deeplinks
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "server.token").write_text("tok")
            with patch("webbrowser.open") as opened:
                rc = parity_deeplinks.dispatch_uri("openbox://moment/g9", tmp, port=8080, open_browser=True)
        self.assertEqual(rc, 0)
        url = opened.call_args.args[0]
        self.assertIn("deeplink=moment", url)
        self.assertIn("id=g9", url)

    def test_build_launch_url_new_actions(self):
        from pkg.parity import parity_deeplinks
        self.assertIn("deeplink=resume", parity_deeplinks.build_launch_url("http://h/", "resume", id="g1"))
        self.assertIn("deeplink=moment", parity_deeplinks.build_launch_url("http://h/", "moment", id="g1"))
        self.assertIn("deeplink=clip", parity_deeplinks.build_launch_url("http://h/", "clip", id="g1"))


if __name__ == "__main__":
    unittest.main()
