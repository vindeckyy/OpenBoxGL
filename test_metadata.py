#!/usr/bin/env python3
import email.message
import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from metadata import IMAGE_URL, apply_game_metadata, build_database, search_games, sync_database


class ImageResponse(io.BytesIO):
    def __init__(self):
        super().__init__(b"image")
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "image/png"
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        # A failed download must not leak the temp zip in the data dir.
        def failing_opener(request, timeout=0):
            raise OSError("network down")
        try:
            sync_database(root / "metadata.db", opener=failing_opener)
            raise AssertionError("expected download failure")
        except OSError:
            pass
        leftovers = [p for p in root.iterdir() if p.suffix == ".zip"]
        assert leftovers == [], f"temp zip leaked: {leftovers}"
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>42</DatabaseID><Name>Real Game (USA)</Name><Platform>NES</Platform><Developer>Studio</Developer><Overview>Real description</Overview></Game>
          <GameImage><DatabaseID>42</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type><Region>North America</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>shot.png</FileName><Type>Screenshot - Gameplay</Type><Region>World</Region></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        assert search_games(database, "Real Game", "NES")[0]["database_id"] == 42
        requests = []
        def opener(request, **_):
            requests.append(request.full_url)
            return ImageResponse()
        game = apply_game_metadata(
            {"name":"Real Game", "path":"/real.rom"},
            database, 42, ["cover","screenshots"], root / "media",
            opener=opener,
        )
        assert game["developer"] == "Studio" and Path(game["cover"]).is_file()
        assert len(game["screenshots"]) == 1
        assert requests == [IMAGE_URL + "cover.png", IMAGE_URL + "shot.png"]
    print("metadata self-test: ok")


if __name__ == "__main__":
    test()
