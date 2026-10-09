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


class SameFixTests(unittest.TestCase):
    def test_a_group_keeps_its_fix_only_when_kind_and_payload_match(self):
        fix = {"kind": "flatpak_grant", "payload": {"app_id": "a", "path": "/x"}}
        self.assertFalse(audit._same_fix(None, fix))
        self.assertTrue(audit._same_fix(fix, dict(fix)))
        self.assertFalse(audit._same_fix(fix, {**fix, "payload": {"app_id": "a", "path": "/y"}}))


class GroupKeyTests(unittest.TestCase):
    def test_games_in_two_folders_are_two_groups_each_with_its_own_grant(self):
        def check(path):
            return {"code": "FLATPAK_FS_DENIED", "severity": "error", "message": "", "fix_action": {
                "kind": "flatpak_grant", "payload": {"app_id": "org.libretro.RetroArch", "path": path, "command": f"override {path}"}}}

        groups: dict = {}
        audit._record_checks(groups, {"game_id": "1", "index": 0, "name": "A"}, [check("/roms/snes")])
        audit._record_checks(groups, {"game_id": "2", "index": 1, "name": "B"}, [check("/roms/nes")])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["fix_action"] for group in groups.values()))


class TotalsChangeNoticeTests(unittest.TestCase):
    """The notice for a changed audit. Counts, not wall clock, so it holds on any runner."""

    @staticmethod
    def report(blocked, warning):
        return {"totals": {"ready": 5, "warning": warning, "blocked": blocked}, "computed_at": "2026-10-09T09:00:00+00:00"}

    def test_the_first_audit_says_nothing(self):
        self.assertIsNone(audit.totals_change_notice(None, self.report(1, 0)))

    def test_unchanged_totals_say_nothing(self):
        self.assertIsNone(audit.totals_change_notice(self.report(1, 2), self.report(1, 2)))

    def test_a_new_blocked_game_is_a_warning_that_states_both_counts(self):
        notice = audit.totals_change_notice(self.report(0, 2), self.report(1, 2))
        self.assertEqual(notice["level"], "warning")
        self.assertIn("Won't launch: 1 (was 0)", notice["body"])
        self.assertEqual(notice["dedupe_key"], "launch-audit:2026-10-09T09:00:00+00:00")

    def test_a_fixed_game_is_informational(self):
        notice = audit.totals_change_notice(self.report(3, 0), self.report(2, 0))
        self.assertEqual(notice["level"], "info")

    def test_the_notice_reaches_the_feed_once_per_result(self):
        from handlers import launch as launch_handler

        state = {}

        def apply(mutate):
            mutate(state)

        notice = audit.totals_change_notice(self.report(0, 0), self.report(1, 0))
        with mock.patch("webapp_state.transact_state", side_effect=apply):
            launch_handler._record_audit_notice(notice)
            launch_handler._record_audit_notice(notice)
        feed = state["notifications"]
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0]["kind"], "launch_audit")
        self.assertTrue(feed[0]["title"].startswith("Launch check changed: 1 won't launch"), feed[0]["title"])


class GroupFixPayloadTests(unittest.TestCase):
    """A group's one fix needs the folder a grant names; other per-game keys stay out of the cause."""

    def test_a_grant_keeps_its_folder_so_the_group_button_can_act(self):
        cause = audit._cause_payload({"kind": "flatpak_grant", "payload": {"app_id": "org.libretro.RetroArch", "path": "/home/u/Games", "command": "flatpak override"}})
        self.assertEqual(cause["path"], "/home/u/Games")
        self.assertEqual(cause["app_id"], "org.libretro.RetroArch")

    def test_other_causes_still_drop_the_per_game_path(self):
        cause = audit._cause_payload({"kind": "pick_core", "payload": {"core": "snes9x_libretro.so", "path": "/roms/g.sfc", "name": "G"}})
        self.assertEqual(cause, {"core": "snes9x_libretro.so"})


