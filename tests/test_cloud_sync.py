import json
import tempfile
from pathlib import Path

from cloud_sync import CloudRemoteInvalid, game_key, legacy_game_key, sync_statistics


def main():
    with tempfile.TemporaryDirectory() as directory:
        game = {
            "name": "Game",
            "path": "/games/game.rom",
            "play_count": 1,
            "playtime_seconds": 10,
            "progress": "Playing",
        }
        state = {"games": [game], "settings": {"last_cloud_sync": "2026-01-01T00:00:00"}, "notifications": []}
        target = Path(directory) / "openbox-statistics.json"
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-02-01T00:00:00",
                    "games": {
                        game_key(game): {
                            "play_count": 3,
                            "playtime_seconds": 25,
                            "progress": "Completed",
                            "rating": 4.5,
                            "favorite": True,
                        }
                    },
                }
            )
        )
        result = sync_statistics(state, directory, "2026-03-01T00:00:00")
        assert result["merged"] == 1
        assert game["play_count"] == 3
        assert game["playtime_seconds"] == 25
        assert game["progress"] == "Playing", "local progress must survive remote conflict"
        assert game["rating"] == 4.5
        assert game["favorite"] is True
        assert json.loads(target.read_text())["format"] == 1
        assert any(item.get("kind") == "cloud_sync" and item.get("level") == "info" for item in state["notifications"])

        # A game deleted locally must not be resurrected from the cloud.
        deleted = {"name": "Deleted", "path": "/games/gone.rom", "play_count": 2}
        target.write_text(json.dumps({"generated_at": "2026-01-01T00:00:00", "games": {game_key(deleted): {"play_count": 2}}}))
        state2 = {"games": [], "settings": {}, "notifications": []}
        sync_statistics(state2, directory, "2026-04-01T00:00:00")
        payload = json.loads(target.read_text())
        assert game_key(deleted) not in payload["games"]

        # Local-wins timestamps must preserve local progress, favorite, and rating.
        local_wins = {
            "name": "Local",
            "path": "/games/local.rom",
            "last_played": "2026-05-01T00:00:00",
            "progress": "Playing",
            "rating": 3,
            "favorite": False,
            "play_count": 1,
            "playtime_seconds": 60,
        }
        cloud_state = {
            "games": [local_wins],
            "settings": {"last_cloud_sync": "2026-01-01T00:00:00"},
            "notifications": [],
        }
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-01T00:00:00",
                    "games": {
                        game_key(local_wins): {
                            "last_played": "2026-04-01T00:00:00",
                            "progress": "Completed",
                            "rating": 5,
                            "favorite": True,
                            "play_count": 2,
                            "playtime_seconds": 120,
                        }
                    },
                }
            )
        )
        sync_statistics(cloud_state, directory, "2026-07-01T00:00:00")
        merged = cloud_state["games"][0]
        assert merged["progress"] == "Playing"
        assert merged["rating"] == 3
        assert merged["favorite"] is False
        assert merged["play_count"] == 2
        assert merged["playtime_seconds"] == 120
        assert merged["last_played"] == "2026-05-01T00:00:00"

        # Remote fills missing local progress/rating/favorite only.
        missing = {"name": "Missing", "path": "/games/missing.rom", "play_count": 0}
        fill_state = {"games": [missing], "settings": {}, "notifications": []}
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "games": {
                        game_key(missing): {
                            "progress": "Beaten",
                            "rating": 2.5,
                            "favorite": True,
                            "last_played": "2026-02-01T12:00:00",
                        }
                    },
                }
            )
        )
        sync_statistics(fill_state, directory, "2026-03-01T00:00:00")
        filled = fill_state["games"][0]
        assert filled["progress"] == "Beaten"
        assert filled["rating"] == 2.5
        assert filled["favorite"] is True
        assert filled["last_played"] == "2026-02-01T12:00:00"

        # Parsed last_played: ISO beats empty string.
        iso_game = {"name": "Iso", "path": "/games/iso.rom", "last_played": "2026-03-15T10:00:00"}
        iso_state = {"games": [iso_game], "settings": {}, "notifications": []}
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "games": {game_key(iso_game): {"last_played": ""}},
                }
            )
        )
        sync_statistics(iso_state, directory, "2026-04-01T00:00:00")
        assert iso_state["games"][0]["last_played"] == "2026-03-15T10:00:00"

        # Corrupt remote JSON must raise CLOUD_REMOTE_INVALID and leave files untouched.
        bad_bytes = b"{not valid json"
        target.write_text(bad_bytes.decode("utf-8"))
        corrupt_state = {"games": [], "settings": {"last_cloud_sync": "2026-01-01T00:00:00"}, "notifications": []}
        try:
            sync_statistics(corrupt_state, directory, "2026-06-01T00:00:00")
            raise AssertionError("expected CloudRemoteInvalid")
        except CloudRemoteInvalid as error:
            assert error.code == "CLOUD_REMOTE_INVALID"
        assert target.read_bytes() == bad_bytes
        assert corrupt_state["settings"]["last_cloud_sync"] == "2026-01-01T00:00:00"
        assert any(item.get("kind") == "cloud_sync" and item.get("level") == "error" for item in corrupt_state["notifications"])

        # Legacy keys and invalid remote shapes.
        assert game_key({"game_id": "abc"}) == "id:abc"
        assert legacy_game_key({"steam_app_id": "570"}) == "steam:570"
        assert legacy_game_key({"heroic_app_id": "h1", "source": "gog"}) == "heroic:gog:h1"
        assert legacy_game_key({"lutris_id": "l1"}) == "lutris:l1"
        assert legacy_game_key({"rom_name": "pacman", "source": "mame"}) == "arcade:mame:pacman"

        legacy_game = {"name": "Legacy", "steam_app_id": "999", "play_count": 1}
        legacy_state = {"games": [legacy_game], "settings": {}, "notifications": []}
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "games": {"steam:999": {"play_count": 4}},
                }
            )
        )
        sync_statistics(legacy_state, directory, "2026-02-01T00:00:00")
        assert legacy_state["games"][0]["play_count"] == 4

        target.write_text("[]")
        try:
            sync_statistics({"games": [], "settings": {}, "notifications": []}, directory)
            raise AssertionError("expected CloudRemoteInvalid for non-object remote")
        except CloudRemoteInvalid:
            pass

        target.write_text(json.dumps({"generated_at": "2026-01-01T00:00:00", "games": []}))
        try:
            sync_statistics({"games": [], "settings": {}, "notifications": []}, directory)
            raise AssertionError("expected CloudRemoteInvalid for non-dict games")
        except CloudRemoteInvalid:
            pass

        try:
            sync_statistics({"games": []}, "/does/not/exist", "2026-01-01T00:00:00")
            raise AssertionError("expected ValueError for missing folder")
        except ValueError as error:
            assert "does not exist" in str(error)

        # First sync with no remote file yet.
        fresh_dir = Path(directory) / "fresh"
        fresh_dir.mkdir()
        fresh_game = {"name": "Fresh", "path": "/games/fresh.rom", "play_count": 2}
        fresh_state = {"games": [fresh_game], "settings": {}, "notifications": []}
        sync_statistics(fresh_state, fresh_dir, "2026-05-01T00:00:00")
        assert (fresh_dir / "openbox-statistics.json").is_file()

        # Resolve legacy remote entry when the canonical id key is absent.
        bridged = {"game_id": "g-bridge", "steam_app_id": "42", "play_count": 1}
        bridge_state = {"games": [bridged], "settings": {}, "notifications": []}
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "games": {"steam:42": {"play_count": 9}, "id:g-bridge": "bad"},
                }
            )
        )
        sync_statistics(bridge_state, directory, "2026-02-01T00:00:00")
        assert bridge_state["games"][0]["play_count"] == 9

        # Invalid ratings are ignored; local newer sync bumps generated_at.
        rating_game = {"name": "Ratings", "path": "/games/ratings.rom", "rating": "bad"}
        rating_state = {
            "games": [rating_game],
            "settings": {"last_cloud_sync": "2026-06-01T00:00:00"},
            "notifications": [],
        }
        target.write_text(
            json.dumps(
                {
                    "generated_at": "2026-01-01T00:00:00",
                    "games": {game_key(rating_game): {"rating": "nope", "progress": "bogus"}},
                }
            )
        )
        sync_statistics(rating_state, directory, "2026-07-01T00:00:00")
        assert "rating" not in rating_state["games"][0] or rating_state["games"][0].get("rating") in (0, "bad")
        remote_payload = json.loads(target.read_text())
        assert remote_payload["generated_at"] == "2026-07-01T00:00:00"

    print("cloud-sync self-test: ok")


if __name__ == "__main__":
    main()
