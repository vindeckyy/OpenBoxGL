#!/usr/bin/env python3
"""One-click Flatpak folder access (pkg/parity/parity_flatpak_grant.py).

The grant changes an emulator's sandbox permissions, so these tests pin each guard:
a declared Flatpak id, a folder under the home directory (never the home itself),
an existing directory, and a read-only grant that undo removes.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: E402,F401  (flat-import finder for parity modules)
from api_errors import BadRequest  # noqa: E402
from pkg.parity import parity_flatpak_grant as grant  # noqa: E402


class GrantGuardTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obx-grant-home-"))
        self.games = self.home / "Games"
        self.games.mkdir()
        patcher = mock.patch("pkg.parity.parity_flatpak_grant.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_a_declared_flatpak_id_is_accepted(self):
        with self.assertRaises(BadRequest):
            grant.validate_grant("org.evil.Launcher", str(self.games))
        self.assertIn("org.libretro.RetroArch", grant.known_flatpak_ids())

    def test_the_folder_must_be_absolute_and_inside_home(self):
        for folder in ("Games", "", str(self.home), "/etc", str(self.home.parent)):
            with self.subTest(folder=folder), self.assertRaises(BadRequest):
                grant.validate_grant("org.libretro.RetroArch", folder)

    def test_the_folder_must_exist(self):
        with self.assertRaises(BadRequest):
            grant.validate_grant("org.libretro.RetroArch", str(self.home / "missing"))

    def test_a_valid_request_resolves_to_the_folder(self):
        app, folder = grant.validate_grant("org.libretro.RetroArch", str(self.games))
        self.assertEqual(app, "org.libretro.RetroArch")
        self.assertEqual(folder, str(self.games.resolve()))


class GrantCommandTests(unittest.TestCase):
    def test_grant_is_read_only_and_undo_removes_the_same_path(self):
        folder = "/home/u/Games"
        self.assertEqual(
            grant.grant_command("org.libretro.RetroArch", folder),
            ["flatpak", "override", "--user", "--filesystem=/home/u/Games:ro", "org.libretro.RetroArch"],
        )
        self.assertEqual(
            grant.grant_command("org.libretro.RetroArch", folder, undo=True),
            ["flatpak", "override", "--user", "--nofilesystem=/home/u/Games", "org.libretro.RetroArch"],
        )


class ApplyGrantTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="obx-grant-apply-"))
        self.games = self.home / "Games"
        self.games.mkdir()
        patcher = mock.patch("pkg.parity.parity_flatpak_grant.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_successful_grant_runs_the_exact_command(self):
        seen = {}

        def run(command, **kwargs):
            seen["command"] = command
            seen["timeout"] = kwargs.get("timeout")
            return subprocess.CompletedProcess(command, 0, "", "")

        result = grant.apply_grant("org.libretro.RetroArch", str(self.games), run=run)
        self.assertTrue(result["applied"])
        self.assertFalse(result["undone"])
        self.assertEqual(seen["command"][-2], f"--filesystem={self.games.resolve()}:ro")
        self.assertEqual(seen["timeout"], grant.GRANT_TIMEOUT_SECONDS)

    def test_undo_reports_itself(self):
        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, "", "")

        result = grant.apply_grant("org.libretro.RetroArch", str(self.games), undo=True, run=run)
        self.assertTrue(result["undone"])

    def test_a_refused_change_says_why(self):
        def run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "", "error: permission denied")

        with self.assertRaises(BadRequest) as raised:
            grant.apply_grant("org.libretro.RetroArch", str(self.games), run=run)
        self.assertIn("permission denied", str(raised.exception))

    def test_a_hung_flatpak_is_a_bounded_error(self):
        def run(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, grant.GRANT_TIMEOUT_SECONDS)

        with self.assertRaises(BadRequest):
            grant.apply_grant("org.libretro.RetroArch", str(self.games), run=run)

    def test_a_missing_flatpak_binary_is_a_bounded_error(self):
        def run(command, **_kwargs):
            raise FileNotFoundError("flatpak")

        with self.assertRaises(BadRequest):
            grant.apply_grant("org.libretro.RetroArch", str(self.games), run=run)


class GrantRouteTests(unittest.TestCase):
    def test_both_routes_are_registered_as_post(self):
        from routes import POST_TABLE

        self.assertEqual(POST_TABLE["/api/v2/launch/grant"], "_api_post_api_v2_launch_grant")
        self.assertEqual(POST_TABLE["/api/v2/launch/grant/undo"], "_api_post_api_v2_launch_grant_undo")


class GrantRouteBehaviourTests(unittest.TestCase):
    """The grant and undo routes hand the request to apply_grant and return its result."""

    def handler(self):
        from handlers.launch import LaunchHandlers

        class Handler(LaunchHandlers):
            def __init__(self):
                self.responses = []

            def send_json(self, status, payload, **kwargs):
                self.responses.append((status, payload))

        return Handler()

    def test_grant_and_undo_call_apply_grant_with_the_request(self):
        with mock.patch("pkg.parity.parity_flatpak_grant.apply_grant", return_value={"applied": True}) as apply:
            handler = self.handler()
            handler._api_post_api_v2_launch_grant({"app_id": "org.libretro.RetroArch", "folder": "/roms"})
            handler._api_post_api_v2_launch_grant_undo({"app_id": "org.libretro.RetroArch", "folder": "/roms"})
        apply.assert_any_call("org.libretro.RetroArch", "/roms")
        apply.assert_any_call("org.libretro.RetroArch", "/roms", undo=True)
        self.assertEqual([status for status, _ in handler.responses], [200, 200])


if __name__ == "__main__":
    unittest.main()
