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

        # Remote freshness must never be consulted after a local-wins
        # decision: the field-merge branch is unconditional, not gated on
        # a comparison that can never be true there.
        local_wins = {"name":"Local", "path":"/games/local.rom", "last_played":"2026-05-01T00:00:00",
                      "progress":"Playing", "rating":3, "favorite":False, "play_count":1,
                      "playtime_seconds":60}
        cloud_state = {"games":[local_wins], "settings":{"last_cloud_sync":"2026-01-01T00:00:00"}}
        target.write_text(json.dumps({
            "generated_at":"2026-06-01T00:00:00",
            "games":{game_key(local_wins):{"last_played":"2026-04-01T00:00:00",
                "progress":"Completed", "rating":5, "favorite":True,
                "play_count":2, "playtime_seconds":120}},
        }))
        sync_statistics(cloud_state, directory, "2026-07-01T00:00:00")
        game = cloud_state["games"][0]
        assert game["progress"] == "Completed", "newer per-field values must merge when local wins"
        assert game["rating"] == 5 and game["favorite"] is True
    print("cloud-sync self-test: ok")


if __name__ == "__main__":
    main()
