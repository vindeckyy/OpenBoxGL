#!/usr/bin/env python3
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from retroachievements import game_hash, game_progress, match_game, save_credentials


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        nes = root / "game.nes"
        nes.write_bytes(b"NES\x1a" + b"\0" * 12 + b"real rom")
        digest = hashlib.md5(b"real rom").hexdigest()
        assert game_hash({"path":str(nes), "platform":"NES"}) == digest
        responses = {
            "API_GetUserProfile.php":{"User":"player","TotalPoints":10},
            "API_GetConsoleIDs.php":[{"ID":7,"Name":"Nintendo Entertainment System"}],
            "API_GetGameList.php":[{"ID":42,"Hashes":[digest]}],
            "API_GetGameInfoAndUserProgress.php":{
                "Title":"Real Game","NumAchievements":1,"NumAwardedToUser":1,"UserCompletion":"100%",
                "Achievements":{"1":{"Title":"Win","Description":"Finish","Points":5,"BadgeName":"123","DateEarned":"today"}},
            },
        }
        def fetch(endpoint, params, credentials):
            return responses[endpoint]
        save_credentials(root, "player", "key", fetch)
        assert (root / "retroachievements.json").stat().st_mode & 0o777 == 0o600
        game_id, matched_hash = match_game({"path":str(nes),"platform":"NES"}, {"username":"player","api_key":"key"}, root / "cache", fetch)
        assert (game_id, matched_hash) == (42, digest)
        progress = game_progress(game_id, {"username":"player","api_key":"key"}, fetch)
        assert progress["earned"] == 1 and progress["achievements"][0]["earned"]

        # A game-list entry with "Hashes": null must not abort the match.
        null_hash = hashlib.md5(b"other rom").hexdigest()
        responses["API_GetGameList.php"] = [{"ID":7,"Hashes":None}, {"ID":42,"Hashes":[null_hash]}]
        other = root / "other.nes"
        other.write_bytes(b"NES\x1a" + b"\0" * 12 + b"other rom")
        # Use a fresh cache dir so the updated game list is fetched.
        game_id, matched_hash = match_game({"path":str(other),"platform":"NES"}, {"username":"player","api_key":"key"}, root / "cache2", fetch)
        assert (game_id, matched_hash) == (42, null_hash)
    print("RetroAchievements self-test: ok")


if __name__ == "__main__":
    test()
