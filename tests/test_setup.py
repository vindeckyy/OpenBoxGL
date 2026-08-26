"""Tests for setup v2 HTTP handlers."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, PreviewExpired, PreviewNotFound  # noqa: E402
from handlers.setup import SetupHandlers, _parse_limit, _preview_state_for_job  # noqa: E402
from pkg.parity.parity_setup_preview import create_preview_record, save_preview  # noqa: E402
from pkg.state.operations import get_operation_service  # noqa: E402


class DummySetupHandler(SetupHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


class SetupHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.data_dir)
        import openbox
        from state_store import JsonStateStore

        openbox.APP_DIR = self.data_dir
        openbox.DATA = self.data_dir / "library.json"
        openbox.STATE_STORE = JsonStateStore(openbox.DATA)
        openbox.DATA.write_text(
            json.dumps(
                {
                    "schema_version": 6,
                    "games": [
                        {
                            "game_id": "ready-1",
                            "name": "Ready Game",
                            "platform": "NES",
                            "path": str(self.data_dir / "ready.nes"),
                            "emulator_adapter_id": "retroarch-nes",
                            "emulator_id": "retroarch",
                        },
                        {
                            "game_id": "warn-1",
                            "name": "Warning Game",
                            "platform": "NES",
                            "path": str(self.data_dir / "warn.nes"),
                            "emulator_adapter_id": "retroarch-nes",
                            "emulator_id": "retroarch",
                        },
                        {
                            "game_id": "block-1",
                            "name": "Blocked Game",
                            "platform": "Mystery Platform",
                            "path": str(self.data_dir / "block.bin"),
                        },
                        {
                            "game_id": "unk-1",
                            "name": "Custom Game",
                            "platform": "NES",
                            "path": str(self.data_dir / "custom.nes"),
                            "launch": "/bin/sh {path}",
                        },
                    ],
                    "settings": {},
                }
            )
        )
        (self.data_dir / "ready.nes").write_bytes(b"NES")
        (self.data_dir / "warn.nes").write_bytes(b"NES")
        (self.data_dir / "block.bin").write_bytes(b"X")
        (self.data_dir / "custom.nes").write_bytes(b"NES")
        get_operation_service(openbox.DATA).persist()

    def tearDown(self):
        self.tempdir.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def test_summary_contains_required_keys_and_readiness_buckets(self):
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.load_state"), mock.patch(
            "handlers.setup.compute_summary",
            return_value={
                "library_count": 4,
                "source_coverage": [{"source_id": "library", "label": "Library", "game_count": 4, "coverage_percent": 100.0}],
                "metadata_match_percent": 0.0,
                "media_gaps": 4,
                "duplicate_count": 0,
                "missing_paths": 0,
                "emulator_readiness": {"ready": 1, "warning": 1, "blocked": 1, "unknown": 1},
                "active_operations": 0,
                "next_action": {"id": "fix_launch", "label": "Fix launch readiness", "step": 5},
            },
        ):
            handler._api_get_api_v2_setup_summary(mock.Mock(query=""))
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        for key in (
            "library_count",
            "source_coverage",
            "metadata_match_percent",
            "media_gaps",
            "duplicate_count",
            "missing_paths",
            "emulator_readiness",
            "active_operations",
            "next_action",
        ):
            self.assertIn(key, payload)
        readiness = payload["emulator_readiness"]
        self.assertEqual(
            readiness["ready"] + readiness["warning"] + readiness["blocked"] + readiness["unknown"],
            payload["library_count"],
        )

    def test_post_preview_returns_required_keys(self):
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.JOB_MANAGER.submit") as submit_mock, mock.patch(
            "handlers.setup.run_scan_job",
            return_value={"scanned_entries": 0},
        ):
            submit_mock.return_value = {"job_id": "job-1", "state": "queued"}
            handler._api_post_api_v2_setup_preview(
                {
                    "sources": [{"type": "files", "id": "f1", "path": None, "paths": ["/tmp/a.nes"], "recursive": False, "watched": False, "platform": None, "emulator_id": None, "include_uninstalled": False, "dat_path": None, "set_type": None, "adapter_id": None}],
                    "options": {"include_owned_uninstalled": False},
                }
            )
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 202)
        for key in ("preview_id", "revision", "job_id", "expires_at", "state"):
            self.assertIn(key, payload)

    def test_get_preview_document_has_counts_without_items(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={"include_owned_uninstalled": False},
            items=[],
        )
        handler = DummySetupHandler()
        handler._api_get_api_v2_setup_preview(mock.Mock(query=f"preview_id={preview['preview_id']}"))
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertNotIn("items", payload)
        self.assertIn("counts", payload)

    def test_post_revalidate_returns_202_contract(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={"include_owned_uninstalled": False},
            items=[],
        )
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.JOB_MANAGER.submit") as submit_mock:
            submit_mock.return_value = {"job_id": "job-rev", "state": "queued"}
            handler._api_post_api_v2_setup_preview_revalidate({"preview_id": preview["preview_id"]})
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 202)
        for key in ("preview_id", "revision", "job_id", "state"):
            self.assertIn(key, payload)

    def test_post_commit_returns_required_keys(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={"include_owned_uninstalled": False},
            items=[],
        )
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.JOB_MANAGER.submit") as submit_mock:
            submit_mock.return_value = {"job_id": "job-commit", "state": "queued"}
            handler._api_post_api_v2_setup_commit(
                {
                    "preview_id": preview["preview_id"],
                    "revision": preview["revision"],
                    "options": {
                        "metadata_types": [],
                        "media_types": [],
                        "region": "usa",
                        "download_limit": None,
                        "watched": False,
                        "include_owned_uninstalled": False,
                        "replace_existing": False,
                    },
                    "emulator_choices": [],
                }
            )
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 202)
        for key in ("job_id", "import_batch_id", "preview_id", "revision"):
            self.assertIn(key, payload)

    def test_scan_does_not_mutate_library_mtime(self):
        library = self.data_dir / "library.json"
        before = library.stat().st_mtime_ns
        with mock.patch("pkg.parity.parity_setup_preview.scan_sources", return_value=([], {})):
            preview = create_preview_record(
                sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
                options={},
            )
            preview["items"] = []
            save_preview(preview)
        after = library.stat().st_mtime_ns
        self.assertEqual(before, after)

    def test_expired_preview_get_raises(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
        )
        preview["expires_at"] = "2000-01-01T00:00:00+00:00"
        save_preview(preview)
        handler = DummySetupHandler()
        with self.assertRaises(PreviewExpired):
            handler._api_get_api_v2_setup_preview(mock.Mock(query=f"preview_id={preview['preview_id']}"))

    def test_parse_limit_and_preview_state_helpers(self):
        self.assertEqual(_parse_limit(None, default=50, maximum=200), 50)
        self.assertEqual(_parse_limit("300", default=50, maximum=200), 200)
        with self.assertRaises(BadRequest):
            _parse_limit("bad", default=50, maximum=200)
        self.assertEqual(_preview_state_for_job(None), "ready")
        service = get_operation_service()
        operation = service.create(operation_type="setup.scan", title="queued-job")
        self.assertEqual(_preview_state_for_job(operation["job_id"]), "queued")

    def test_preview_state_for_job_terminal_states(self):
        service = get_operation_service()
        done = service.create(operation_type="setup.scan", title="done")
        service.finish(done["job_id"], state="done")
        self.assertEqual(_preview_state_for_job(done["job_id"]), "ready")
        partial = service.create(operation_type="setup.scan", title="partial")
        service.finish(partial["job_id"], state="partial")
        self.assertEqual(_preview_state_for_job(partial["job_id"]), "ready")
        errored = service.create(operation_type="setup.scan", title="error")
        service.finish(errored["job_id"], state="error", error={"message": "boom"})
        self.assertEqual(_preview_state_for_job(errored["job_id"]), "ready")
        weird = service.create(operation_type="setup.scan", title="weird")
        with mock.patch(
            "handlers.setup.get_operation_service",
            return_value=type("S", (), {"get": lambda _self, _id: {"state": "paused", "type": "setup.scan"}})(),
        ):
            self.assertEqual(_preview_state_for_job(weird["job_id"]), "paused")

    def test_get_preview_not_found(self):
        handler = DummySetupHandler()
        with self.assertRaises(PreviewNotFound):
            handler._api_get_api_v2_setup_preview(mock.Mock(query="preview_id=missing"))

    def test_decisions_validation_errors(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
        )
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_preview_decisions({"preview_id": preview["preview_id"], "items": []})
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_preview_decisions({"preview_id": "", "items": [{"candidate_id": "c1", "action": "import"}]})

    def test_revalidate_missing_preview_id(self):
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_preview_revalidate({})

    def test_commit_missing_preview_id(self):
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_commit(
                {"preview_id": "", "revision": 1, "options": {}, "emulator_choices": []}
            )

    def test_post_handlers_invoke_workers(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
        )
        handler = DummySetupHandler()
        captured = {}

        def capture_submit(name, worker, **kwargs):
            captured["worker"] = worker
            return {"job_id": "job-worker", "state": "queued"}

        with mock.patch("handlers.setup.JOB_MANAGER.submit", side_effect=capture_submit), mock.patch(
            "handlers.setup.revalidate_preview_record",
            return_value={"revalidated": True},
        ):
            handler._api_post_api_v2_setup_preview_revalidate({"preview_id": preview["preview_id"]})
            captured["worker"](None)
            handler._api_post_api_v2_setup_commit(
                {
                    "preview_id": preview["preview_id"],
                    "revision": preview["revision"],
                    "options": {},
                    "emulator_choices": [],
                }
            )
        self.assertIn("worker", captured)

    def test_post_preview_worker_runs_scan_job(self):
        rom = self.data_dir / "worker.nes"
        rom.write_bytes(b"NES")
        handler = DummySetupHandler()
        worker_ref = {}

        def capture_submit(name, worker, **kwargs):
            worker_ref["fn"] = worker
            return {"job_id": "job-scan-worker", "state": "queued"}

        with mock.patch("handlers.setup.JOB_MANAGER.submit", side_effect=capture_submit), mock.patch(
            "handlers.setup.run_scan_job",
            return_value={"scanned_entries": 0},
        ) as scan_mock:
            handler._api_post_api_v2_setup_preview(
                {"sources": [{"type": "files", "paths": [str(rom)]}], "options": {}}
            )
            worker_ref["fn"](None)
            scan_mock.assert_called_once()

    def test_summary_live_compute_summary(self):
        handler = DummySetupHandler()
        handler._api_get_api_v2_setup_summary(mock.Mock(query=""))
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["library_count"], 4)

    def test_post_preview_validation_errors(self):
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_preview({"sources": []})
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_preview({"sources": [{"type": "files"}], "options": "bad"})

    def test_get_preview_missing_id_and_job_state(self):
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_get_api_v2_setup_preview(mock.Mock(query=""))
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
        )
        operation = get_operation_service().create(operation_type="setup.scan", title="scan-live")
        preview["job_id"] = operation["job_id"]
        preview["state"] = "queued"
        save_preview(preview)
        handler._api_get_api_v2_setup_preview(mock.Mock(query=f"preview_id={preview['preview_id']}"))
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "queued")

    def test_get_preview_items_and_decisions_handlers(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
            items=[
                {
                    "candidate_id": "c1",
                    "group": "additions",
                    "source": {"type": "files", "id": "1", "label": "g", "path": "/g.nes"},
                    "detected_title": "g",
                    "detected_platform": "NES",
                    "intended_action": "import",
                    "existing_game_target": None,
                    "warnings": [],
                    "emulator_choices": [],
                    "selected_emulator_id": None,
                    "selected_adapter_id": None,
                    "launch_setup": None,
                    "merge_diff": None,
                    "_game": {"name": "g", "platform": "NES", "path": "/g.nes"},
                    "_identity": "path:/g.nes",
                }
            ],
        )
        handler = DummySetupHandler()
        handler._api_get_api_v2_setup_preview_items(
            mock.Mock(query=f"preview_id={preview['preview_id']}&limit=10")
        )
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)
        with self.assertRaises(BadRequest):
            handler._api_get_api_v2_setup_preview_items(mock.Mock(query=""))
        handler._api_post_api_v2_setup_preview_decisions(
            {
                "preview_id": preview["preview_id"],
                "items": [
                    {
                        "candidate_id": "c1",
                        "action": "import",
                        "merge_target": None,
                        "emulator_id": None,
                        "adapter_id": None,
                        "launch_setup": None,
                    }
                ],
            }
        )
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertEqual(payload["accepted"], 1)

    def test_post_commit_validation_errors(self):
        handler = DummySetupHandler()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_commit({"preview_id": "", "revision": 1, "options": {}, "emulator_choices": []})
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
        )
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_setup_commit(
                {"preview_id": preview["preview_id"], "revision": 1, "options": {}, "emulator_choices": "bad"}
            )

    def test_post_revalidate_runs_worker(self):
        preview = create_preview_record(
            sources=[{"type": "files", "id": "f1", "paths": ["/x.nes"]}],
            options={},
            source_fingerprints={"0:files:/x": "fp"},
            items=[],
        )
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.JOB_MANAGER.submit") as submit_mock, mock.patch(
            "handlers.setup.revalidate_preview_record",
            return_value={"revalidated": True},
        ):
            submit_mock.side_effect = lambda name, worker, **kwargs: {"job_id": "job-r", "state": "queued"}
            handler._api_post_api_v2_setup_preview_revalidate({"preview_id": preview["preview_id"]})
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 202)
        self.assertEqual(payload["job_id"], "job-r")

    def test_post_preview_without_mocked_scan_job(self):
        rom = self.data_dir / "handler.nes"
        rom.write_bytes(b"NES")
        handler = DummySetupHandler()
        with mock.patch("handlers.setup.JOB_MANAGER.submit") as submit_mock:
            submit_mock.return_value = {"job_id": "job-scan", "state": "queued"}
            handler._api_post_api_v2_setup_preview(
                {
                    "sources": [{"type": "files", "paths": [str(rom)]}],
                    "options": {"include_owned_uninstalled": False},
                }
            )
        status, payload, _kwargs = handler.responses[-1]
        self.assertEqual(status, 202)
        self.assertIn("preview_id", payload)


if __name__ == "__main__":
    unittest.main()
