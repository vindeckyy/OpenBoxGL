#!/usr/bin/env python3
"""P7 contract: the virtualized grid uses the M6 ARIA helpers.

The grid rows/cells must expose gridcell roles with filtered-order row/col
indices, and aria-selected must not land on role-less elements.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIBRARY = ROOT / "static" / "library.js"
UTIL = ROOT / "static" / "util.js"


class GridA11yStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = LIBRARY.read_text(encoding="utf-8")
        cls.util = UTIL.read_text(encoding="utf-8")

    def test_helpers_are_exported_and_imported(self):
        self.assertIn("function applyGridA11y(", self.util)
        self.assertIn("function gridCellAttrs(", self.util)
        self.assertIn("applyGridA11y, gridCellAttrs", self.util)
        self.assertIn("applyGridA11y, gridCellAttrs", self.library)

    def test_cards_are_rows_and_cells(self):
        card = self.library[self.library.index("function gridCardHTML(") :]
        card = card[: card.index("</article>`;")]
        self.assertIn('role="row"', card)
        self.assertIn("gridCellAttrs({ index, columns, selected })", card)

    def test_list_rows_use_gridcell_attrs(self):
        self.assertIn("gridCellAttrs({index: absoluteIndex, columns: 1, size: total, position: absoluteIndex + 1", self.library)

    def test_container_geometry_is_applied(self):
        self.assertIn("applyGridA11y($('grid')", self.library)
        self.assertIn("a11yRows", self.library)

    def test_grouped_geometry_keeps_absolute_start(self):
        self.assertIn("start:gameIndex", self.library)
        self.assertIn("row.start + i", self.library)

    def test_aria_selected_requires_a_role(self):
        block = self.library[self.library.index("function markFilterAria()") :]
        block = block[: block.index("async function refresh()")]
        self.assertIn("if (card.getAttribute('role'))", block)
        self.assertIn("removeAttribute('aria-selected')", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
