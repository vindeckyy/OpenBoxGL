#!/usr/bin/env python3
"""Reliability #7: SIGTERM/SIGINT must drain webhooks and stop sessions.

Verifies the web_app stop() teardown contract without binding a server:
source pins the signal handlers + teardown calls, then the same public seams
stop() uses (control_game_session "stop" per RUNNING entry, then
shutdown_webhooks) are exercised behaviorally against a real session.
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401  # register flat-import finder


def main():
    web_app_src = (Path(__file__).resolve().parent.parent / "web_app.py").read_text(encoding="utf-8")
    stop_match = re.search(r"def stop\(\):.*?(?=\n    def |\nclass |\Z)", web_app_src, re.DOTALL)
    assert stop_match, "web_app must define stop()"
    stop_src = stop_match.group(0)
    assert 'control_game_session(launch_id, "stop")' in stop_src, "stop() must stop every running session"
    assert "shutdown_webhooks(" in stop_src, "stop() must drain webhooks"
    assert "signal.signal(signal.SIGTERM, request_shutdown)" in web_app_src, "SIGTERM must route to graceful teardown"
    assert "signal.signal(signal.SIGINT, request_shutdown)" in web_app_src, "SIGINT must route to graceful teardown"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = directory
        hold_script = os.path.join(directory, "hold.sh")
        with open(hold_script, "w", encoding="utf-8") as script_file:
            script_file.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(hold_script, 0o755)
        try:
            from openbox import save_state
            from web_app import RUNNING
            from webapp_state import control_game_session, shutdown_webhooks, start_game

            from pkg.state import sse as sse_module

            save_state({"games": [{"name": "Shutdown test", "path": hold_script}], "profiles": {}, "history": []})
            session = start_game(0)
            assert session["launch_id"] in RUNNING

            # Mirror web_app.stop(): stop every session, then drain webhooks.
            with mock.patch.object(sse_module, "WEBHOOK_DISPATCHER") as dispatcher:
                dispatcher.shutdown.return_value = None
                launch_ids = list(RUNNING.keys())
                for launch_id in launch_ids:
                    try:
                        control_game_session(launch_id, "stop")
                    except ValueError:
                        pass
                for _ in range(200):
                    if not RUNNING:
                        break
                    time.sleep(0.02)
                assert not RUNNING, "sessions must drain on shutdown"
                shutdown_webhooks(wait_seconds=0.1)
                assert dispatcher.shutdown.called, "shutdown must drain the webhook dispatcher"
        finally:
            if prev_data_dir is None:
                os.environ.pop("OPENBOX_DATA_DIR", None)
            else:
                os.environ["OPENBOX_DATA_DIR"] = prev_data_dir
    print("shutdown drain self-test: ok")


if __name__ == "__main__":
    main()
