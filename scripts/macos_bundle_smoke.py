#!/usr/bin/env python3
"""Smoke test for the macOS app bundle.

Checks the four things that break silently when the bundle is assembled
wrong: the universal Rust openbox-server binary, the runtime payload, the
loopback server the host boots, and the host's own self test. The bundle is
also exercised from a path with a space in it, because a bundle that only
works in the build directory is not a bundle.
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
    server = bundle / "Contents/MacOS/openbox-server"
    share = bundle / "Contents/Resources/openbox"
    host = bundle / "Contents/MacOS/OpenBox"
    return server, share, host


def check_server_binary(bundle: Path) -> None:
    server, share, _host = bundle_paths(bundle)
    if not server.is_file() or not os.access(server, os.X_OK):
        fail(f"the bundle has no executable openbox-server at {server}")
    bundled_python = bundle / "Contents/Resources/python"
    if bundled_python.exists():
        fail(f"unexpected bundled Python runtime found at {bundled_python}")
    if not (share / "index.html").is_file():
        fail(f"the bundle has no frontend index.html at {share / 'index.html'}")
    result = subprocess.run(
        [str(server), "--dump-contracts"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        fail(f"openbox-server --dump-contracts failed: {result.stderr.strip()}")
    try:
        contracts = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        fail(f"openbox-server --dump-contracts produced invalid JSON: {exc}")
    if not contracts.get("routes") and not contracts.get("public_routes"):
        fail("openbox-server --dump-contracts returned empty route contracts")
    if not os.environ.get("OPENBOX_SERVER_BIN"):
        archs = subprocess.run(
            ["lipo", "-archs", str(server)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if archs.returncode != 0:
            fail(f"lipo -archs failed on openbox-server: {archs.stderr.strip()}")
        slices = set(archs.stdout.strip().split())
        if not {"arm64", "x86_64"}.issubset(slices):
            fail(f"openbox-server is not universal (arm64 + x86_64): got {sorted(slices)}")


def check_server(bundle: Path) -> None:
    server, share, _host = bundle_paths(bundle)
    if not server.is_file():
        fail(f"the bundle has no server binary at {server}")
    with tempfile.TemporaryDirectory(prefix="openbox-macos-smoke.") as directory:
        data_dir = Path(directory)
        environment = dict(os.environ)
        environment["OPENBOX_DATA_DIR"] = str(data_dir)
        environment["OPENBOX_ROOT"] = str(share)
        environment["OPENBOX_NATIVE_HOST"] = "1"
        process = subprocess.Popen(
            [str(server), "--no-browser"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
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
                    stderr = process.stderr.read().strip() if process.stderr else ""
                    fail(f"the bundled server exited before publishing its port: {stderr}")
                time.sleep(0.1)
            else:
                fail("the bundled server did not boot within 30 seconds")
            port = port_file.read_text(encoding="utf-8").strip()
            token = token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/host/capabilities",
                headers={
                    "X-OpenBox-Token": token,
                    "X-OpenBox-API-Version": "2",
                },
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
    server, share, host = bundle_paths(bundle)
    if not host.is_file():
        fail(f"the bundle has no native host at {host}")
    with tempfile.TemporaryDirectory(prefix="openbox-macos-selftest.") as directory:
        environment = dict(os.environ)
        environment["OPENBOX_DATA_DIR"] = directory
        environment["OPENBOX_SERVER_BIN"] = str(server)
        environment["OPENBOX_ROOT"] = str(share)
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
    print("smoke: bundled Rust server binary")
    check_server_binary(bundle)
    print("smoke: bundled server")
    check_server(bundle)
    print("smoke: native host self test")
    check_host(bundle)

    with tempfile.TemporaryDirectory(prefix="openbox macos smoke .") as directory:
        moved = Path(directory) / "OpenBox.app"
        shutil.copytree(bundle, moved, symlinks=True)
        print("smoke: relocated bundle")
        check_server_binary(moved)
        check_server(moved)

    print("smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
