#!/usr/bin/env python3
"""P6: structural contract for the state.js toast queue.

Concurrent notifications used to overwrite the single #toast element. The queue
now keeps at most three toasts visible in a dynamically created, stacked host,
preserves dataset.notifyLevel styling, and auto-dismisses each toast with the
existing 2800ms timer.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE_JS = ROOT / "static" / "state.js"


class ToastQueueStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = STATE_JS.read_text(encoding="utf-8")
        cls.notify_body = cls.source[
            cls.source.index("function notify(") : cls.source.index("function notifyError(")
        ]

    def test_bounded_stacked_queue(self):
        self.assertIn("NOTIFY_MAX_VISIBLE = 3", self.source)
        self.assertIn("column-reverse", self.source)
        self.assertIn("notifyQueue.push", self.source)
        self.assertIn("notifyQueue.shift", self.source)

    def test_dynamic_host_instead_of_single_toast(self):
        self.assertIn("createElement('div')", self.source)
        self.assertIn("id = 'toastQueue'", self.source)
        self.assertIn("querySelectorAll('.toast')", self.source)

    def test_level_styling_and_timer_preserved(self):
        self.assertIn("toast.dataset.notifyLevel = level", self.source)
        self.assertIn("NOTIFY_DURATION_MS = 2800", self.source)

    def test_notify_never_writes_the_legacy_toast(self):
        self.assertNotIn("$('toast')", self.notify_body)
        self.assertNotIn("notify.timer", self.source)

    def test_exported_api_unchanged(self):
        self.assertIn("function notifyError(", self.source)
        self.assertIn("notify, notifyAction, notifyError", self.source)

    def test_notify_action_supports_undo_buttons(self):
        self.assertIn("function notifyAction(", self.source)
        self.assertIn("action.onAction", self.source)
        exported = self.source[self.source.index("export {"):]
        self.assertIn("notifyAction", exported)

    def test_trash_undo_uses_the_queue_and_defers_purge(self):
        library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
        self.assertIn("notifyAction(t('trash.moved'", library)
        self.assertIn("scheduleTrashPurge", library)
        self.assertIn("trash.purge_pending", library)
        self.assertNotIn("$('toast')", library)


if __name__ == "__main__":
    unittest.main(verbosity=2)
