import os
import sys
import tempfile
import time
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _wait_for_exit_code(*args, **kwargs):
    from pkg.parity.parity_tracking import wait_for_exit

    result = wait_for_exit(*args, **kwargs)
    return result.exit_code


def test_double_launch_lease(save_state, start_game, control_game_session, RUNNING, hold_script):
    """Reliability #8: rapid double Play dedupes via launch lease (SESSION_ALREADY_STARTING)."""
    import pkg.state.launch as launch_mod

    leases = getattr(launch_mod, "_LAUNCH_LEASES", None)
    assert leases is not None, "launch lease registry missing: second Play must be deduped"
    leases.clear()
    try:
        save_state({"games": [{"name": "Lease test", "path": hold_script}], "profiles": {}, "history": []})
        first = start_game(0)
        try:
            start_game(0)
        except Exception as error:
            assert getattr(error, "code", "") == "SESSION_ALREADY_STARTING", f"expected SESSION_ALREADY_STARTING, got {error!r}"
            assert getattr(error, "status", None) == 409, f"expected status 409, got {error!r}"
            detail = getattr(error, "detail", None) or {}
            assert detail.get("launch_id") == first["launch_id"], "lease must name the session to focus"
        else:
            raise AssertionError("rapid second Play must return SESSION_ALREADY_STARTING, started a duplicate")
        assert len(RUNNING) == 1, f"double launch must never create two sessions: {sorted(RUNNING)}"
        control_game_session(first["launch_id"], "stop")
        for _ in range(100):
            if not RUNNING:
                break
            time.sleep(0.01)
        assert not RUNNING
        # Lease expiry (simulated) allows launching again.
        leases.clear()
        second = start_game(0)
        control_game_session(second["launch_id"], "stop")
        for _ in range(100):
            if not RUNNING:
                break
            time.sleep(0.01)
        assert not RUNNING
    finally:
        leases.clear()


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = directory
        hold_script = os.path.join(directory, "hold.sh")
        with open(hold_script, "w", encoding="utf-8") as script_file:
            script_file.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(hold_script, 0o755)
        try:
            from openbox import load_state, save_state
            from web_app import RUNNING, control_game_session
            from webapp_state import STATE_LOCK, finish_session, start_game

            wait_patch = mock.patch("webapp_state.wait_for_exit", side_effect=_wait_for_exit_code)
            wait_patch.start()

            save_state({"games":[{"name":"Session test", "path":hold_script}], "profiles":{}, "history":[]})
            session = start_game(0)
            control_game_session(session["launch_id"], "pause")
            time.sleep(.03)
            with open(f"/proc/{session['pid']}/status", encoding="utf-8") as status_file:
                assert "\nState:\tT" in status_file.read()
            control_game_session(session["launch_id"], "resume")
            control_game_session(session["launch_id"], "resume")
            with mock.patch("os.killpg", side_effect=OSError("No such process")):
                try:
                    control_game_session(session["launch_id"], "pause")
                    raise AssertionError("expected ValueError for failed signal")
                except ValueError:
                    pass
            control_game_session(session["launch_id"], "stop")
            for _ in range(100):
                if not RUNNING:
                    break
                time.sleep(.01)
            assert not RUNNING

            class FinishedProcess:
                pid = 0

                def wait(self):
                    return 0

                def poll(self):
                    return 0

            RUNNING["restart-test"] = {"restart": True}
            with mock.patch("webapp_state.start_game") as restart:
                lease = mock.Mock()
                finish_session("restart-test", 0, datetime.now(), FinishedProcess(), lease)
                restart.assert_called_once_with(0, stable_game_id=load_state()["games"][0]["game_id"])

            # Deleting a game ahead of a running title must not credit the wrong entry.
            save_state({
                "games": [
                    {"name": "Keep me", "path": "/bin/true", "playtime_seconds": 0},
                    {"name": "Running", "path": "/bin/sleep", "launch": "sleep 1", "playtime_seconds": 0},
                ],
                "profiles": {},
                "history": [],
                "settings": {"track_session_history": True},
            })
            session = start_game(1)
            with STATE_LOCK:
                state = load_state()
                del state["games"][0]
                save_state(state)
            for _ in range(200):
                if session["launch_id"] not in RUNNING:
                    break
                time.sleep(0.02)
            state = load_state()
            assert len(state["games"]) == 1
            assert state["games"][0]["name"] == "Running"
            assert state["games"][0]["playtime_seconds"] >= 1
            assert state["history"] and state["history"][-1]["game"] == "Running"
            test_double_launch_lease(save_state, start_game, control_game_session, RUNNING, hold_script)
        finally:
            wait_patch.stop()
            if prev_data_dir is None:
                os.environ.pop("OPENBOX_DATA_DIR", None)
            else:
                os.environ["OPENBOX_DATA_DIR"] = prev_data_dir
    print("session self-test: ok")


if __name__ == "__main__":
    main()
