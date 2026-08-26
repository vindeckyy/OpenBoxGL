"""Crash report packager tests."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openbox_logging
from crash_report import (
    build_preview,
    build_report,
    disk_space_facts,
    install_channel,
    package_integrity_facts,
    recent_request_ids_from_log,
    system_facts,
    tokenize_home_paths,
)


class CrashReportTests(unittest.TestCase):
    def test_report_is_valid_json_with_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(directory, include_log=False)
            payload = json.loads(report)
            self.assertEqual(payload["report"], "openbox-diagnostic")
            self.assertIn("system", payload)
            self.assertIn("version", payload)
            self.assertEqual(payload["diagnostic_log"], "")

    def test_preview_has_required_fields_without_home_paths(self):
        home = os.path.expanduser("~")
        with tempfile.TemporaryDirectory(dir=home) as directory:
            library = os.path.join(directory, "library.json")
            with open(library, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 6, "games": [{"name": "A"}, {"name": "B"}]}, handle)
            preview = build_preview(directory, recent_job_ids=["job-abc"], recent_request_ids=["req-123"])
            self.assertEqual(preview["report"], "openbox-diagnostic")
            for key in (
                "version",
                "install_channel",
                "architecture",
                "distro",
                "desktop_session",
                "native_host",
                "renderer_flags",
                "library_count",
                "schema_version",
                "disk_space",
                "package_integrity",
                "recent_job_ids",
                "recent_request_ids",
            ):
                self.assertIn(key, preview, msg=key)
            encoded = json.dumps(preview)
            self.assertNotIn(home, encoded)
            self.assertNotIn("/home/", encoded)

    def test_tokenize_home_paths_replaces_username_segments(self):
        home = os.path.expanduser("~")
        tokenized = tokenize_home_paths({"path": f"{home}/games/rom.iso"})
        self.assertNotIn("/home/", tokenized["path"])
        self.assertIn("~/", tokenized["path"])

    def test_build_preview_makes_no_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("urllib.request.urlopen") as urlopen:
                build_preview(directory)
            urlopen.assert_not_called()

    def test_install_channel_and_package_integrity_branches(self):
        with mock.patch.dict(os.environ, {"FLATPAK_ID": "io.openbox.GameLauncher"}, clear=False):
            self.assertEqual(install_channel(), "flatpak")
        with tempfile.TemporaryDirectory() as directory:
            appimage = os.path.join(directory, "OpenBox.AppImage")
            with open(appimage, "w", encoding="utf-8") as handle:
                handle.write("appimage")
            with mock.patch.dict(os.environ, {"APPIMAGE": appimage}, clear=False):
                self.assertEqual(install_channel(), "appimage")
                self.assertEqual(package_integrity_facts()["reason"], "manifest_missing")
                manifest = os.path.join(directory, "sbom-manifest.json")
                with open(manifest, "w", encoding="utf-8") as handle:
                    handle.write("{not-json")
                self.assertEqual(package_integrity_facts()["status"], "failed")
                with open(manifest, "w", encoding="utf-8") as handle:
                    json.dump({"file_count": 3}, handle)
                self.assertEqual(package_integrity_facts()["file_count"], 3)

    def test_disk_space_handles_os_error(self):
        with mock.patch("crash_report.shutil.disk_usage", side_effect=OSError("denied")):
            self.assertEqual(disk_space_facts("/tmp"), {"total_bytes": 0, "free_bytes": 0})

    def test_recent_request_ids_from_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = openbox_logging.diagnostic_log_path(directory)
            log_path.write_text(
                "HTTP GET /api/library started [aabbccdd]\nHTTP GET /api/jobs started [11223344]\n",
                encoding="utf-8",
            )
            self.assertEqual(recent_request_ids_from_log(directory), ["aabbccdd", "11223344"])

    def test_recent_request_ids_from_log_respects_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            lines = [f"HTTP GET /api/library started [{index:08x}]\n" for index in range(12)]
            openbox_logging.diagnostic_log_path(directory).write_text("".join(lines), encoding="utf-8")
            self.assertEqual(len(recent_request_ids_from_log(directory, limit=10)), 10)

    def test_log_section_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = openbox_logging.configure_logging(directory)
            logger.warning("Credentials token=supersecret-value were rejected")
            report = build_report(directory, include_log=True)
            self.assertNotIn("supersecret-value", report)

    def test_missing_library_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = json.loads(build_report(directory, include_log=False))
            self.assertEqual(payload["system"]["library_bytes"], 0)

    def test_system_facts_shape(self):
        facts = system_facts()
        for key in ("python", "platform", "machine", "version"):
            self.assertIn(key, facts)


if __name__ == "__main__":
    unittest.main()
