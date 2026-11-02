"""Starting OpenBox as a script must load decorator-only routes.

The server is normally imported as ``web_app`` in tests, but users start it as
``python web_app.py``. In that mode the module is ``__main__``; importing
``web_app`` again executes a second copy. Regression pin for the decorator
registry loading under the script entrypoint.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ScriptEntrypointTests(unittest.TestCase):
    def test_decorator_only_version_route_answers_when_started_as_a_script(self):
        with tempfile.TemporaryDirectory() as data_dir:
            env = dict(os.environ)
            env["OPENBOX_DATA_DIR"] = data_dir
            env["OPENBOX_DISABLE_EXTERNAL_OPEN"] = "1"
            process = subprocess.Popen(
                [sys.executable, "-B", "web_app.py", "--no-browser"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                port_file = Path(data_dir) / "server.port"
                token_file = Path(data_dir) / "server.token"
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and not (port_file.is_file() and token_file.is_file()):
                    if process.poll() is not None:
                        self.fail(f"server exited during startup: {process.stderr.read()[-2000:]}")
                    time.sleep(0.1)
                self.assertTrue(port_file.is_file() and token_file.is_file(), "server did not publish port/token")
                port = port_file.read_text(encoding="utf-8").strip()
                token = token_file.read_text(encoding="utf-8").strip()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/v2/version",
                    headers={"X-OpenBox-Token": token},
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["api_version"], 2)
                self.assertEqual(payload["state_schema"], 7)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
