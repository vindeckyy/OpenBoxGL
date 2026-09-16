#!/usr/bin/env python3
"""P1-11: operations item files are pruned with their records and appended under a lock."""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.state.operations import MAX_OPERATIONS, OperationService, operation_items_path


class OperationItemsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_path = Path(self.tempdir.name) / "library.json"
        self.data_path.write_text("{}", encoding="utf-8")
        self.service = OperationService(self.data_path)

    def _age_record(self, job_id):
        record = self.service.get(job_id)
        record["finished_at"] = "2000-01-01T00:00:00+00:00"
        record["state"] = "done"
        with self.service._lock:
            self.service._operations[job_id] = self.service._snapshot(record)

    def test_items_file_pruned_with_aged_record(self):
        op = self.service.create(operation_type="setup.scan", title="aged")
        self.service.add_item_failure(op["job_id"], item_id="1", label="one", state="error")
        items_path = operation_items_path(op["job_id"], self.data_path)
        self.assertTrue(items_path.is_file())

        self._age_record(op["job_id"])
        self.assertTrue(self.service.persist())

        self.assertFalse(items_path.exists())
        self.assertIsNone(self.service.get(op["job_id"]))

    def test_items_file_pruned_with_overflow_record(self):
        old = self.service.create(operation_type="setup.scan", title="old-overflow")
        self.service.add_item_failure(old["job_id"], item_id="1", label="one", state="error")
        old_items = operation_items_path(old["job_id"], self.data_path)
        self.assertTrue(old_items.is_file())
        self.service.finish(old["job_id"], state="done")

        for index in range(MAX_OPERATIONS):
            op = self.service.create(operation_type="setup.scan", title=f"fill-{index}")
            self.service.finish(op["job_id"], state="done")

        self.assertFalse(old_items.exists())
        self.assertIsNone(self.service.get(old["job_id"]))

    def test_concurrent_item_appends_are_not_lost(self):
        op = self.service.create(operation_type="setup.scan", title="concurrent")
        count = 8
        started = threading.Barrier(count)
        original_read_text = Path.read_text

        def slow_read_text(path, *args, **kwargs):
            if path.name == f"{op['job_id']}.json":
                time.sleep(0.02)
            return original_read_text(path, *args, **kwargs)

        def worker(index):
            started.wait(timeout=10)
            self.service.add_item_failure(
                op["job_id"], item_id=f"item-{index}", label=str(index), state="error"
            )

        with mock.patch.object(Path, "read_text", slow_read_text):
            threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        payload = json.loads(
            operation_items_path(op["job_id"], self.data_path).read_text(encoding="utf-8")
        )
        self.assertEqual({item["item_id"] for item in payload["items"]}, {f"item-{index}" for index in range(count)})
        self.assertEqual(len(self.service.list_items(op["job_id"], limit=count)["items"]), count)

    def test_corrupt_items_file_is_replaced(self):
        op = self.service.create(operation_type="setup.scan", title="corrupt")
        items_path = operation_items_path(op["job_id"], self.data_path)
        items_path.parent.mkdir(parents=True, exist_ok=True)
        items_path.write_text("{not json", encoding="utf-8")

        entry = self.service.add_item_failure(op["job_id"], item_id="1", label="one", state="error")

        self.assertEqual(entry["item_id"], "1")
        payload = json.loads(items_path.read_text(encoding="utf-8"))
        self.assertEqual([item["item_id"] for item in payload["items"]], ["1"])

    def test_items_write_failure_does_not_raise(self):
        op = self.service.create(operation_type="setup.scan", title="disk-full")
        with mock.patch("pkg.state.operations.atomic_write_text", side_effect=OSError("disk full")):
            entry = self.service.add_item_failure(op["job_id"], item_id="1", label="one", state="error")
        self.assertEqual(entry["item_id"], "1")

    def test_prune_unlink_failure_does_not_raise(self):
        op = self.service.create(operation_type="setup.scan", title="locked")
        self.service.add_item_failure(op["job_id"], item_id="1", label="one", state="error")
        self._age_record(op["job_id"])
        items_path = operation_items_path(op["job_id"], self.data_path)
        original_unlink = Path.unlink

        def failing_unlink(path_self, *args, **kwargs):
            if path_self == items_path:
                raise OSError("locked")
            return original_unlink(path_self, *args, **kwargs)

        with mock.patch.object(Path, "unlink", failing_unlink):
            self.assertTrue(self.service.persist())
        self.assertTrue(items_path.is_file())


if __name__ == "__main__":
    unittest.main()
