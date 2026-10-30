#!/usr/bin/env python3
"""Compatibility report (plan W6-02): each section names its items with a disposition, deterministically.

The report is read-only. Two runs over the same data are equal, because it carries no timestamps.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_compat as compat  # noqa: E402


def write_archive(path: Path, declared_format):
    manifest = {} if declared_format is None else {"format": declared_format}
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("manifest.json", json.dumps(manifest))
        package.writestr("library.json", "{}")


class CompatReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "app"
        (self.app / "static").mkdir(parents=True)
        (self.app / "static" / "app.css").write_text(":root { --brand: #000; --focus: #111; }", encoding="utf-8")
        self.data = Path(self.tmp.name) / "data"
        (self.data / "themes").mkdir(parents=True)
        (self.data / "backups").mkdir(parents=True)
        (self.data / "themes" / "custom.css").write_text(":root { --brand: #222; }", encoding="utf-8")
        state = {
            "schema_version": 7,
            "path_mode": "portable",
            "library_root": "/games",
            "settings": {"theme": "dark", "cloud_folder": "", "bogus_key": 1},
        }
        (self.data / "library.json").write_text(json.dumps(state), encoding="utf-8")

    def test_each_section_names_its_items_with_a_disposition(self):
        write_archive(self.data / "backups" / "v2.zip", 2)
        write_archive(self.data / "backups" / "v1.zip", None)
        report = compat.build_report(self.data, root=self.app)
        self.assertEqual(report["library"]["schema_version"], 7)
        self.assertEqual(report["library"]["path_mode"], "portable")
        self.assertEqual(report["themes"][0]["name"], "custom")
        self.assertEqual(report["themes"][0]["missing"], ["--focus"])
        dispositions = {row["name"]: row["disposition"] for row in report["backups"]}
        self.assertEqual(dispositions["v2.zip"], "2.0-only")
        self.assertEqual(dispositions["v1.zip"], "both")
        self.assertEqual(report["settings"]["unknown_keys"], ["bogus_key"])

    def test_two_runs_over_the_same_data_are_equal(self):
        write_archive(self.data / "backups" / "v2.zip", 2)
        self.assertEqual(compat.build_report(self.data, root=self.app), compat.build_report(self.data, root=self.app))

    def test_the_v1_bridge_flag_follows_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENBOX_ENABLE_V1_API": "1"}):
            self.assertTrue(compat.build_report(self.data, root=self.app)["api"]["v1_bridge"])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(compat.build_report(self.data, root=self.app)["api"]["v1_bridge"])

    def test_a_configured_sync_folder_is_reported_as_found_when_it_exists(self):
        state = json.loads((self.data / "library.json").read_text(encoding="utf-8"))
        state["settings"]["cloud_folder"] = str(self.data)
        (self.data / "library.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(compat.build_report(self.data, root=self.app)["sync"], {"configured": True, "found": True})


class PluginSectionTests(CompatReportTests):
    def test_the_report_names_each_plugin_with_its_api_version_and_disposition(self):
        plugins = self.data / "plugins"
        plugins.mkdir()
        shutil.copytree(ROOT / "plugins" / "openbox.sample-v2", plugins / "openbox.sample-v2")
        report = compat.build_report(self.data, root=self.app)
        rows = {row["id"]: row for row in report["plugins"]}
        self.assertIn("openbox.sample-v2", rows)
        self.assertEqual(rows["openbox.sample-v2"]["api_version"], 2)
        self.assertEqual(rows["openbox.sample-v2"]["disposition"], "compatible")
        self.assertTrue(rows["openbox.sample-v2"]["valid"])


if __name__ == "__main__":
    unittest.main()
