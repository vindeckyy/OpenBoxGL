#!/usr/bin/env python3
"""P2-11: emulator installs run as a cancellable background job."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OPENBOX_DATA_DIR", tempfile.mkdtemp(prefix="openbox-emulator-job-"))

import handlers.imports as imports_module  # noqa: E402
from handlers.imports import _submit_emulator_install_job  # noqa: E402


class FakeCancelEvent:
    def __init__(self, cancel_after=None):
        self._cancelled = False
        self.cancel_after = cancel_after
        self.progress_calls = []

    def is_set(self):
        return self._cancelled

    def progress(self, **kwargs):
        self.progress_calls.append(kwargs)
        if self.cancel_after is not None and len(self.progress_calls) >= self.cancel_after:
            self._cancelled = True
        return {}


class FakeJobManager:
    def __init__(self):
        self.names = []
        self.worker = None

    def submit(self, name, worker):
        self.names.append(name)
        self.worker = worker
        return {"job_id": "fake-job", "state": "queued"}


class EmulatorInstallJobTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeJobManager()
        self.installed = []

    def _submit(self, app_ids, cancel_event=None):
        def fake_install(app_id):
            if app_id == "fail.app":
                raise OSError("no such flatpak")
            self.installed.append(app_id)
            return {}

        with mock.patch.object(imports_module, "JOB_MANAGER", self.manager), \
             mock.patch.object(imports_module, "install_emulator", side_effect=fake_install):
            descriptor = _submit_emulator_install_job(app_ids)
            result = self.manager.worker(cancel_event)
        return descriptor, result

    def test_background_job_installs_sequentially_and_reports_progress(self):
        descriptor, result = self._submit(["ok.app", "fail.app", "later.app"])
        self.assertEqual(descriptor["job_id"], "fake-job")
        self.assertEqual(descriptor["pending"], ["ok.app", "fail.app", "later.app"])
        self.assertEqual(result["installed"], ["ok.app", "later.app"])
        self.assertEqual(result["errors"], ["fail.app: no such flatpak"])
        self.assertEqual(self.manager.names, ["emulator-install"])

    def test_cancellation_stops_before_the_next_install(self):
        cancel_event = FakeCancelEvent(cancel_after=1)
        _, result = self._submit(["ok.app", "second.app"], cancel_event)
        self.assertEqual(result["installed"], ["ok.app"])
        self.assertTrue(cancel_event.progress_calls)

    def test_import_wizard_response_shape_is_backward_compatible(self):
        from handlers.imports import ImportsHandlers

        handler = object.__new__(ImportsHandlers)
        handler.send_json = mock.Mock()
        with mock.patch.object(imports_module, "import_folder_path", return_value=(3, 5, {"PC": ["mame"]})), \
             mock.patch.object(imports_module, "_submit_emulator_install_job", return_value={"job_id": "j1", "state": "queued", "pending": ["a.app"], "total": 1}) as submit, \
             mock.patch.object(imports_module, "clear_file_probe_cache"):
            handler.import_wizard({"folder": "/tmp/roms", "chosen_emulators": {"PC": "a.app"}})
        submit.assert_called_once_with(["a.app"])
        args, payload = handler.send_json.call_args[0]
        self.assertEqual(args, 200)
        self.assertEqual(payload["added"], 3)
        self.assertEqual(payload["found"], 5)
        self.assertEqual(payload["installed"], [])
        self.assertEqual(payload["install_job"]["job_id"], "j1")


if __name__ == "__main__":
    unittest.main()
