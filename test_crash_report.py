"""Crash report packager tests."""

import json
import unittest
import tempfile

import openbox_logging
from crash_report import build_report, system_facts


class CrashReportTests(unittest.TestCase):
    def test_report_is_valid_json_with_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            report = build_report(directory, include_log=False)
            payload = json.loads(report)
            self.assertEqual(payload["report"], "openbox-diagnostic")
            self.assertIn("system", payload)
            self.assertIn("version", payload)
            self.assertEqual(payload["diagnostic_log"], "")

    def test_log_section_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = openbox_logging.configure_logging(directory)
            logger.warning("Credentials token=supersecret-value were rejected")
            report = build_report(directory)
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
