import json
import tempfile
from pathlib import Path

from cloud_sync import game_key, sync_statistics


def main():
    with tempfile.TemporaryDirectory() as directory:
        game = {"name":"Game", "path":"/games/game.rom", "play_count":1, "playtime_seconds":10, "progress":"Playing"}
        state = {"games":[game], "settings":{"last_cloud_sync":"2026-01-01T00:00:00"}}
        target = Path(directory) / "openbox-statistics.json"
        target.write_text(json.dumps({"generated_at":"2026-02-01T00:00:00","games":{game_key(game):{"play_count":3,"playtime_seconds":25,"progress":"Completed","rating":4.5,"favorite":True}}}))
        result = sync_statistics(state, directory, "2026-03-01T00:00:00")
        assert result["merged"] == 1
        assert game["play_count"] == 3 and game["progress"] == "Completed" and game["favorite"]
        assert json.loads(target.read_text())["format"] == 1

        # A game deleted locally must not be resurrected from the cloud.
        deleted = {"name":"Deleted", "path":"/games/gone.rom", "play_count":2}
        target.write_text(json.dumps({"generated_at":"2026-01-01T00:00:00","games":{game_key(deleted):{"play_count":2}}}))
        state2 = {"games":[], "settings":{}}
        sync_statistics(state2, directory, "2026-04-01T00:00:00")
        payload = json.loads(target.read_text())
        assert game_key(deleted) not in payload["games"]
    print("cloud-sync self-test: ok")


if __name__ == "__main__":
    main()
