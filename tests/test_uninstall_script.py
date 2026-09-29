#!/usr/bin/env python3
"""scripts/uninstall.ps1 removes exactly what install.ps1 and updates.py create, and nothing else."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UNINSTALL = ROOT / "scripts" / "uninstall.ps1"
INSTALL = ROOT / "scripts" / "install.ps1"


class UninstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.uninstall = UNINSTALL.read_text(encoding="utf-8")
        self.install = INSTALL.read_text(encoding="utf-8")

    def test_removes_every_location_the_installer_creates(self):
        # The install tree layout must be the installer's own.
        for fragment in ("'share'", "'openbox'", "'openbox.previous'", "'OpenBox'"):
            self.assertIn(fragment, self.install)
            self.assertIn(fragment, self.uninstall)
        # PATH entry, Start Menu shortcut and protocol registration.
        self.assertIn("SetEnvironmentVariable('Path'", self.uninstall)
        self.assertIn("OpenBox.lnk", self.uninstall)
        self.assertIn(r"HKCU:\Software\Classes\openbox", self.uninstall)

    def test_shortcut_and_protocol_names_match_the_registering_code(self):
        source = (ROOT / "updates.py").read_text(encoding="utf-8")
        self.assertIn('programs / "OpenBox.lnk"', source)
        self.assertIn(r'Software\Classes\openbox', source)

    def test_never_touches_the_data_directory(self):
        # Library and settings survive an uninstall: the script must not resolve or remove them.
        for token in ("OPENBOX_DATA_DIR", "library.json", "$env:APPDATA", "RemoveData"):
            self.assertNotIn(token, self.uninstall)

    def test_dry_run_removes_nothing(self):
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell or sys.platform != "win32":
            self.skipTest("needs Windows PowerShell")
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory) / "share" / "openbox"
            previous = Path(directory) / "share" / "openbox.previous"
            tree.mkdir(parents=True)
            previous.mkdir()
            (tree / "web_app.py").write_text("x", encoding="utf-8")
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(UNINSTALL), "-InstallDir", directory, "-WhatIf"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(tree.is_dir(), "-WhatIf must not delete the install tree")
            self.assertTrue(previous.is_dir(), "-WhatIf must not delete the rollback copy")
            self.assertIn("What if", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
