import os
import tempfile
import time
from datetime import datetime
from unittest import mock


def main():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["OPENBOX_DATA_DIR"] = directory
        from openbox import load_state, save_state
        from web_app import RUNNING, STATE_LOCK, control_game_session, finish_session, start_game

        save_state({"games":[{"name":"Session test", "path":"/bin/sleep", "launch":"sleep 30"}], "profiles":{}, "history":[]})
        session = start_game(0)
        control_game_session(session["launch_id"], "pause")
        time.sleep(.03)
        with open(f"/proc/{session['pid']}/status", encoding="utf-8") as status_file:
            assert "\nState:\tT" in status_file.read()
        control_game_session(session["launch_id"], "resume")
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
        with mock.patch("web_app.start_game") as restart:
            finish_session("restart-test", 0, datetime.now(), FinishedProcess())
        restart.assert_called_once_with(0)

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
    print("session self-test: ok")


if __name__ == "__main__":
    main()
