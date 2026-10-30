#!/usr/bin/env python3
"""Windows ARM64 artifact selection (plan W5-03, W5-04, ADR 0095).

The updater names the ARM64 artifact OpenBox-arm64-windows.zip and never offers an x86_64 zip to
an ARM64 host. The installer resolves the architecture from the requested value and the machine,
refuses a request that does not match the machine, and selects ARM64 on an ARM64 machine now
that the release publishes the artifact. The installer check runs the real Resolve-Arch function
from scripts/install.ps1 under PowerShell 7 when it is installed; without PowerShell that case
is skipped, not passed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import updates  # noqa: E402

PWSH = os.environ.get("OPENBOX_PWSH") or shutil.which("pwsh")

HARNESS = r"""
param([string]$Arch = '', [string]$Machine = 'AMD64')
$ErrorActionPreference = 'Stop'
function Die([string]$m) { throw $m }
$env:PROCESSOR_ARCHITECTURE = $Machine
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__INSTALL__', [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resolve-Arch' }, $true)
Invoke-Expression $fn.Extent.Text
try { Write-Output (Resolve-Arch) } catch { Write-Output ("refused: " + $_.Exception.Message) }
"""


class WindowsArtifactNameTests(unittest.TestCase):
    def test_an_arm64_windows_host_asks_for_the_arm64_zip(self):
        with mock.patch.object(updates, "IS_WINDOWS", True), mock.patch.object(updates, "IS_MACOS", False):
            self.assertEqual(updates._arch_asset(updates._current_arch("arm64")), "OpenBox-arm64-windows.zip")

    def test_an_x86_64_windows_host_never_asks_for_the_arm64_zip(self):
        with mock.patch.object(updates, "IS_WINDOWS", True), mock.patch.object(updates, "IS_MACOS", False):
            self.assertEqual(updates._arch_asset(updates._current_arch("AMD64")), "OpenBox-x86_64-windows.zip")


@unittest.skipUnless(PWSH, "PowerShell 7 is not installed (set OPENBOX_PWSH or put pwsh on PATH)")
class InstallerArchitectureTests(unittest.TestCase):
    def resolve(self, arch: str, machine: str) -> str:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "resolve.ps1"
            script.write_text(HARNESS.replace("__INSTALL__", str(ROOT / "scripts" / "install.ps1")), encoding="utf-8")
            args = [PWSH, "-NoProfile", "-File", str(script), "-Machine", machine]
            if arch:
                args += ["-Arch", arch]
            done = subprocess.run(args, capture_output=True, text=True, timeout=120, check=False)
            return (done.stdout.strip().splitlines() or [done.stderr.strip()])[-1]

    def test_a_matching_x86_64_request_selects_x86_64(self):
        self.assertEqual(self.resolve("x86_64", "AMD64"), "x86_64")

    def test_an_arm64_host_selects_arm64(self):
        self.assertEqual(self.resolve("", "ARM64"), "arm64")

    def test_a_request_that_does_not_match_the_machine_is_refused(self):
        self.assertIn("does not match it", self.resolve("x86_64", "ARM64"))


if __name__ == "__main__":
    unittest.main()
