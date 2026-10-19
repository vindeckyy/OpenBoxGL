#!/usr/bin/env python3
import hashlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from urllib.error import HTTPError

from retroachievements import api_get, game_hash, game_progress, match_game, save_credentials


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
        if hasattr(__import__("os"), "geteuid"):
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

        # An M3U hashes its first existing disc, resolved beside the playlist.
        playlist = root / "game.m3u"
        playlist.write_text("#EXTM3U\nmissing.bin\ngame.nes\n", encoding="utf-8")
        assert game_hash({"path":str(playlist), "platform":"NES"}) == digest

        # CHD extraction hashes the first data track named by the cue sheet.
        chd = root / "disc.chd"
        chd.write_bytes(b"chd")
        def fake_chdman(argv, **kwargs):
            cue = Path(argv[argv.index("-o") + 1])
            cue.write_text('FILE "track1.bin" BINARY\n', encoding="utf-8")
            (cue.parent / "track1.bin").write_bytes(b"ps1 data")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("retroachievements.shutil.which", return_value="/usr/bin/chdman"), patch("retroachievements.subprocess.run", side_effect=fake_chdman):
            assert game_hash({"path":str(chd), "platform":"PlayStation"}) == hashlib.md5(b"ps1 data").hexdigest()

        # RVZ uses dolphin-tool's SHA-1 line rather than container bytes.
        rvz = root / "disc.rvz"
        rvz.write_bytes(b"rvz")
        dolphin = hashlib.sha1(b"disc image").hexdigest()
        with patch("retroachievements.shutil.which", return_value="/usr/bin/dolphin-tool"), patch("retroachievements.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=f"SHA1: {dolphin}\n", stderr="")):
            assert game_hash({"path":str(rvz), "platform":"GameCube"}) == dolphin

        # A WiiWare WAD hashes the aligned TMD and encrypted contents, not the
        # container. The table is built independently so the test pins the
        # rcheevos rc_hash_wiiware layout, not this implementation's offsets.
        wad = root / "game.wad"
        tmd = bytearray(0x200)
        tmd[0x1DE:0x1E0] = b"\x00\x01"
        tmd[0x1F0:0x1F4] = (0x10).to_bytes(4, "big")
        content = bytes(range(0x10))
        header = bytearray(0x40)
        header[4:8] = b"Is\x00\x00"
        header[20:24] = (0x200).to_bytes(4, "big")
        wad.write_bytes(bytes(header) + bytes(tmd) + content)
        expected_wad = hashlib.md5(bytes(tmd) + content).hexdigest()
        assert game_hash({"path":str(wad), "platform":"Wii"}) == expected_wad
        assert game_hash({"path":str(wad), "platform":"WiiWare"}) == expected_wad
        wad.write_bytes(b"not a wad")
        try:
            game_hash({"path":str(wad), "platform":"Wii"})
            raise AssertionError("invalid WAD magic should fail")
        except ValueError as error:
            assert "WAD" in str(error)
        with patch("retroachievements.shutil.which", return_value=None):
            try:
                game_hash({"path":str(chd), "platform":"PlayStation"})
                raise AssertionError("missing chdman should fail")
            except ValueError as error:
                assert "chdman" in str(error)

        # A 401 surfaces as a credentials hint, not a generic HTTP error.
        def rejected(request, timeout=0):
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        try:
            api_get("API_GetUserProfile.php", {"u":"player"}, {"username":"player","api_key":"bad"}, opener=rejected)
            raise AssertionError("401 should fail")
        except ValueError as error:
            assert "credentials" in str(error).casefold()
    print("RetroAchievements self-test: ok")


if __name__ == "__main__":
    test()
