import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHANGELOG = ROOT / "docs" / "CHANGELOG.md"


def assert_released_170_heading():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## [1.7.0]" in text, (
        "docs/CHANGELOG.md must include a [1.7.0] release section heading"
    )


def main():
    assert_released_170_heading()
    with tempfile.TemporaryDirectory() as directory:
        prev_data_dir = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = directory
        try:
            from catalog import apply_progress_automation, game_media_paths
            from emulators import EMULATORS, install_all_emulators, launch_emulator
            from openbox import save_state
            from web_app import Handler
            from webapp_state import finish_session, public_settings

            game = {
                "name": "Demo",
                "path": "/tmp/demo.rom",
                "cover": f"{directory}/media/cover.jpg",
                "screenshots": [f"{directory}/media/shot.png"],
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
            lease = mock.Mock()
            finish_session("launch", 0, datetime.now(), process, lease)
            state = __import__("openbox").load_state()
            assert state["history"] == []

            cover = Path(directory) / "media" / "cover.jpg"
            cover.parent.mkdir(parents=True, exist_ok=True)
            cover.write_text("cover")
            handler = object.__new__(Handler)
            handler.send_json = mock.Mock()
            handler.delete_game({"id": 0, "delete_media": True})
            assert not cover.exists()

            with mock.patch("emulators.subprocess.Popen") as popen:
                popen.return_value = mock.Mock()
                def fake_which(name):
                    return "/usr/bin/mame" if name == "mame" else None
                result = launch_emulator("org.mamedev.MAME", which=fake_which)
                assert result["mode"] == "native"

            with mock.patch("emulators.install_emulator", return_value={"Arcade": "mame {path}"}):
                with mock.patch("emulators.emulator_status") as status_mock:
                    status_mock.return_value = [
                        {"app_id": app_id, "name": EMULATORS[app_id]["name"], "installed": app_id != "org.mamedev.MAME"}
                        for app_id in EMULATORS
                    ]
                    bulk = install_all_emulators(run=mock.Mock(return_value=mock.Mock(returncode=1)), which=lambda name: None)
            # The mock marks only MAME as not installed, so only MAME installs.
            assert bulk["installed"] == ["MAME"]
        finally:
            if prev_data_dir is None:
                os.environ.pop("OPENBOX_DATA_DIR", None)
            else:
                os.environ["OPENBOX_DATA_DIR"] = prev_data_dir

    print("changelog feature self-test: ok")


if __name__ == "__main__":
    main()
