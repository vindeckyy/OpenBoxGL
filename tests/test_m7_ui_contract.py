#!/usr/bin/env python3
"""M7 UI wiring contracts for the P5 feature surface.

The JS dialogs build their own markup, so these structural checks keep the
routes, entry points, and i18n'd controls connected without a browser.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "static"


class M7UiWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = (STATIC / "library.js").read_text(encoding="utf-8")
        cls.sessions = (STATIC / "sessions.js").read_text(encoding="utf-8")
        cls.settings = (STATIC / "settings.js").read_text(encoding="utf-8")
        cls.palette = (STATIC / "palette.js").read_text(encoding="utf-8")

    def test_repair_wizard_hits_the_repair_routes(self):
        self.assertIn("export function openRepairWizard()", self.library)
        self.assertIn("/api/v2/library/repair/preview", self.library)
        self.assertIn("/api/v2/library/repair/apply", self.library)
        self.assertIn("repair.find_folder", self.library)

    def test_duplicate_merge_previews_and_merges(self):
        self.assertIn("export function openDuplicatesDialog()", self.library)
        self.assertIn("/api/v2/library/duplicates/preview", self.library)
        self.assertIn("/api/v2/library/duplicates/merge", self.library)
        self.assertIn("duplicates.restore_hint", self.library)

    def test_health_dialog_exposes_both_tools(self):
        self.assertIn("ensureHealthTools", self.settings)
        self.assertIn("repairButton", self.settings)
        self.assertIn("duplicatesButton", self.settings)
        self.assertIn("openDuplicatesDialog", self.settings)

    def test_sessions_ui_has_test_restore_and_export(self):
        self.assertIn("/api/v2/saves/history/test-restore", self.sessions)
        self.assertIn("data-save-drill", self.sessions)
        self.assertIn("/api/v2/sessions/export", self.sessions)
        self.assertIn("historyExportScope", self.sessions)

    def test_collection_transfer_sits_with_library_export(self):
        self.assertIn("ensureCollectionTransfer", self.settings)
        self.assertIn("/api/v2/collections/export", self.settings)
        self.assertIn("/api/v2/collections/import", self.settings)

    def test_shortcut_cheat_sheet_groups_the_shared_table(self):
        self.assertIn("openShortcutCheatSheet", self.palette)
        self.assertIn("shortcutsDialog", self.palette)
        self.assertIn("shortcut-row", self.palette)
        navigation = (STATIC / "navigation.js").read_text(encoding="utf-8")
        self.assertIn("group: 'shortcuts.group_library'", navigation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
