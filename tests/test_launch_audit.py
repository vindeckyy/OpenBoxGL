#!/usr/bin/env python3
"""F1 -- the Launch Audit: the Doctor across the whole library, grouped by cause.

What these tests pin:

- **Spawn count, not wall clock.** A 2,000-game audit spawns at most two
  ``flatpak`` processes per distinct app id (<= 48 for the 24 shipped
  definitions), and probes each binary name with ``which`` once, however many
  games share it. Deterministic and CI-safe, so it holds on a loaded runner.
- **One taxonomy.** Every game's audit membership is exactly the set of codes
  the single-game Doctor reports for it, so the two can never disagree.
- **Paginated, never unbounded**, and **the audit never mutates the library**.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_launch_audit as audit  # noqa: E402
from pkg.parity.parity_emulator_defs import _registry  # noqa: E402
from pkg.parity.parity_launch_doctor import run_preflight_checks  # noqa: E402


class CountingRun:
    """A fake ``subprocess.run`` that counts real calls per argv."""

    def __init__(self, stdout="filesystem=host\n", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr="")


class CountingWhich:
    """``which`` that finds only flatpak, so every adapter takes the flatpak path."""

    def __init__(self):
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return "/usr/bin/flatpak" if name == "flatpak" else None


def _flatpak_adapters():
    seen = {}
    for adapter in _registry()["by_adapter_id"].values():
        app_id = adapter.get("flatpak_app_id")
        if app_id and adapter.get("extensions") and app_id not in seen:
            seen[app_id] = adapter
    return list(seen.values())


class LaunchAuditTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        self.adapters = _flatpak_adapters()
        self.assertTrue(self.adapters, "the shipped registry has no flatpak adapters to audit against")
        self.roms = []
        for number, adapter in enumerate(self.adapters):
            rom = self.data_dir / f"rom{number}.{adapter['extensions'][0]}"
            rom.write_bytes(b"ROM")
            self.roms.append(rom)

    def tearDown(self):
        self.tempdir.cleanup()

    def library(self, size):
        games = []
        for index in range(size):
            slot = index % len(self.adapters)
            adapter = self.adapters[slot]
            games.append({
                "game_id": f"game-{index}",
                "name": f"Game {index}",
                "path": str(self.roms[slot]),
                "platform": adapter.get("platform", ""),
                "emulator_adapter_id": adapter["adapter_id"],
            })
        return games


class ProbeMemoizationTests(LaunchAuditTestCase):
    def test_two_thousand_games_spawn_at_most_two_flatpaks_per_app_id(self):
        run = CountingRun()
        which = CountingWhich()
        report = audit.audit_library(self.library(2000), {}, str(self.data_dir), which=which, run=run)

        app_ids = {adapter["flatpak_app_id"] for adapter in self.adapters}
        self.assertLessEqual(len(run.calls), 2 * len(app_ids))
        self.assertLessEqual(len(run.calls), 48, "the plan's bound: 24 definitions, two probes each")
        self.assertEqual(len(run.calls), len(set(run.calls)), "an argv was spawned twice")
        self.assertEqual(report["probes"]["flatpak_spawns"], len(run.calls))
        # Each distinct binary name is probed once, independent of library size.
        self.assertEqual(len(which.calls), len(set(which.calls)))
        self.assertEqual(report["probes"]["which_misses"], len(which.calls))
        self.assertEqual(sum(report["totals"].values()), 2000)

    def test_the_spawn_count_does_not_grow_with_the_library(self):
        small_run, large_run = CountingRun(), CountingRun()
        audit.audit_library(self.library(len(self.adapters)), {}, str(self.data_dir), which=CountingWhich(), run=small_run)
        audit.audit_library(self.library(2000), {}, str(self.data_dir), which=CountingWhich(), run=large_run)
        self.assertEqual(len(large_run.calls), len(small_run.calls))

    def test_the_filesystem_grant_is_still_decided_per_rom(self):
        """The permission *text* is cached, never the per-ROM answer.

        One app id, two ROMs: one inside the granted folder, one outside. Caching
        the boolean by app id would report both the same way.
        """
        adapter = self.adapters[0]
        granted = self.data_dir / "granted"
        granted.mkdir()
        inside = granted / f"in.{adapter['extensions'][0]}"
        outside = self.data_dir / f"out.{adapter['extensions'][0]}"
        inside.write_bytes(b"ROM")
        outside.write_bytes(b"ROM")
        run = CountingRun(stdout=f"[Context]\nfilesystem={granted}\n")
        games = [
            {"game_id": "in", "name": "In", "path": str(inside), "platform": adapter.get("platform", ""), "emulator_adapter_id": adapter["adapter_id"]},
            {"game_id": "out", "name": "Out", "path": str(outside), "platform": adapter.get("platform", ""), "emulator_adapter_id": adapter["adapter_id"]},
        ]
        with mock.patch("pkg.parity.parity_launch_doctor.Path.home", return_value=self.data_dir / "nohome"):
            report = audit.audit_library(games, {}, str(self.data_dir), which=CountingWhich(), run=run)
        denied = [group for group in report["groups"] if group["code"] == "FLATPAK_FS_DENIED"]
        self.assertEqual(len(denied), 1)
        self.assertEqual([member["game_id"] for member in denied[0]["games"]], ["out"])
        self.assertLessEqual(len(run.calls), 2)

    def test_a_hung_flatpak_is_not_installed_rather_than_a_crash(self):
        def hung(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))

        report = audit.audit_library(self.library(3), {}, str(self.data_dir), which=CountingWhich(), run=hung)
        self.assertEqual(report["failed"]["count"], 0)
        self.assertEqual(report["totals"]["blocked"], 3)


class TaxonomyParityTests(LaunchAuditTestCase):
    def test_audit_membership_is_exactly_the_doctors_codes(self):
        games = self.library(len(self.adapters) * 2)
        # A missing ROM and a game with no path, so the parity is not only one code.
        games.append({"game_id": "gone", "name": "Gone", "path": str(self.data_dir / "missing.bin"), "platform": "NES"})
        games.append({"game_id": "blank", "name": "Blank", "path": ""})
        run, which = CountingRun(), CountingWhich()
        report = audit.audit_library(games, {}, str(self.data_dir), which=which, run=run)

        by_game = {}
        for group in report["groups"]:
            for member in group["games"]:
                by_game.setdefault(member["game_id"], set()).add(group["code"])
        for game in games:
            expected = {check["code"] for check in run_preflight_checks(game, {}, str(self.data_dir), which=which, run=run, deep=False)}
            self.assertEqual(by_game.get(game["game_id"], set()), expected, game["game_id"])

    def test_groups_are_root_causes_with_one_fix_for_all_members(self):
        report = audit.audit_library(self.library(len(self.adapters) * 3), {}, str(self.data_dir),
                                     which=lambda name: None, run=CountingRun(returncode=1))
        install = [group for group in report["groups"] if (group["fix_action"] or {}).get("kind") == "flatpak_install"]
        self.assertTrue(install, "no 'install this emulator' group was produced")
        for group in install:
            self.assertEqual(group["count"], 3, group["key"])
            self.assertTrue(group["fix_action"]["payload"]["app_id"])
        # Blocked causes sort before warnings, largest first.
        ranks = [{"error": 0, "warning": 1}.get(group["severity"], 2) for group in report["groups"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_deep_mode_is_the_only_path_that_opens_archives(self):
        broken = self.data_dir / "broken.zip"
        broken.write_bytes(b"not a zip")
        game = {"game_id": "zip", "name": "Zip", "path": str(broken), "platform": "Arcade"}
        shallow = audit.audit_library([game], {}, str(self.data_dir), which=lambda name: None, run=CountingRun())
        deep = audit.audit_library([game], {}, str(self.data_dir), which=lambda name: None, run=CountingRun(), deep=True)
        self.assertNotIn("ARCHIVE_INVALID", {group["code"] for group in shallow["groups"]})
        self.assertIn("ARCHIVE_INVALID", {group["code"] for group in deep["groups"]})

    def test_cancel_stops_the_audit_and_reports_nothing(self):
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 5

        progress = mock.Mock()
        report = audit.audit_library(self.library(50), {}, str(self.data_dir), which=lambda name: None,
                                     run=CountingRun(), is_cancelled=cancelled, progress=progress)
        self.assertIsNone(report)
        progress.assert_called()


class AuditRouteTests(LaunchAuditTestCase):
    def setUp(self):
        super().setUp()
        from handlers.launch import LaunchHandlers

        class Handler(LaunchHandlers):
            def __init__(self):
                self.responses = []

            def send_json(self, status, payload, **kwargs):
                self.responses.append((status, json.loads(json.dumps(payload))))

        self.Handler = Handler
        self.state = {"games": self.library(120), "profiles": {}}
        self.patches = [
            mock.patch("handlers.launch.load_state", side_effect=lambda: json.loads(json.dumps(self.state))),
            mock.patch("handlers.launch.openbox.DATA", self.data_dir / "library.json"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        super().tearDown()

    def get(self, query=""):
        handler = self.Handler()
        with mock.patch("handlers.launch._audit_job", return_value=None):
            handler._api_get_api_v2_launch_audit(urlparse(f"/api/v2/launch/audit?{query}"))
        return handler.responses[-1]

    def run_job(self):
        from handlers.launch import run_launch_audit

        with mock.patch("pkg.parity.parity_launch_audit.shutil.which", lambda name: None), \
             mock.patch("pkg.parity.parity_launch_audit.subprocess.run", CountingRun(returncode=1)):
            return run_launch_audit(None)

    def test_get_never_scans(self):
        status, payload = self.get()
        self.assertEqual(status, 200)
        self.assertFalse(payload["scanned"])
        self.assertFalse(audit.audit_path(self.data_dir).exists())

    def test_the_job_reports_and_the_route_pages_members(self):
        before = json.dumps(self.state, sort_keys=True)
        result = self.run_job()
        self.assertEqual(json.dumps(self.state, sort_keys=True), before, "the audit mutated the library")
        self.assertEqual(sum(result["totals"].values()), 120)

        status, payload = self.get()
        self.assertTrue(payload["scanned"])
        self.assertFalse(payload["stale"])
        self.assertNotIn("games", payload["groups"][0], "the summary must not carry member lists")
        biggest = max(payload["groups"], key=lambda group: group["count"])

        _, page = self.get(f"group={biggest['key']}&limit=7&offset=0")
        self.assertEqual(len(page["members"]["games"]), 7)
        self.assertEqual(page["members"]["total"], biggest["count"])
        _, capped = self.get(f"group={biggest['key']}&limit=999999")
        self.assertEqual(capped["members"]["limit"], 500)

    def test_a_library_change_marks_the_report_stale(self):
        self.run_job()
        self.state["games"].append({"game_id": "new", "name": "New", "path": ""})
        _, payload = self.get()
        self.assertTrue(payload["stale"])

    def test_a_bad_page_parameter_is_a_400(self):
        from api_errors import BadRequest

        with self.assertRaises(BadRequest):
            self.get("group=x&limit=ten")

    def test_scan_queues_one_replacing_job(self):
        handler = self.Handler()
        manager = mock.Mock()
        manager.submit.return_value = {"job_id": "job-1"}
        with mock.patch("webapp_state.JOB_MANAGER", manager):
            handler._api_post_api_v2_launch_audit_scan({"deep": True})
        status, payload = handler.responses[-1]
        self.assertEqual(status, 202)
        self.assertEqual(payload["job_id"], "job-1")
        self.assertTrue(payload["deep"])
        self.assertEqual(manager.submit.call_args.args[0], "launch-audit")
        self.assertTrue(manager.submit.call_args.kwargs["replace"])


if __name__ == "__main__":
    unittest.main()