class RefreshReportTests(LaunchAuditTestCase):
    """A per-game refresh gives the same answer as a full audit of the changed library."""

    @staticmethod
    def membership(report):
        return [(g["key"], sorted(m["game_id"] for m in g["games"]), (g["fix_action"] or {}).get("kind")) for g in report["groups"]]

    def checks(self, games):
        # Emulators found and granted, so the library has a mix of ready, warning and blocked games.
        return dict(which=lambda name: f"/usr/bin/{name}", run=CountingRun())

    def test_refreshing_only_the_changed_games_matches_a_full_audit(self):
        games = self.library(60)
        before = audit.audit_library(games, {}, str(self.data_dir), **self.checks(games))
        changed = [dict(game) for game in games]
        for index in (0, 5, 11):
            changed[index]["path"] = str(self.data_dir / f"gone-{index}.bin")
        full = audit.audit_library(changed, {}, str(self.data_dir), **self.checks(changed))
        refreshed = audit.refresh_report(before, changed, {}, str(self.data_dir),
                                         game_ids=["game-0", "game-5", "game-11"], **self.checks(changed))
        self.assertEqual(refreshed["totals"], full["totals"])
        self.assertEqual(self.membership(refreshed), self.membership(full))

    def test_refreshing_every_game_matches_a_full_audit(self):
        games = self.library(40)
        before = audit.audit_library(games, {}, str(self.data_dir), **self.checks(games))
        changed = [dict(game) for game in games]
        for index in range(0, 40, 3):
            changed[index]["path"] = str(self.data_dir / f"missing-{index}.bin")
        full = audit.audit_library(changed, {}, str(self.data_dir), **self.checks(changed))
        refreshed = audit.refresh_report(before, changed, {}, str(self.data_dir),
                                         game_ids=[g["game_id"] for g in changed], **self.checks(changed))
        self.assertEqual(refreshed["totals"], full["totals"])
        self.assertEqual(self.membership(refreshed), self.membership(full))

    def test_a_game_that_changes_status_moves_the_totals_and_its_groups(self):
        games = self.library(12)
        before = audit.audit_library(games, {}, str(self.data_dir), **self.checks(games))
        broken = [dict(game) for game in games]
        broken[2]["path"] = str(self.data_dir / "gone.bin")
        broke = audit.refresh_report(before, broken, {}, str(self.data_dir), game_ids=["game-2"], **self.checks(broken))
        full = audit.audit_library(broken, {}, str(self.data_dir), **self.checks(broken))
        self.assertEqual(broke["totals"], full["totals"])
        self.assertEqual(broke["totals"]["blocked"], before["totals"]["blocked"] + 1)
        blocked = {m["game_id"] for g in broke["groups"] if g["severity"] == "error" for m in g["games"]}
        self.assertIn("game-2", blocked)
        healed = audit.refresh_report(broke, games, {}, str(self.data_dir), game_ids=["game-2"], **self.checks(games))
        self.assertEqual(healed["totals"], before["totals"])
        self.assertEqual(self.membership(healed), self.membership(before))

    def test_ids_the_library_does_not_have_are_ignored(self):
        games = self.library(6)
        before = audit.audit_library(games, {}, str(self.data_dir), **self.checks(games))
        refreshed = audit.refresh_report(before, games, {}, str(self.data_dir),
                                         game_ids=["no-such-game"], **self.checks(games))
        self.assertEqual(refreshed["totals"], before["totals"])
        self.assertEqual(self.membership(refreshed), self.membership(before))


