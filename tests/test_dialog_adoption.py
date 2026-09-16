#!/usr/bin/env python3
"""P0/P7 contract: every dialog routes through dialogs.js openDialog().

Raw dialog.showModal() bypasses the focus/trigger bookkeeping in dialogs.js.
This structural guard keeps new dialogs from reintroducing the pattern.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATIC = ROOT / "static"
SHOW_MODAL_RE = re.compile(r"\.showModal\s*\(")


class DialogAdoptionTests(unittest.TestCase):
    def test_no_raw_show_modal_outside_dialogs_module(self):
        offenders = []
        for path in sorted(STATIC.glob("*.js")):
            if path.name == "dialogs.js":
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if SHOW_MODAL_RE.search(line):
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [], f"raw showModal outside dialogs.js: {offenders}")

    def test_dialogs_module_owns_the_only_show_modal_call(self):
        source = (STATIC / "dialogs.js").read_text(encoding="utf-8")
        self.assertIn("dialog.showModal()", source)
        self.assertIn("function openDialog(", source)
        self.assertIn("dialogTriggers.set(dialog, opener)", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
