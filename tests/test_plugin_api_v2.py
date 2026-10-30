#!/usr/bin/env python3
"""Plugin API v2 (plan W9-01 to W9-05, ADR 0092).

A v2 manifest declares its API version, the oldest OpenBox that serves it, and the events it
subscribes to. A v2 plugin is served by the JSON Lines runner. A v1 plugin keeps the v1 runner
and the v1 payload shapes: nothing about a v1 package changes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins import read_manifest, run_plugin_hook  # noqa: E402

RUNNER_V2 = ROOT / "plugin_runner_v2.py"
ECHO_HOOK = "def before_launch(payload):\n    payload['args'].append('--ran')\n    return payload\n"


def write_package(root: Path, plugin_id: str, manifest: dict, source: str) -> Path:
    package = root / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "plugin.py").write_text(source, encoding="utf-8")
    return package


def v2_manifest(**overrides):
    manifest = {
        "id": "v2.plugin",
        "name": "V2",
        "version": "1",
        "entry": "plugin.py",
        "hooks": ["before_launch"],
        "api_version": 2,
    }
    manifest.update(overrides)
    return manifest


class ManifestV2Tests(unittest.TestCase):
    def read(self, manifest):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_package(root, "v2.plugin", manifest, ECHO_HOOK)
            return read_manifest(root / "v2.plugin")

    def test_a_v2_manifest_is_accepted(self):
        manifest = self.read(v2_manifest(min_openbox="1.19.0", subscriptions=["session.ended"]))
        self.assertEqual(manifest["api_version"], 2)
        self.assertEqual(manifest["min_openbox"], "1.19.0")
        self.assertEqual(manifest["subscriptions"], ["session.ended"])

    def test_a_v1_manifest_stays_v1(self):
        manifest = self.read({"id": "v2.plugin", "name": "V1", "version": "1", "entry": "plugin.py", "hooks": ["before_launch"]})
        self.assertEqual(manifest["api_version"], 1)
        self.assertEqual(manifest["min_openbox"], "")
        self.assertEqual(manifest["subscriptions"], [])

    def test_an_unknown_api_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, "requires API v3"):
            self.read(v2_manifest(api_version=3))

    def test_a_min_openbox_newer_than_this_build_is_refused(self):
        with self.assertRaisesRegex(ValueError, "or newer"):
            self.read(v2_manifest(min_openbox="99.0.0"))

    def test_a_min_openbox_that_is_not_a_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, "version number"):
            self.read(v2_manifest(min_openbox="soon"))

    def test_an_unknown_subscription_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown event"):
            self.read(v2_manifest(subscriptions=["library.exploded"]))

    def test_v2_only_fields_on_a_v1_manifest_are_refused(self):
        with self.assertRaisesRegex(ValueError, "min_openbox needs plugin api_version 2"):
            self.read({"id": "v2.plugin", "name": "V1", "version": "1", "entry": "plugin.py", "hooks": ["before_launch"], "min_openbox": "1.0.0"})


class RunnerV2Tests(unittest.TestCase):
    def run_runner(self, requests, source=ECHO_HOOK):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "plugin.py"
            entry.write_text(source, encoding="utf-8")
            payload = "".join(json.dumps(request) + "\n" for request in requests).encode("utf-8")
            return subprocess.run(
                [sys.executable, "-B", str(RUNNER_V2), str(entry)],
                input=payload, capture_output=True, check=False, timeout=60,
            )

    def test_a_request_is_answered_with_a_result_line(self):
        completed = self.run_runner([{"id": "1", "type": "invoke", "hook": "before_launch", "payload": {"args": []}}])
        message = json.loads(completed.stdout.decode().splitlines()[0])
        self.assertEqual(message["type"], "result")
        self.assertEqual(message["result"]["args"], ["--ran"])

    def test_a_session_answers_every_request(self):
        completed = self.run_runner([
            {"id": "1", "type": "invoke", "hook": "before_launch", "payload": {"args": []}},
            {"id": "2", "type": "invoke", "hook": "before_launch", "payload": {"args": []}},
        ])
        lines = [json.loads(line) for line in completed.stdout.decode().splitlines() if line.strip()]
        self.assertEqual([message["id"] for message in lines], ["1", "2"])
        self.assertTrue(all(message["type"] == "result" for message in lines))

    def test_a_cancel_answers_the_cancelled_request(self):
        completed = self.run_runner([
            {"id": "1", "type": "cancel"},
            {"id": "2", "type": "invoke", "hook": "before_launch", "payload": {"args": []}},
        ])
        lines = [json.loads(line) for line in completed.stdout.decode().splitlines() if line.strip()]
        self.assertEqual(lines[0]["type"], "cancelled")
        self.assertEqual(lines[1]["type"], "cancelled")

    def test_a_failing_hook_answers_with_an_error(self):
        completed = self.run_runner(
            [{"id": "1", "type": "invoke", "hook": "before_launch", "payload": {}}],
            source="def before_launch(payload):\n    raise ValueError('nope')\n",
        )
        message = json.loads(completed.stdout.decode().splitlines()[0])
        self.assertEqual(message["type"], "error")
        self.assertIn("nope", message["error"])

    def test_output_beyond_the_cap_stops_the_runner(self):
        completed = self.run_runner(
            [{"id": "1", "type": "invoke", "hook": "before_launch", "payload": {}}],
            source="def before_launch(payload):\n    return {'blob': 'x' * (5 * 1024 * 1024)}\n",
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PLUGIN_OUTPUT_TOO_LARGE", completed.stdout.decode())


class HostV2Tests(unittest.TestCase):
    def test_the_host_serves_a_v2_plugin_through_the_json_lines_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_package(root, "v2.plugin", v2_manifest(), ECHO_HOOK)
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
                result, error = run_plugin_hook(root, "v2.plugin", "before_launch", {"args": []})
            self.assertEqual(error, "")
            self.assertEqual(result["args"], ["--ran"])

    def test_a_v1_plugin_still_runs_with_the_v1_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_package(root, "v1.plugin", {
                "id": "v1.plugin", "name": "V1", "version": "1", "entry": "plugin.py", "hooks": ["before_launch"],
            }, ECHO_HOOK)
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
                result, error = run_plugin_hook(root, "v1.plugin", "before_launch", {"args": []})
            self.assertEqual(error, "")
            self.assertEqual(result["args"], ["--ran"])

    def test_a_v2_plugin_error_reaches_the_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_package(root, "v2.plugin", v2_manifest(), "def before_launch(payload):\n    raise ValueError('boom')\n")
            with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
                result, error = run_plugin_hook(root, "v2.plugin", "before_launch", {"args": []})
            self.assertIsNone(result)
            self.assertIn("boom", error)


class SandboxV2Tests(unittest.TestCase):
    def test_a_v2_plugin_uses_the_same_masks_and_its_own_runner(self):
        import plugins
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "p"
            package.mkdir()
            entry = package / "plugin.py"
            entry.write_text(ECHO_HOOK, encoding="utf-8")
            # The command is only built when bubblewrap is present, so stub the lookup.
            with mock.patch.object(plugins.shutil, "which", return_value="/usr/bin/bwrap"):
                v1 = plugins._sandboxed_command(package, entry, "before_launch")
                v2 = plugins._sandboxed_command(package, entry, "before_launch", api_version=2)
        masks = ("--tmpfs", "--proc", "--dev", "--ro-bind")
        self.assertEqual([part for part in v2 if part in masks], [part for part in v1 if part in masks])
        self.assertIn("plugin_runner.py", " ".join(v1))
        self.assertIn("plugin_runner_v2.py", " ".join(v2))
        self.assertNotIn("plugin_runner.py", " ".join(v2))
        # The v2 runner learns the hook from the request, so the command carries no hook name.
        self.assertNotIn("before_launch", v2)


if __name__ == "__main__":
    unittest.main()
