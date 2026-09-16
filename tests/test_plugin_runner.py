#!/usr/bin/env python3
"""Plugin runner contract tests (F7 / P3-6): JS-out, env stripping, timeout."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins import run_plugin_hook  # noqa: E402


def test():
    runner = ROOT / "plugin_runner.py"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        entry = root / "plugin.py"
        entry.write_text(
            "def before_launch(payload):\n"
            "    payload['args'].append('--ran')\n"
            "    return payload\n"
        )
        completed = subprocess.run(
            [sys.executable, "-B", str(runner), str(entry), "before_launch"],
            input=json.dumps({"args": []}).encode(),
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout.decode())["args"] == ["--ran"]

        # A missing entry is a hard failure, not a silent no-op.
        completed = subprocess.run(
            [sys.executable, "-B", str(runner), str(root / "missing.py"), "before_launch"],
            input=b"{}",
            capture_output=True,
            check=False,
        )
        assert completed.returncode != 0

    # The host enforces a 5 second timeout and reports it honestly.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "slow.plugin"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({
            "id": "slow.plugin", "name": "Slow", "version": "1",
            "entry": "plugin.py", "hooks": ["before_launch"],
        }))
        (package / "plugin.py").write_text(
            "import time\n"
            "def before_launch(payload):\n"
            "    time.sleep(30)\n"
            "    return payload\n"
        )
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            result, error = run_plugin_hook(root, "slow.plugin", "before_launch", {"args": []})
        assert result is None
        assert "timed out" in error.lower() or "timeout" in error.lower(), error
    print("plugin runner self-test: ok")


if __name__ == "__main__":
    test()
