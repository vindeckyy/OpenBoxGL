import os
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory() as directory:
        data = Path(directory) / "data"
        games = Path(directory) / "games"
        games.mkdir()
        (games / "one.nes").write_bytes(b"NES\x1a")
        (games / "ignore.txt").write_text("not a game")
        os.environ["OPENBOX_DATA_DIR"] = str(data)
        from openbox import load_state
        from web_app import import_folder_path

        added, found, _ = import_folder_path(games)
        assert (added, found) == (1, 1)
        added, found, _ = import_folder_path(games)
        assert (added, found) == (0, 1)
        imported = load_state()["games"][0]
        assert imported["platform"] == "NES" and imported["name"] == "one"
    print("auto-import self-test: ok")


if __name__ == "__main__":
    main()
