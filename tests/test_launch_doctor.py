"""Tests for Launch Doctor preflight checks and v2 routes."""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, GameNotFound  # noqa: E402
from handlers.launch import LaunchHandlers  # noqa: E402
from pkg.parity.parity_launch_doctor import (  # noqa: E402
    PRECEDENCE_NUMBERS,
    preflight_batch,
    preflight_single,
    run_preflight_checks,
)


class DummyLaunchHandler(LaunchHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


class LaunchDoctorCoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.data_dir)
        self.rom = self.data_dir / "game.nes"
        self.rom.write_bytes(b"NES")

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_missing_rom_blocked_with_path_missing(self):
        game = {
            "name": "Missing",
            "path": str(self.data_dir / "missing.nes"),
            "platform": "NES",
            "emulator_adapter_id": "retroarch-nes",
        }
        result = preflight_single(
            {"game_id": None, "candidate": None},
            game=game,
            profiles={},
            data_dir=str(self.data_dir),
            which=lambda _: None,
        )
        self.assertEqual(result["status"], "blocked")
        codes = [item["code"] for item in result["checks"]]
        self.assertIn("PATH_MISSING", codes)

    def test_missing_core_blocked_with_remediation(self):
        game = {
            "name": "Game",
            "path": str(self.rom),
            "platform": "NES",
            "emulator_adapter_id": "retroarch-nes",
        }
        with mock.patch("pkg.parity.parity_launch_doctor.shutil.which", return_value="/usr/bin/retroarch"):
            result = preflight_single(
                {"game_id": None, "candidate": None},
                game=game,
                profiles={},
                data_dir=str(self.data_dir),
                which=lambda name: "/usr/bin/retroarch" if name == "retroarch" else None,
            )
        self.assertEqual(result["status"], "blocked")
        core_check = next(item for item in result["checks"] if item["code"] == "RETROARCH_CORE_MISSING")
        self.assertEqual(core_check["severity"], "error")
        self.assertTrue(any(item["id"] == "choose_adapter" for item in core_check["remediations"]))

    def test_valid_native_ready(self):
        game = {
            "name": "Game",
            "path": str(self.rom),
            "platform": "NES",
            "emulator_adapter_id": "retroarch-nes",
            "save_paths": [str(self.data_dir / "saves")],
            "screenshots": [str(self.data_dir / "shot.png")],
            "documents": [{"name": "manual", "path": str(self.data_dir / "manual.pdf")}],
        }
        core = self.data_dir / "fceumm_libretro.so"
        core.write_bytes(b"core")

        def which(name):
            if name == "retroarch":
                return "/usr/bin/retroarch"
            return None

        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", side_effect=which):
            with mock.patch("pkg.parity.parity_launch_doctor._retroarch_core_missing", return_value=None):
                result = preflight_single(
                    {"game_id": None, "candidate": None},
                    game=game,
                    profiles={},
                    data_dir=str(self.data_dir),
                    which=which,
                )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["resolved"]["precedence"], PRECEDENCE_NUMBERS["game_adapter"])
        self.assertTrue(result["resolved"]["argv_preview"])

    def test_identity_xor_validation(self):
        with self.assertRaises(BadRequest):
            preflight_single({"game_id": "g1", "candidate": {"candidate_id": "c1"}}, state={"games": []}, profiles={})
        with self.assertRaises(BadRequest):
            preflight_single({"game_id": None, "candidate": None}, state={"games": []}, profiles={})

    def test_candidate_identity_sets_candidate_id(self):
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir(parents=True)
        preview_id = "preview-1"
        (preview_dir / f"{preview_id}.json").write_text(
            json.dumps({"preview_id": preview_id, "expires_at": "2099-01-01T00:00:00"}),
            encoding="utf-8",
        )
        core = self.data_dir / "fceumm_libretro.so"
        core.write_bytes(b"core")
        candidate = {
            "candidate_id": "cand-1",
            "preview_id": preview_id,
            "path": str(self.rom),
            "platform": "NES",
            "emulator_id": "org.libretro.RetroArch",
            "adapter_id": "retroarch-nes",
            "archive_member": None,
        }

        def which(name):
            if name == "retroarch":
                return "/usr/bin/retroarch"
            return None

        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", side_effect=which):
            result = preflight_single(
                {"game_id": None, "candidate": candidate},
                state={"games": []},
                profiles={},
                data_dir=str(self.data_dir),
                which=which,
            )
        self.assertEqual(result["candidate_id"], "cand-1")
        self.assertIsNone(result["game_id"])
        self.assertIn("resolved", result)
        self.assertIn("argv_preview", result["resolved"])

    def test_unknown_adapter_blocked(self):
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir(parents=True)
        preview_id = "preview-2"
        (preview_dir / f"{preview_id}.json").write_text(
            json.dumps({"preview_id": preview_id, "expires_at": "2099-01-01T00:00:00"}),
            encoding="utf-8",
        )
        candidate = {
            "candidate_id": "cand-2",
            "preview_id": preview_id,
            "path": str(self.rom),
            "platform": "NES",
            "emulator_id": None,
            "adapter_id": "no-such-adapter",
            "archive_member": None,
        }
        result = preflight_single(
            {"game_id": None, "candidate": candidate},
            state={"games": []},
            profiles={},
            data_dir=str(self.data_dir),
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn("ADAPTER_UNKNOWN", [item["code"] for item in result["checks"]])

    def test_batch_totals_and_by_platform(self):
        games = [
            {
                "name": "Ready",
                "path": str(self.rom),
                "platform": "NES",
                "emulator_adapter_id": "retroarch-nes",
                "save_paths": [str(self.data_dir / "saves")],
                "screenshots": [str(self.data_dir / "shot.png")],
                "documents": [{"name": "manual", "path": str(self.data_dir / "manual.pdf")}],
            },
            {
                "name": "Missing",
                "path": str(self.data_dir / "gone.nes"),
                "platform": "NES",
            },
        ]
        core = self.data_dir / "fceumm_libretro.so"
        core.write_bytes(b"core")

        def which(name):
            if name == "retroarch":
                return "/usr/bin/retroarch"
            return None

        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", side_effect=which):
            with mock.patch("pkg.parity.parity_launch_doctor._retroarch_core_missing", return_value=None):
                payload = preflight_batch(
                    {
                        "items": [
                            {"game_id": None, "candidate": None},
                            {"game_id": None, "candidate": None},
                        ],
                        "fail_on_blocked": False,
                    },
                    games=games,
                    profiles={},
                    data_dir=str(self.data_dir),
                    which=which,
                )
        self.assertEqual(payload["totals"]["ready"], 1)
        self.assertEqual(payload["totals"]["blocked"], 1)
        self.assertEqual(len(payload["results"]), 2)
        self.assertTrue(payload["by_platform"])
        for result in payload["results"]:
            for key in ("status", "game_id", "candidate_id", "resolved", "checks"):
                self.assertIn(key, result)
            for key in ("emulator_id", "adapter_id", "argv_preview", "cwd", "precedence"):
                self.assertIn(key, result["resolved"])

    def test_empty_batch_raises(self):
        with self.assertRaises(BadRequest):
            preflight_batch({"items": []}, state={"games": []}, profiles={})

    def test_preview_expired_raises(self):
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir(parents=True)
        preview_id = "preview-old"
        (preview_dir / f"{preview_id}.json").write_text(
            json.dumps({"preview_id": preview_id, "expires_at": "2000-01-01T00:00:00"}),
            encoding="utf-8",
        )
        candidate = {
            "candidate_id": "cand-old",
            "preview_id": preview_id,
            "path": str(self.rom),
            "platform": "NES",
            "emulator_id": None,
            "adapter_id": "retroarch-nes",
            "archive_member": None,
        }
        with self.assertRaises(BadRequest) as ctx:
            preflight_single(
                {"game_id": None, "candidate": candidate},
                state={"games": []},
                profiles={},
                data_dir=str(self.data_dir),
            )
        self.assertEqual(ctx.exception.code, "PREVIEW_EXPIRED")

    def test_archive_member_invalid(self):
        archive = self.data_dir / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("readme.txt", "nope")
        game = {
            "name": "Zip",
            "path": str(archive),
            "platform": "NES",
            "archive_member": "missing.nes",
        }
        checks = run_preflight_checks(game, {}, str(self.data_dir))
        self.assertIn("ARCHIVE_MEMBER_INVALID", [item["code"] for item in checks])


class LaunchDoctorHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = self.tempdir.name
        from openbox import save_state

        self.rom = Path(self.tempdir.name) / "game.nes"
        self.rom.write_bytes(b"NES")
        save_state({
            "games": [{
                "name": "Library Game",
                "path": str(self.rom),
                "platform": "NES",
                "game_id": "game-0123456789abcdef01234567-1",
                "emulator_adapter_id": "retroarch-nes",
            }],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        self.handler = DummyLaunchHandler()

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_single_preflight_game_id(self):
        core = Path(self.tempdir.name) / "fceumm_libretro.so"
        core.write_bytes(b"core")

        def which(name):
            if name == "retroarch":
                return "/usr/bin/retroarch"
            return None

        with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", side_effect=which):
            with mock.patch("pkg.parity.parity_launch_doctor._retroarch_core_missing", return_value=None):
                self.handler.launch_preflight({
                    "game_id": "game-0123456789abcdef01234567-1",
                    "candidate": None,
                })
        status, payload, _kwargs = self.handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["game_id"], "game-0123456789abcdef01234567-1")
        self.assertIsNone(payload["candidate_id"])
        self.assertIn("precedence", payload["resolved"])

    def test_fail_on_blocked_returns_409(self):
        self.handler.launch_preflight({
            "game_id": "game-0123456789abcdef01234567-1",
            "candidate": None,
            "fail_on_blocked": True,
        }, request_id="req-abc")
        status, payload, _kwargs = self.handler.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "LAUNCH_PREFLIGHT_BLOCKED")
        self.assertEqual(payload["request_id"], "req-abc")

    def test_unknown_game_raises_not_found(self):
        with self.assertRaises(GameNotFound):
            self.handler.launch_preflight({
                "game_id": "game-missing",
                "candidate": None,
            })

    def test_invalid_identity_400(self):
        with self.assertRaises(BadRequest):
            self.handler.launch_preflight({"game_id": "g1", "candidate": {"candidate_id": "c1"}})

    def test_batch_endpoint(self):
        status_handler = DummyLaunchHandler()
        status_handler.launch_preflight_batch({
            "items": [{
                "game_id": "game-0123456789abcdef01234567-1",
                "candidate": None,
            }],
        })
        status, payload, _kwargs = status_handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("totals", payload)
        self.assertIn("by_platform", payload)
        self.assertEqual(len(payload["results"]), 1)

    def test_route_methods_delegate(self):
        handler = DummyLaunchHandler()
        with mock.patch.object(handler, "launch_preflight") as launch_mock:
            handler._api_post_api_v2_launch_preflight({"game_id": "x"})
            launch_mock.assert_called_once_with({"game_id": "x"})
        with mock.patch.object(handler, "launch_preflight_batch") as batch_mock:
            handler._api_post_api_v2_launch_preflight_batch({"items": []})
            batch_mock.assert_called_once_with({"items": []})

    def test_batch_fail_on_blocked_409(self):
        handler = DummyLaunchHandler()
        handler.launch_preflight_batch({
            "items": [{
                "game_id": "game-0123456789abcdef01234567-1",
                "candidate": None,
            }],
            "fail_on_blocked": True,
        }, request_id="batch-req")
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "LAUNCH_PREFLIGHT_BLOCKED")
        self.assertEqual(payload["request_id"], "batch-req")


class LaunchDoctorCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_helper_paths(self):
        from pkg.parity import parity_launch_doctor as doctor

        core = self.data_dir / "core.so"
        core.write_bytes(b"core")
        self.assertIsNone(doctor._retroarch_core_missing({"startup_args": ["-L", str(core)]}))
        self.assertEqual(doctor._retroarch_core_missing({"startup_args": ["-L", "/no/such/core.so"]}), "/no/such/core.so")
        self.assertTrue(doctor._flatpak_fs_allowed("org.test.App", str(self.data_dir / "game.nes"), lambda _: "/usr/bin/flatpak", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "filesystem=home"})()))
        self.assertFalse(doctor._flatpak_fs_allowed(
            "org.test.App",
            "/srv/outside/game.nes",
            lambda _: "/usr/bin/flatpak",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "filesystem=/srv/allowed"})(),
        ))
        self.assertTrue(doctor._flatpak_installed("org.test.App", lambda _: "/usr/bin/flatpak", lambda *a, **k: type("R", (), {"returncode": 0})()))

    def test_preview_not_found_and_invalid_payload(self):
        from pkg.parity.parity_launch_doctor import preflight_batch, preflight_single, validate_preview

        with self.assertRaises(BadRequest) as ctx:
            validate_preview("", str(self.data_dir))
        self.assertEqual(ctx.exception.code, "PREVIEW_NOT_FOUND")
        with self.assertRaises(BadRequest):
            preflight_single("bad", state={"games": []}, profiles={})
        with self.assertRaises(BadRequest):
            preflight_batch({"items": "nope"}, state={"games": []}, profiles={})

    def test_path_and_platform_checks(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        folder = self.data_dir / "folder"
        folder.mkdir()
        checks = run_preflight_checks({"path": str(folder), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("PATH_WRONG_TYPE", [item["code"] for item in checks])

    def test_emulator_and_template_checks(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        checks = run_preflight_checks({
            "path": str(rom),
            "platform": "NES",
            "emulator_id": "missing-emulator",
        }, {}, str(self.data_dir), which=lambda _: None)
        codes = [item["code"] for item in checks]
        self.assertIn("EMULATOR_UNKNOWN", codes)

        with mock.patch("pkg.parity.parity_launch_doctor.resolve_launch", side_effect=ValueError("bad template")):
            checks = run_preflight_checks({
                "path": str(rom),
                "platform": "NES",
                "launch": "{broken}",
            }, {}, str(self.data_dir))
        self.assertIn("TEMPLATE_INVALID", [item["code"] for item in checks])

    def test_platform_unknown_on_valid_file(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        checks = run_preflight_checks({"path": str(rom), "platform": ""}, {}, str(self.data_dir))
        self.assertIn("PLATFORM_UNKNOWN", [item["code"] for item in checks])

    def test_install_and_bios_checks(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        rom = self.data_dir / "game.bin"
        rom.write_bytes(b"PSX")
        checks = run_preflight_checks({
            "path": str(rom),
            "platform": "PlayStation",
            "emulator_adapter_id": "retroarch-nes",
        }, {}, str(self.data_dir), which=lambda _: None)
        codes = [item["code"] for item in checks]
        self.assertIn("NATIVE_EXE_MISSING", codes)
        with mock.patch("pkg.parity.parity_launch_doctor.detect_dependencies", return_value={"missing": [{"name": "firmware pack"}]}):
            checks = run_preflight_checks({
                "path": str(rom),
                "platform": "PlayStation",
                "emulator_adapter_id": "retroarch-nes",
            }, {}, str(self.data_dir), which=lambda name: "/usr/bin/retroarch" if name == "retroarch" else None)
        codes = [item["code"] for item in checks]
        self.assertIn("BIOS_MISSING", codes)
        self.assertIn("FIRMWARE_MISSING", codes)

    def test_archive_invalid_zip(self):
        import zipfile
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        archive = self.data_dir / "bad.zip"
        archive.write_bytes(b"not-a-zip")
        checks = run_preflight_checks({"path": str(archive), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("ARCHIVE_INVALID", [item["code"] for item in checks])

        empty = self.data_dir / "empty.zip"
        with zipfile.ZipFile(empty, "w"):
            pass
        checks = run_preflight_checks({"path": str(empty), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("ARCHIVE_INVALID", [item["code"] for item in checks])

    def test_flatpak_denied_and_batch_identity(self):
        from openbox import save_state
        from pkg.parity.parity_launch_doctor import preflight_batch, preflight_single

        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir()
        preview_id = "preview-batch"
        (preview_dir / f"{preview_id}.json").write_text(
            json.dumps({"preview_id": preview_id, "expires_at": "2099-01-01T00:00:00"}),
            encoding="utf-8",
        )
        save_state({
            "games": [{
                "name": "G",
                "path": str(rom),
                "platform": "NES",
                "game_id": "game-0123456789abcdef01234567-9",
            }],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        denied = mock.Mock(return_value=type("R", (), {"returncode": 0, "stdout": ""})())

        def which(name):
            if name == "flatpak":
                return "/usr/bin/flatpak"
            return None

        with mock.patch("pkg.parity.parity_launch_doctor._retroarch_core_missing", return_value=None):
            with mock.patch("pkg.parity.parity_launch_doctor._flatpak_fs_allowed", return_value=False):
                with mock.patch("pkg.parity.parity_launch_doctor._flatpak_installed", return_value=True):
                    result = preflight_single({
                        "game_id": None,
                        "candidate": {
                            "candidate_id": "c1",
                            "preview_id": preview_id,
                            "path": str(rom),
                            "platform": "NES",
                            "emulator_id": "org.libretro.RetroArch",
                            "adapter_id": "retroarch-nes",
                            "archive_member": None,
                        },
                    }, data_dir=str(self.data_dir), which=which, run=denied)
        self.assertIn("FLATPAK_FS_DENIED", [item["code"] for item in result["checks"]])

        payload = preflight_batch({
            "items": [{"game_id": "game-0123456789abcdef01234567-9", "candidate": None}],
        }, which=which, run=denied)
        self.assertEqual(len(payload["results"]), 1)


class LaunchDoctorEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_candidate_missing_fields_and_preview_corrupt(self):
        from pkg.parity.parity_launch_doctor import preflight_single, validate_preview

        with self.assertRaises(BadRequest):
            preflight_single({
                "game_id": None,
                "candidate": {"candidate_id": "c1", "preview_id": "p1", "path": "/x"},
            }, state={"games": []}, profiles={})
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir()
        (preview_dir / "bad.json").write_text("{", encoding="utf-8")
        with self.assertRaises(BadRequest) as ctx:
            validate_preview("bad", str(self.data_dir))
        self.assertEqual(ctx.exception.code, "PREVIEW_NOT_FOUND")

    def test_empty_path_and_warning_status(self):
        from pkg.parity.parity_launch_doctor import _derive_status, run_preflight_checks

        self.assertEqual(_derive_status([{"severity": "warning"}]), "warning")
        checks = run_preflight_checks({"path": ""}, {}, str(self.data_dir))
        self.assertIn("PATH_MISSING", [item["code"] for item in checks])

    def test_flatpak_fs_grant_paths(self):
        from pkg.parity import parity_launch_doctor as doctor

        def run_ok(*_args, **_kwargs):
            return type("R", (), {"returncode": 0, "stdout": "filesystem=host"})()

        self.assertTrue(doctor._flatpak_fs_allowed("app", "/any/path", lambda _: "flatpak", run_ok))

        def run_fail(*_args, **_kwargs):
            return type("R", (), {"returncode": 1, "stdout": ""})()

        self.assertTrue(doctor._flatpak_fs_allowed("app", "/any/path", lambda _: None, run_fail))
        allowed = str(self.data_dir)

        def run_allowed(*_args, **_kwargs):
            return type("R", (), {"returncode": 0, "stdout": f"filesystem={allowed}"})()
        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        self.assertTrue(doctor._flatpak_fs_allowed("app", str(rom), lambda _: "flatpak", run_allowed))

    def test_archive_member_listing_and_cwd_invalid(self):
        import zipfile
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        archive = self.data_dir / "game.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("game.nes", b"NES")
        checks = run_preflight_checks({
            "path": str(archive),
            "platform": "NES",
            "archive_member": "missing.nes",
        }, {}, str(self.data_dir))
        self.assertIn("ARCHIVE_MEMBER_INVALID", [item["code"] for item in checks])

        rom = self.data_dir / "direct.nes"
        rom.write_bytes(b"NES")
        with mock.patch("pkg.parity.parity_launch_doctor.resolve_launch", return_value={"args": ["ok"], "cwd": "/no/such", "precedence": "direct_exe"}):
            checks = run_preflight_checks({"path": str(rom), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("CWD_INVALID", [item["code"] for item in checks])

    def test_batch_item_platform_resolution(self):
        from openbox import save_state
        from pkg.parity.parity_launch_doctor import preflight_batch

        rom = self.data_dir / "game.nes"
        rom.write_bytes(b"NES")
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir()
        (preview_dir / "preview-x.json").write_text(
            json.dumps({"preview_id": "preview-x", "expires_at": "2099-01-01T00:00:00"}),
            encoding="utf-8",
        )
        save_state({
            "games": [{
                "name": "G",
                "path": str(rom),
                "platform": "SNES",
                "game_id": "game-0123456789abcdef01234567-8",
            }],
            "profiles": {},
            "history": [],
            "settings": {},
            "playlists": [],
        })
        payload = preflight_batch({
            "items": [
                {"game_id": "game-0123456789abcdef01234567-8", "candidate": None},
                {
                    "game_id": None,
                    "candidate": {
                        "candidate_id": "cand-x",
                        "preview_id": "preview-x",
                        "path": str(rom),
                        "platform": "NES",
                        "emulator_id": None,
                        "adapter_id": "retroarch-nes",
                        "archive_member": None,
                    },
                },
            ],
        }, data_dir=str(self.data_dir))
        self.assertEqual(len(payload["by_platform"]), 2)

    def test_remaining_branches(self):
        from pkg.parity import parity_launch_doctor as doctor

        with self.assertRaises(BadRequest):
            doctor.validate_preview("missing-preview", str(self.data_dir))
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir()
        (preview_dir / "weird.json").write_text(
            json.dumps({"preview_id": "weird", "expires_at": "not-a-date"}),
            encoding="utf-8",
        )
        doctor.validate_preview("weird", str(self.data_dir))

        home_rom = Path.home() / "openbox-f13-test.nes"
        home_rom.write_bytes(b"NES")
        try:
            def run_shared(*_args, **_kwargs):
                return type("R", (), {"returncode": 0, "stdout": "shared=ipc"})()

            self.assertFalse(doctor._flatpak_fs_allowed("app", str(home_rom), lambda _: "flatpak", run_shared))

            def run_ro(*_args, **_kwargs):
                return type("R", (), {"returncode": 0, "stdout": f"filesystem={self.data_dir}:ro"})()

            rom = self.data_dir / "inside.nes"
            rom.write_bytes(b"NES")
            self.assertTrue(doctor._flatpak_fs_allowed("app", str(rom), lambda _: "flatpak", run_ro))

            def run_broken(*_args, **_kwargs):
                return type("R", (), {"returncode": 0, "stdout": "filesystem=::broken"})()

            self.assertFalse(doctor._flatpak_fs_allowed("app", str(rom), lambda _: "flatpak", run_broken))
        finally:
            home_rom.unlink(missing_ok=True)

        rom = self.data_dir / "argv.nes"
        rom.write_bytes(b"NES")
        with mock.patch("pkg.parity.parity_launch_doctor.resolve_launch", side_effect=FileNotFoundError("path no longer exists")):
            checks = doctor.run_preflight_checks({"path": str(rom), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("PATH_MISSING", [item["code"] for item in checks])
        with mock.patch("pkg.parity.parity_launch_doctor.resolve_launch", return_value={"args": ["{path}"], "cwd": str(self.data_dir), "precedence": "direct_exe"}):
            checks = doctor.run_preflight_checks({"path": str(rom), "platform": "NES"}, {}, str(self.data_dir))
        self.assertIn("ARGV_INVALID", [item["code"] for item in checks])

        with self.assertRaises(BadRequest):
            doctor.preflight_batch("bad", state={"games": []}, profiles={})
        payload = doctor.preflight_batch(
            {"items": ["not-a-dict"]},
            games=[{"name": "G", "path": str(rom), "platform": "NES"}],
            profiles={},
            data_dir=str(self.data_dir),
        )
        self.assertEqual(len(payload["results"]), 1)


class TestFixActionCoverage(unittest.TestCase):
    """F4: every blocking check must have fix_action — LAUNCH_PREFLIGHT_BLOCKED/EMULATOR_REQUIRED/AMBIGUOUS_PLATFORM actionable."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.tempdir.name)
        self.rom = self.data_dir / "game.nes"
        self.rom.write_bytes(b"NES")

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def _assert_every_error_has_fix_action(self, checks):
        for c in checks:
            if c.get("severity") == "error":
                self.assertIn("fix_action", c, msg=f"missing fix_action for {c.get('code')}")
                fix = c["fix_action"]
                self.assertIn("kind", fix)
                self.assertIn("label", fix)
                self.assertIn("payload", fix)
                self.assertIn(fix["kind"], {"flatpak_install", "reveal_bios_path", "pick_core", "explain_token"})

    def test_every_blocking_check_has_fix_action(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        games = [
            {"path": "", "platform": "NES"},  # PATH_MISSING
            {"path": str(self.data_dir / "folder"), "platform": "NES"},  # PATH_WRONG_TYPE after mkdir
            {"path": str(self.rom), "platform": "NES", "emulator_adapter_id": "no-such"},  # ADAPTER_UNKNOWN
            {"path": str(self.rom), "platform": "NES", "emulator_id": "missing-emulator"},  # EMULATOR_UNKNOWN
            {"path": str(self.rom), "platform": "NES", "launch": "{unknown_token}"},  # TEMPLATE_INVALID
            {"path": str(self.rom), "platform": "PlayStation", "emulator_adapter_id": "retroarch-nes"},  # NATIVE_EXE_MISSING
        ]
        (self.data_dir / "folder").mkdir(exist_ok=True)
        for game in games:
            checks = run_preflight_checks(game, {}, str(self.data_dir), which=lambda _: None)
            # filter at least one error per case except maybe not; ensure we actually get errors
            errors = [c for c in checks if c["severity"] == "error"]
            if errors:
                self._assert_every_error_has_fix_action(checks)

    def test_ambiguous_iso_returns_picker_chips(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        iso = self.data_dir / "game.iso"
        iso.write_bytes(b"ISO")
        # No platform, no adapter, iso is ambiguous across 4 adapters
        game = {"path": str(iso), "platform": "", "name": "IsoGame"}
        checks = run_preflight_checks(game, {}, str(self.data_dir), which=lambda _: None)
        codes = [c["code"] for c in checks]
        self.assertIn("AMBIGUOUS_PLATFORM", codes)
        amb = next(c for c in checks if c["code"] == "AMBIGUOUS_PLATFORM")
        self.assertEqual(amb["severity"], "error")
        self.assertIn("fix_action", amb)
        self.assertEqual(amb["fix_action"]["kind"], "pick_core")
        payload = amb["fix_action"]["payload"]
        self.assertIn("platforms", payload)
        self.assertGreaterEqual(len(payload["platforms"]), 2)
        # chip UI should be able to render these platforms
        for plat in payload["platforms"]:
            self.assertIsInstance(plat, str)

    def test_emulator_required_with_pick_core(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        # Use .nes with platform NES but no installed emulator, should get EMULATOR_REQUIRED or NATIVE_EXE_MISSING
        game = {"path": str(self.rom), "platform": "NES", "name": "NesGame"}
        checks = run_preflight_checks(game, {}, str(self.data_dir), which=lambda _: None)
        codes = [c["code"] for c in checks]
        # Either EMULATOR_REQUIRED or NATIVE_EXE_MISSING qualifies as actionable missing emulator
        self.assertTrue(any(code in codes for code in ("EMULATOR_REQUIRED", "NATIVE_EXE_MISSING", "FLATPAK_NOT_INSTALLED")))
        for c in checks:
            if c["code"] in ("EMULATOR_REQUIRED", "NATIVE_EXE_MISSING", "FLATPAK_NOT_INSTALLED"):
                self.assertEqual(c["fix_action"]["kind"], "pick_core" if c["code"] == "EMULATOR_REQUIRED" else "flatpak_install")

    def test_bios_missing_reveal_path(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        rom = self.data_dir / "game.bin"
        rom.write_bytes(b"PSX")
        # Mock BIOS missing for DuckStation
        with mock.patch("pkg.parity.parity_launch_doctor.detect_dependencies", return_value={"missing": [{"name": "PSX BIOS (scph1001.bin)", "path": "/home/test/.local/share/duckstation/bios/scph1001.bin"}], "required": [{"name": "PSX BIOS (scph1001.bin)", "path": "/home/test/.local/share/duckstation/bios/scph1001.bin"}]}):
            def which(name):
                if name == "duckstation-qt":
                    return "/usr/bin/duckstation-qt"
                return None
            with mock.patch("pkg.parity.parity_emulator_defs.shutil.which", side_effect=which):
                checks = run_preflight_checks({"path": str(rom), "platform": "PlayStation", "emulator_adapter_id": "duckstation-psx"}, {}, str(self.data_dir), which=which)
        # BIOS_MISSING is warning, but should have fix_action reveal_bios_path
        bios_checks = [c for c in checks if c["code"] == "BIOS_MISSING"]
        if bios_checks:
            self.assertEqual(bios_checks[0]["fix_action"]["kind"], "reveal_bios_path")
            self.assertIn("path", bios_checks[0]["fix_action"]["payload"])

    def test_token_invalid_explain(self):
        from pkg.parity.parity_launch_doctor import run_preflight_checks

        checks = run_preflight_checks({"path": str(self.rom), "platform": "NES", "launch": "emu {unknown_token} {path}"}, {}, str(self.data_dir), which=lambda _: None)
        self.assertIn("TEMPLATE_INVALID", [c["code"] for c in checks])
        ti = next(c for c in checks if c["code"] == "TEMPLATE_INVALID")
        self.assertEqual(ti["fix_action"]["kind"], "explain_token")
        self.assertIn("invalid_tokens", ti["fix_action"]["payload"])

    def test_batch_ambiguous_iso_actionable(self):
        from openbox import save_state
        from pkg.parity.parity_launch_doctor import preflight_batch

        iso = self.data_dir / "ambig.iso"
        iso.write_bytes(b"ISO")
        save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        preview_dir = self.data_dir / "previews"
        preview_dir.mkdir(exist_ok=True)
        (preview_dir / "preview-iso.json").write_text(json.dumps({"preview_id": "preview-iso", "expires_at": "2099-01-01T00:00:00"}), encoding="utf-8")
        payload = preflight_batch(
            {"items": [{"game_id": None, "candidate": {"candidate_id": "cand-iso", "preview_id": "preview-iso", "path": str(iso), "platform": "", "emulator_id": None, "adapter_id": None, "archive_member": None}}]},
            state={"games": []}, profiles={}, data_dir=str(self.data_dir), which=lambda _: None,
        )
        result = payload["results"][0]
        self.assertEqual(result["status"], "blocked")
        self._assert_every_error_has_fix_action(result["checks"])
        self.assertTrue(any(c["code"] == "AMBIGUOUS_PLATFORM" for c in result["checks"]))

    def test_launch_preflight_blocked_code_has_fix_action(self):
        h = DummyLaunchHandler()
        # Use a game that will be blocked (missing path)
        from openbox import save_state

        save_state({"games": [{"name": "Missing", "path": str(self.data_dir / "missing.nes"), "platform": "NES", "game_id": "game-0123456789abcdef01234567-1"}], "profiles": {}, "history": [], "settings": {}, "playlists": []})
        h.launch_preflight({"game_id": "game-0123456789abcdef01234567-1", "fail_on_blocked": True}, request_id="req-fix")
        status, payload, _ = h.responses[-1]
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "LAUNCH_PREFLIGHT_BLOCKED")
        for chk in payload["checks"]:
            if chk["severity"] == "error":
                self.assertIn("fix_action", chk)

    def test_all_error_kinds_are_valid_flavors(self):
        from pkg.parity.parity_launch_doctor import _fix, _check

        # Ensure _check fallback creates explain_token for generic errors
        chk = _check("SOME_ERROR", "error", "Something failed")
        self.assertEqual(chk["fix_action"]["kind"], "explain_token")
        # Ensure fix helpers produce correct kinds
        for kind in ["flatpak_install", "reveal_bios_path", "pick_core", "explain_token"]:
            fix = _fix(kind, "label", {"x": 1})
            self.assertEqual(fix["kind"], kind)


if __name__ == "__main__":
    unittest.main()
