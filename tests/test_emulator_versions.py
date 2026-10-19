#!/usr/bin/env python3
"""Tests for the best-effort emulator version probe (P6.3)."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pkg.parity import parity_emulator_settings as settings  # noqa: E402


class Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def which_from(mapping):
    def which(name):
        return mapping.get(name)
    return which


class VersionProbeTests(unittest.TestCase):
    def setUp(self):
        settings._reset_version_cache()

    def test_native_probe_parses_version_and_compares_latest(self):
        run = FakeRun([
            Completed(stdout="MAME 0.259\n"),
            Completed(stdout="ID: org.mamedev.MAME\nVersion: 0.260\n"),
        ])
        result = settings.emulator_version_status(
            "org.mamedev.MAME",
            {"native": "mame", "flatpak_app_id": "org.mamedev.MAME"},
            which=which_from({"mame": "/usr/bin/mame", "flatpak": "/usr/bin/flatpak"}),
            run=run,
            now=lambda: 1000.0,
        )
        self.assertEqual(result["version"], "MAME 0.259")
        self.assertEqual(result["latest_version"], "0.260")
        self.assertTrue(result["update_available"])
        self.assertTrue(result["installed"])
        self.assertTrue(result["native"])
        self.assertFalse(result["flatpak"])
        self.assertEqual(run.calls[0][0], ["/usr/bin/mame", "--version"])
        self.assertEqual(run.calls[0][1]["timeout"], 5.0)
        self.assertEqual(run.calls[1][0], ["/usr/bin/flatpak", "remote-info", "--user", "flathub", "org.mamedev.MAME"])
        self.assertEqual(run.calls[1][1]["timeout"], 10.0)

    def test_flatpak_only_probe(self):
        run = FakeRun([
            Completed(stdout="1.16.0\n"),
            Completed(stdout="Version: 1.16.0\n"),
        ])
        result = settings.emulator_version_status(
            "org.libretro.RetroArch",
            {"native": "", "flatpak_app_id": "org.libretro.RetroArch"},
            which=which_from({"flatpak": "/usr/bin/flatpak"}),
            run=run,
            now=lambda: 1.0,
        )
        self.assertTrue(result["installed"])
        self.assertTrue(result["flatpak"])
        self.assertFalse(result["native"])
        self.assertEqual(result["version"], "1.16.0")
        self.assertEqual(result["latest_version"], "1.16.0")
        self.assertFalse(result["update_available"])
        self.assertEqual(run.calls[0][0], [
            "/usr/bin/flatpak", "info", "--user", "--show-version", "org.libretro.RetroArch",
        ])

    def test_timeout_is_bounded_and_reported(self):
        timeout = subprocess.TimeoutExpired(cmd=["mame", "--version"], timeout=5)
        run = FakeRun([timeout, timeout])
        result = settings.emulator_version_status(
            "mame",
            {"native": "mame", "flatpak_app_id": "org.mamedev.MAME"},
            which=which_from({"mame": "/usr/bin/mame", "flatpak": "/usr/bin/flatpak"}),
            run=run,
            now=lambda: 1.0,
        )
        self.assertEqual(result["version"], "")
        self.assertEqual(result["latest_version"], "")
        self.assertFalse(result["update_available"])
        self.assertIn("timed out", result["error"])
        self.assertTrue(all(call[1]["timeout"] for call in run.calls))

    def test_remote_failure_is_best_effort(self):
        run = FakeRun([
            Completed(stdout="1.0\n"),
            Completed(returncode=1, stderr="network unavailable\n"),
        ])
        result = settings.emulator_version_status(
            "retroarch-flatpak",
            {"flatpak_app_id": "org.libretro.RetroArch"},
            which=which_from({"flatpak": "/usr/bin/flatpak"}),
            run=run,
            now=lambda: 1.0,
        )
        self.assertEqual(result["version"], "1.0")
        self.assertEqual(result["latest_version"], "")
        self.assertFalse(result["update_available"])
        self.assertIn("network unavailable", result["error"])

    def test_missing_tools_report_not_installed(self):
        run = FakeRun([])
        result = settings.emulator_version_status(
            "nothing",
            {"native": "nothing"},
            which=which_from({}),
            run=run,
            now=lambda: 1.0,
        )
        self.assertEqual(result, {
            "installed": False,
            "native": False,
            "flatpak": False,
            "version": "",
            "latest_version": "",
            "update_available": False,
            "error": "",
        })
        self.assertEqual(run.calls, [])

    def test_cache_avoids_repeated_probes_and_expires(self):
        run = FakeRun([
            Completed(stdout="1.0\n"),
            Completed(stdout="Version: 1.0\n"),
            Completed(stdout="1.0\n"),
            Completed(stdout="Version: 1.0\n"),
        ])
        definition = {"native": "mame", "flatpak_app_id": "org.mamedev.MAME"}
        which = which_from({"mame": "/usr/bin/mame", "flatpak": "/usr/bin/flatpak"})
        first = settings.emulator_version_status(
            "cache-mame", definition, which=which, run=run, now=lambda: 100.0
        )
        second = settings.emulator_version_status(
            "cache-mame", definition, which=which, run=run, now=lambda: 159.0
        )
        self.assertEqual(first, second)
        self.assertEqual(len(run.calls), 2)

        third = settings.emulator_version_status(
            "cache-mame", definition, which=which, run=run, now=lambda: 160.0
        )
        self.assertEqual(third["version"], "1.0")
        self.assertEqual(len(run.calls), 4)

    def test_cache_is_per_emulator(self):
        run = FakeRun([
            Completed(stdout="1.0\n"),
            Completed(stdout="Version: 1.0\n"),
            Completed(stdout="2.0\n"),
            Completed(stdout="Version: 2.0\n"),
        ])
        which = which_from({"mame": "/usr/bin/mame", "flatpak": "/usr/bin/flatpak"})
        settings.emulator_version_status(
            "one", {"native": "mame", "flatpak_app_id": "a"}, which=which, run=run, now=lambda: 1.0
        )
        result = settings.emulator_version_status(
            "two", {"native": "mame", "flatpak_app_id": "b"}, which=which, run=run, now=lambda: 1.0
        )
        self.assertEqual(result["version"], "2.0")


class EmulatorListShapeTests(unittest.TestCase):
    def test_list_api_exposes_the_probed_version_and_skip_flag(self):
        from handlers.emulators import EmulatorsHandlers

        class Handler(EmulatorsHandlers):
            def __init__(self):
                self.status = None
                self.payload = None

            def send_json(self, status, payload):
                self.status = status
                self.payload = payload

        handler = Handler()
        rows = [
            {"app_id": "org.mamedev.MAME", "installed": True, "name": "MAME", "platforms": []},
            {"app_id": "org.libretro.RetroArch", "installed": True, "name": "RetroArch", "platforms": []},
        ]
        with patch("handlers.emulators.emulator_status", return_value=rows), patch(
            "handlers.emulators.version_probe",
            return_value={"version": "0.259", "latest_version": "0.260", "update_available": True},
        ), patch(
            "handlers.emulators.load_state_view",
            return_value={"settings": {"emulator_update_checks": ["org.libretro.RetroArch"]}},
        ):
            handler._api_get_api_emulators({})

        self.assertEqual(handler.status, 200)
        mame, retroarch = handler.payload["emulators"]
        # The browser renders ``emulator.version`` as text, so this route must
        # expose the version string itself, not the probe object.
        self.assertEqual(mame["version"], "0.259")
        self.assertTrue(mame["update_available"])
        self.assertFalse(mame["skip_update_check"])
        self.assertEqual(retroarch["version"], "")
        self.assertTrue(retroarch["skip_update_check"])


if __name__ == "__main__":
    unittest.main()
