#!/usr/bin/env python3
"""Smoke test for the macOS app bundle.

Checks the four things that break silently when the bundle is assembled
wrong: the bundled interpreter, the runtime payload, the loopback server the
host boots, and the host's own self test. The bundle is also exercised from a
path with a space in it, because a bundle that only works in the build
directory is not a bundle.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import NoReturn


def fail(message: str) -> NoReturn:
    print(f"smoke: {message}", file=sys.stderr)
    raise SystemExit(1)


def bundle_paths(bundle: Path) -> tuple[Path, Path, Path]:
    interpreter = bundle / "Contents/Resources/python/bin/python3"
    share = bundle / "Contents/Resources/openbox"
    host = bundle / "Contents/MacOS/OpenBox"
    return interpreter, share, host


def check_interpreter(interpreter: Path) -> None:
    if not interpreter.is_file():
        fail(f"the bundle has no interpreter at {interpreter}")
    result = subprocess.run(
        [str(interpreter), "-B", "-c", "import ssl, sqlite3, json, plistlib, urllib.request"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        fail(f"the bundled interpreter cannot import the standard library: {result.stderr.strip()}")


def check_server(bundle: Path) -> None:
    interpreter, share, _host = bundle_paths(bundle)
    web_app = share / "web_app.py"
    if not web_app.is_file():
        fail(f"the bundle has no runtime payload at {web_app}")
    with tempfile.TemporaryDirectory(prefix="openbox-macos-smoke.") as directory:
        data_dir = Path(directory)
        environment = dict(os.environ)
        environment["OPENBOX_DATA_DIR"] = str(data_dir)
        environment["OPENBOX_NATIVE_HOST"] = "1"
        environment["PYTHONPATH"] = str(share)
        certificate = share / "cacert.pem"
        if certificate.is_file():
            environment["SSL_CERT_FILE"] = str(certificate)
        process = subprocess.Popen(
            [str(interpreter), "-B", str(web_app), "--no-browser"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            port_file = data_dir / "server.port"
            token_file = data_dir / "server.token"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if port_file.is_file() and token_file.is_file():
                    break
                if process.poll() is not None:
                    fail("the bundled server exited before it published its port")
                time.sleep(0.1)
            else:
                fail("the bundled server did not boot within 30 seconds")
            port = port_file.read_text(encoding="utf-8").strip()
            token = token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/native/capabilities",
                headers={"X-OpenBox-Token": token},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("platform") != "darwin":
                fail(f"the bundled server reports platform {payload.get('platform')!r}")
            if not payload.get("webview"):
                fail("the bundled server does not see a native host")
        finally:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def check_host(bundle: Path) -> None:
    _interpreter, _share, host = bundle_paths(bundle)
    if not host.is_file():
        fail(f"the bundle has no native host at {host}")
    with tempfile.TemporaryDirectory(prefix="openbox-macos-selftest.") as directory:
        environment = dict(os.environ)
        environment["OPENBOX_DATA_DIR"] = directory
        environment["OPENBOX_PYTHON"] = str(bundle_paths(bundle)[0])
        result = subprocess.run(
            [str(host), "--selftest"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            fail(f"the native host self test failed: {result.stderr.strip()}")
        if "selftest ok platform=darwin" not in result.stdout:
            fail(f"the native host self test said: {result.stdout.strip()}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: macos_bundle_smoke.py <OpenBox.app>", file=sys.stderr)
        return 2
    bundle = Path(argv[1]).resolve()
    if not bundle.is_dir():
        fail(f"{bundle} is not a directory")
    print("smoke: bundled interpreter")
    check_interpreter(bundle_paths(bundle)[0])
    print("smoke: bundled server")
    check_server(bundle)
    print("smoke: native host self test")
    check_host(bundle)

    with tempfile.TemporaryDirectory(prefix="openbox macos smoke .") as directory:
        moved = Path(directory) / "OpenBox.app"
        shutil.copytree(bundle, moved, symlinks=True)
        print("smoke: relocated bundle")
        check_interpreter(bundle_paths(moved)[0])
        check_server(moved)

    print("smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