class RefreshRouteTests(LaunchAuditTestCase):
    """POST /api/v2/launch/audit/refresh checks one group's games and refuses a stale report."""

    def setUp(self):
        super().setUp()
        from handlers.launch import LaunchHandlers

        class Handler(LaunchHandlers):
            def __init__(self):
                self.responses = []

            def send_json(self, status, payload, **kwargs):
                self.responses.append((status, payload))

        self.Handler = Handler
        self.state = {"games": self.library(12), "profiles": {}}
        self.patches = [
            mock.patch("handlers.launch.load_state", side_effect=lambda: json.loads(json.dumps(self.state))),
            mock.patch("handlers.launch.openbox.DATA", self.data_dir / "library.json"),
        ]
        for patch in self.patches:
            patch.start()
        report = audit.audit_library(self.state["games"], {}, str(self.data_dir), which=lambda name: f"/usr/bin/{name}", run=CountingRun())
        audit.store_audit(str(self.data_dir), report)
        self.report = report

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        super().tearDown()

    def test_refresh_checks_the_group_and_answers_with_the_totals(self):
        key = self.report["groups"][0]["key"]
        handler = self.Handler()
        with mock.patch("pkg.parity.parity_launch_audit.refresh_report", wraps=audit.refresh_report) as refresh:
            handler._api_post_api_v2_launch_audit_refresh({"group": key})
        status, payload = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["totals"], audit.load_audit(str(self.data_dir))["totals"])
        ids = refresh.call_args.kwargs["game_ids"]
        self.assertEqual(sorted(ids), sorted(m["game_id"] for m in self.report["groups"][0]["games"]))

    def test_an_unknown_group_is_refused_not_widened(self):
        from api_errors import BadRequest

        with self.assertRaises(BadRequest):
            self.Handler()._api_post_api_v2_launch_audit_refresh({"group": "GONE|x|y"})

    def test_a_changed_library_is_refused_until_it_is_checked_again(self):
        from api_errors import BadRequest

        self.state["games"] = self.state["games"] + [{"game_id": "new", "name": "New", "path": str(self.roms[0])}]
        with self.assertRaises(BadRequest):
            self.Handler()._api_post_api_v2_launch_audit_refresh({"group": self.report["groups"][0]["key"]})

    def test_two_refreshes_at_once_never_interleave_their_merge_and_write(self):
        import threading
        import time

        events = []

        def slow_refresh(report, games, profiles, data_dir, *, game_ids, **_kwargs):
            events.append("refresh")
            time.sleep(0.05)  # long enough that an unlocked second refresh would start inside this one
            return dict(report)

        def record_store(data_dir, report):
            events.append("store")

        key = self.report["groups"][0]["key"]
        errors = []

        def call():
            try:
                self.Handler()._api_post_api_v2_launch_audit_refresh({"group": key})
            except Exception as error:  # surfaced below; a thread must not swallow it
                errors.append(error)

        with mock.patch("pkg.parity.parity_launch_audit.refresh_report", side_effect=slow_refresh), \
             mock.patch("pkg.parity.parity_launch_audit.store_audit", side_effect=record_store):
            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(events, ["refresh", "store", "refresh", "store"])

    def test_a_missing_group_key_is_a_bad_request(self):
        from api_errors import BadRequest

        with self.assertRaises(BadRequest):
            self.Handler()._api_post_api_v2_launch_audit_refresh({})


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

    def status(self):
        handler = self.Handler()
        handler._api_get_api_v2_launch_audit_status(urlparse("/api/v2/launch/audit/status"))
        return handler.responses[-1]

    def test_status_is_empty_before_any_audit_and_never_scans(self):
        self.assertEqual(self.status(), (200, {"statuses": {}}))
        self.assertFalse(audit.audit_path(self.data_dir).exists())

    def test_status_flags_only_games_the_report_names_and_blocked_wins(self):
        self.run_job()
        status, payload = self.status()
        self.assertEqual(status, 200)
        statuses = payload["statuses"]
        self.assertTrue(statuses, "the fixture library has problems the audit reports")
        self.assertTrue(set(statuses.values()) <= {"blocked", "warning"})
        # A game in a blocking group must read blocked even if it is also in a warning group.
        report = audit.load_audit(str(self.data_dir))
        blocked_ids = {m["game_id"] for g in report["groups"] if g["severity"] == "error" for m in g["games"]}
        for game_id in blocked_ids:
            self.assertEqual(statuses[game_id], "blocked", game_id)

    def test_status_is_withheld_when_the_library_changed_since_the_audit(self):
        self.run_job()
        self.state["games"].append({"game_id": "late-add", "name": "Late", "path": "/roms/late.nes", "platform": "NES"})
        self.assertEqual(self.status(), (200, {"statuses": {}}))

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
