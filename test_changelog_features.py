import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock


def main():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["OPENBOX_DATA_DIR"] = directory
        from catalog import apply_progress_automation, game_media_paths
        from emulators import EMULATORS, install_all_emulators, launch_emulator
        from openbox import save_state
        from web_app import Handler, finish_session, public_settings

        game = {
            "name": "Demo",
            "path": "/tmp/demo.rom",
            "cover": f"{directory}/cover.jpg",
            "screenshots": [f"{directory}/shot.png"],
            "save_paths": [],
            "playtime_seconds": 0,
            "progress": "",
        }
        settings = {
            "progress_automation_enabled": True,
            "progress_automation_play_minutes": 10,
            "progress_automation_idle_days": 7,
        }
        game["playtime_seconds"] = 11 * 60
        apply_progress_automation(game, settings)
        assert game["progress"] == "Playing"

        game["progress"] = "Playing"
        game["last_played"] = (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds")
        apply_progress_automation(game, settings)
        assert game["progress"] == "Paused"

        assert len(game_media_paths(game)) == 2

        save_state({
            "games": [game],
            "profiles": {},
            "history": [],
            "settings": {
                "track_session_history": False,
                "backup_on_close": False,
                "image_group": "background",
            },
        })
        settings_payload = public_settings()
        assert settings_payload["track_session_history"] is False
        assert settings_payload["image_group"] == "background"

        process = mock.Mock()
        process.wait.return_value = 0
        finish_session("launch", 0, datetime.now(), process)
        state = __import__("openbox").load_state()
        assert state["history"] == []

        cover = Path(directory) / "cover.jpg"
        cover.write_text("cover")
        handler = object.__new__(Handler)
        handler.send_json = mock.Mock()
        handler.delete_game({"id": 0, "delete_media": True})
        assert not cover.exists()

        with mock.patch("emulators.subprocess.Popen") as popen:
            popen.return_value = mock.Mock()
            fake_which = lambda name: "/usr/bin/mame" if name == "mame" else None
            result = launch_emulator("org.mamedev.MAME", which=fake_which)
            assert result["mode"] == "native"

        with mock.patch("emulators.install_emulator", return_value={"Arcade": "mame {path}"}):
            with mock.patch("emulators.emulator_status") as status_mock:
                status_mock.return_value = [
                    {"app_id": app_id, "name": EMULATORS[app_id]["name"], "installed": app_id != "org.mamedev.MAME"}
                    for app_id in EMULATORS
                ]
                bulk = install_all_emulators(run=mock.Mock(return_value=mock.Mock(returncode=1)), which=lambda name: None)
        assert bulk["installed"] == ["MAME"]

    print("changelog feature self-test: ok")


if __name__ == "__main__":
    main()
