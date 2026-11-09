#!/usr/bin/env python3
"""Tests for the declared state-machine registry and its adopters."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.state.machines import (
    FACADE_TO_OPERATION,
    MACHINES,
    SESSION_ABANDONED,
    SESSION_ACTIVE,
    SESSION_STOPPING,
    job_result_state,
)
from pkg.state.operations import OperationService

EXPECTED_MACHINES = {
    "operation",
    "legacy_job",
    "job_facade",
    "session",
    "setup_preview",
    "match_preview",
    "storefront_source",
    "media_queue_worker",
    "media_queue_item",
    "game_progress",
    "household_challenge",
    "gameyfin_install",
    "setup_wizard",
    "party_wheel",
    "picker_phase",
    "overlay_stack",
    "repair_wizard",
}


class RegistryTests(unittest.TestCase):
    def test_every_discovered_machine_is_declared(self):
        self.assertEqual(EXPECTED_MACHINES, set(MACHINES))

    def test_transitions_only_use_declared_states(self):
        for name, machine in MACHINES.items():
            for action, pairs in machine.transitions.items():
                self.assertTrue(pairs, f"{name}.{action} has no transitions")
                for source, target in pairs:
                    for value in (source, target):
                        if value is not None:
                            self.assertIn(value, machine.states, f"{name}.{action}: {value!r}")

    def test_terminal_and_active_are_subsets(self):
        for name, machine in MACHINES.items():
            self.assertLessEqual(machine.terminal, machine.states, name)
            if machine.active is not None:
                self.assertLessEqual(machine.active, machine.states, name)
            if machine.derived and machine.active is not None:
                self.assertFalse(machine.derived & machine.active, name)

    def test_operation_machine_matches_adr_0011(self):
        machine = MACHINES["operation"]
        self.assertTrue(machine.allows("start", "queued", "running"))
        self.assertFalse(machine.allows("start", "done", "running"))
        self.assertTrue(machine.allows("finish", "queued", "done"))
        self.assertTrue(machine.allows("finish", "running", "partial"))
        self.assertEqual(
            {"interrupted"},
            machine.targets("interrupt", "running"),
        )
        self.assertFalse(machine.allows("interrupt", "done", "interrupted"))
        self.assertTrue(machine.allows("cancel", "cancelling", "cancelling"))
        for terminal in machine.terminal:
            self.assertTrue(machine.allows("retry", terminal, None))
        self.assertFalse(machine.allows("retry", "running", None))
        self.assertEqual({"interrupted"}, set(machine.states) & {"interrupted"})
        self.assertTrue(machine.allows("resume", "interrupted", None))
        self.assertFalse(machine.allows("resume", "cancelled", None))

    def test_facade_states_all_map_to_operation_states(self):
        facade = MACHINES["job_facade"]
        self.assertEqual(facade.states - {"idle"}, set(FACADE_TO_OPERATION))
        for target in FACADE_TO_OPERATION.values():
            self.assertIn(target, MACHINES["operation"].states)


class ProjectionTests(unittest.TestCase):
    def test_job_result_state(self):
        self.assertEqual("ready", job_result_state(None))
        self.assertEqual("ready", job_result_state(""))
        self.assertEqual("queued", job_result_state("queued"))
        self.assertEqual("running", job_result_state("running"))
        self.assertEqual("cancelling", job_result_state("cancelling"))
        self.assertEqual("ready", job_result_state("done"))
        self.assertEqual("ready", job_result_state("partial"))
        self.assertEqual("ready", job_result_state("error"))
        self.assertEqual("interrupted", job_result_state("interrupted"))

    def test_session_constants_match_machine(self):
        states = MACHINES["session"].states
        self.assertEqual({"active", "stopping", "abandoned"}, states)
        self.assertEqual({"active"}, {SESSION_ACTIVE} & states)
        self.assertEqual({"stopping"}, {SESSION_STOPPING} & states)
        self.assertEqual({"abandoned"}, {SESSION_ABANDONED} & states)


class FacadeSyncTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        data_path = Path(self.tempdir.name) / "library.json"
        data_path.write_text("{}", encoding="utf-8")
        self.service = OperationService(data_path)
        self.operation = self.service.create(operation_type="emulator.install", title="Test")

    def tearDown(self):
        self.tempdir.cleanup()

    def _sync(self, data):
        import webapp_state

        with mock.patch.object(webapp_state, "JOB_MANAGER") as manager, mock.patch(
            "pkg.state.operations.get_operation_service", return_value=self.service
        ):
            manager.get_operation_id.return_value = self.operation["job_id"]
            webapp_state._sync_facade_to_operation("emulator-install:test", data)

    def test_active_facade_marks_operation_running(self):
        self._sync({"state": "installing"})
        self.assertEqual("running", self.service.get(self.operation["job_id"])["state"])

    def test_terminal_facade_write_leaves_completion_to_the_job_manager(self):
        self._sync({"state": "installing"})
        self._sync({"state": "error", "message": "boom"})
        record = self.service.get(self.operation["job_id"])
        self.assertEqual("running", record["state"])
        self.assertIsNone(record["finished_at"])

    def test_finished_operation_is_not_reopened_by_a_facade_write(self):
        self.service.finish(self.operation["job_id"], state="partial")
        self._sync({"state": "installing"})
        self.assertEqual("partial", self.service.get(self.operation["job_id"])["state"])

    def test_missing_operation_is_ignored(self):
        import webapp_state

        with mock.patch.object(webapp_state, "JOB_MANAGER") as manager, mock.patch(
            "pkg.state.operations.get_operation_service", return_value=self.service
        ):
            manager.get_operation_id.return_value = None
            webapp_state._sync_facade_to_operation("emulator-install:test", {"state": "done"})

    def test_sync_failures_are_logged_not_raised(self):
        import webapp_state

        class _BoomService:
            def get(self, job_id):
                return {"state": "running"}

            def mark_running(self, *args, **kwargs):
                raise RuntimeError("boom")

            def update_progress(self, *args, **kwargs):
                raise RuntimeError("boom")

        with mock.patch.object(webapp_state, "JOB_MANAGER") as manager, mock.patch(
            "pkg.state.operations.get_operation_service", return_value=_BoomService()
        ):
            manager.get_operation_id.return_value = "job"
            webapp_state._sync_facade_to_operation(
                "emulator-install:test", {"state": "installing", "phase": "fetch"}
            )


class MetadataWorkerFailureTests(unittest.TestCase):
    def test_match_worker_records_error_on_failure(self):
        from handlers import metadata as metadata_module
        from webapp_state import MATCH_JOB

        captured = {}

        class _Handler:
            def send_json(self, status, payload):
                captured["status"] = status

        saved = dict(MATCH_JOB)
        try:
            MATCH_JOB.clear()
            with mock.patch.object(metadata_module, "METADATA_DATABASE", mock.Mock()), mock.patch.object(
                metadata_module, "JOB_MANAGER"
            ) as manager, mock.patch.object(
                metadata_module, "load_state", side_effect=RuntimeError("boom")
            ):
                manager.submit.side_effect = lambda name, worker: worker()
                with self.assertRaises(RuntimeError):
                    metadata_module.MetadataHandlers.match_metadata(_Handler(), {"platform": "all"})
            self.assertEqual("error", MATCH_JOB.get("state"))
            self.assertIn("boom", MATCH_JOB.get("error", ""))
        finally:
            MATCH_JOB.clear()
            MATCH_JOB.update(saved)


class ShutdownReapTests(unittest.TestCase):
    def test_no_sessions_is_a_noop(self):
        import web_app

        state = {"active_sessions": []}
        with mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state"
        ) as update:
            web_app.reap_sessions_for_shutdown()
        update.assert_not_called()

    def test_session_that_exited_gracefully_is_removed(self):
        import web_app

        state = {"active_sessions": [{"launch_id": "l1", "pid": 999999, "status": "active"}]}
        writes = []

        def _update(mutator):
            mutator(state)
            first = state["active_sessions"][0] if state["active_sessions"] else None
            writes.append(dict(first) if first else None)

        with mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state", side_effect=_update
        ), mock.patch.object(web_app, "process_alive", return_value=False), mock.patch.object(
            web_app, "terminate_process_tree"
        ) as terminate:
            web_app.reap_sessions_for_shutdown()
        terminate.assert_not_called()
        self.assertEqual("stopping", writes[0]["status"])
        self.assertEqual([], state["active_sessions"])

    def test_session_still_alive_is_force_killed(self):
        import web_app

        state = {"active_sessions": [{"launch_id": "l1", "pid": 4242, "pgid": 4242, "status": "active"}]}
        clock = iter([0.0, 0.0, 3.0])

        with mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state", side_effect=lambda mutator: mutator(state)
        ), mock.patch.object(web_app, "process_alive", return_value=True), mock.patch.object(
            web_app, "terminate_process_tree"
        ) as terminate, mock.patch("time.monotonic", side_effect=lambda: next(clock)), mock.patch("time.sleep"):
            web_app.reap_sessions_for_shutdown()
        terminate.assert_called_once_with(4242, force=True, pgid=4242)
        self.assertEqual([], state["active_sessions"])


    def test_failures_are_logged_not_raised(self):
        import web_app

        with mock.patch.object(web_app, "load_state", side_effect=RuntimeError("read")):
            web_app.reap_sessions_for_shutdown()

        state = {"active_sessions": [{"launch_id": "l1", "status": "active"}]}
        with mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state", side_effect=[RuntimeError("mark"), None]
        ):
            web_app.reap_sessions_for_shutdown()

        state = {"active_sessions": [{"launch_id": "l1", "status": "active"}]}
        with mock.patch.object(web_app, "load_state", return_value=state), mock.patch.object(
            web_app, "update_state", side_effect=[None, RuntimeError("remove")]
        ):
            web_app.reap_sessions_for_shutdown()


class MediaWorkerStateTests(unittest.TestCase):
    def setUp(self):
        import pkg.state.imports as imports_module

        self.module = imports_module
        self.worker = imports_module._MEDIA_WORKER
        self.original = dict(self.worker)

    def tearDown(self):
        self.worker.clear()
        self.worker.update(self.original)

    def _set(self, *, alive, paused=False, cancelled=False):
        class _Thread:
            def is_alive(self):
                return alive

        pause = threading.Event()
        if paused:
            pause.set()
        cancel = threading.Event()
        if cancelled:
            cancel.set()
        self.worker.update({"thread": _Thread(), "pause": pause, "cancel": cancel})
        return self.module.media_queue_worker_state()

    def test_derived_states_cover_the_declared_machine(self):
        declared = MACHINES["media_queue_worker"].states
        observed = {
            self._set(alive=False)["state"],
            self._set(alive=True)["state"],
            self._set(alive=True, paused=True)["state"],
            self._set(alive=True, cancelled=True)["state"],
        }
        self.assertEqual(declared, observed)

    def test_state_agrees_with_flags(self):
        status = self._set(alive=True, paused=True)
        self.assertEqual("paused", status["state"])
        self.assertTrue(status["running"])
        self.assertTrue(status["paused"])
        self.assertFalse(status["cancelled"])

        status = self._set(alive=False, cancelled=True)
        self.assertEqual("cancelled", status["state"])
        self.assertFalse(status["running"])
        self.assertTrue(status["cancelled"])

        status = self._set(alive=False)
        self.assertEqual("stopped", status["state"])


class MetadataStatusTests(unittest.TestCase):
    def _handler(self):
        captured = {}

        class _Handler:
            def send_json(self, status, payload):
                captured["status"] = status
                captured["payload"] = payload

        return captured, _Handler()

    def test_sync_start_does_not_clear_a_running_match(self):
        from handlers import metadata as metadata_module
        from webapp_state import MATCH_JOB, METADATA_JOB

        captured, handler = self._handler()
        saved_metadata = dict(METADATA_JOB)
        saved_match = dict(MATCH_JOB)
        try:
            MATCH_JOB.clear()
            MATCH_JOB.update({"state": "running", "matched": 4})
            METADATA_JOB.clear()
            with mock.patch.object(metadata_module, "JOB_MANAGER") as manager, mock.patch.object(
                metadata_module, "sync_database"
            ):
                manager.submit.side_effect = lambda name, worker: worker()
                metadata_module.MetadataHandlers.sync_metadata(handler)
            match_state = MATCH_JOB.get("state")
            match_count = MATCH_JOB.get("matched")
        finally:
            METADATA_JOB.clear()
            METADATA_JOB.update(saved_metadata)
            MATCH_JOB.clear()
            MATCH_JOB.update(saved_match)

        self.assertEqual(202, captured["status"])
        self.assertEqual("running", match_state)
        self.assertEqual(4, match_count)

    def test_status_reports_sync_and_match_jobs_separately(self):
        from handlers import metadata as metadata_module
        from webapp_state import MATCH_JOB, METADATA_JOB

        captured, handler = self._handler()
        saved_metadata = dict(METADATA_JOB)
        saved_match = dict(MATCH_JOB)
        try:
            METADATA_JOB.clear()
            METADATA_JOB.update({"state": "downloading"})
            MATCH_JOB.clear()
            MATCH_JOB.update({"state": "running", "matched": 3})
            with mock.patch.object(
                metadata_module, "METADATA_DATABASE", Path(tempfile.gettempdir()) / "missing-lbdb.db"
            ), mock.patch.object(
                metadata_module, "load_state_view", return_value={"games": []}
            ), mock.patch.object(metadata_module, "MEDIA_TYPES_ALL", set()):
                metadata_module.MetadataHandlers._api_get_api_metadata_status(handler, {})
        finally:
            METADATA_JOB.clear()
            METADATA_JOB.update(saved_metadata)
            MATCH_JOB.clear()
            MATCH_JOB.update(saved_match)

        self.assertEqual(200, captured["status"])
        self.assertEqual("downloading", captured["payload"]["job"]["state"])
        self.assertEqual("running", captured["payload"]["match_job"]["state"])
        self.assertEqual(3, captured["payload"]["match_job"]["matched"])


if __name__ == "__main__":
    unittest.main()
