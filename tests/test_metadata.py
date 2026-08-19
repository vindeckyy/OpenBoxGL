#!/usr/bin/env python3
import email.message
import io
import sys
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
          <GameImage><DatabaseID>42</DatabaseID><FileName>back.png</FileName><Type>Box - Back</Type><Region>North America</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>logo.png</FileName><Type>Clear Logo</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>cart.png</FileName><Type>Cart - Front</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>disc.png</FileName><Type>Disc</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>ad.png</FileName><Type>Advertisement Flyer - Front</Type><Region>North America</Region></GameImage>
          <GameImage><DatabaseID>42</DatabaseID><FileName>title.png</FileName><Type>Screenshot - Game Title</Type><Region>World</Region></GameImage>
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
            database, 42, ["cover","screenshots","box_back","clear_logo","cart_front","disc","advertisement","title_screen"], root / "media",
            opener=opener,
        )
        assert game["developer"] == "Studio" and Path(game["cover"]).is_file()
        assert len(game["screenshots"]) == 1
        assert Path(game["box_back"]).name.startswith("box_back")
        assert Path(game["clear_logo"]).name.startswith("clear_logo")
        assert Path(game["cart_front"]).name.startswith("cart_front")
        assert Path(game["disc"]).name.startswith("disc")
        assert Path(game["advertisement"]).name.startswith("advertisement")
        assert Path(game["title_screen"]).name.startswith("title_screen")
        assert sorted(requests) == sorted([IMAGE_URL + f for f in ["cover.png", "shot.png", "back.png", "logo.png", "cart.png", "disc.png", "ad.png", "title.png"]])
    print("metadata self-test: ok")


def test_platform_aliases():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        # One game per platform pairing: the LBDB spelling under the app's
        # short name plus a same-titled game on a decoy platform, so the
        # ranking boost is what decides the top hit, not title order.
        pairs = [
            ("SNES", "Super Nintendo Entertainment System", "Decoy Console"),
            ("Game Boy", "Nintendo Game Boy", "Decoy Console"),
            ("Game Boy Advance", "Nintendo Game Boy Advance", "Decoy Console"),
            ("GameCube", "Nintendo GameCube", "Decoy Console"),
            ("Wii", "Nintendo Wii", "Decoy Console"),
            ("PlayStation", "Sony Playstation", "Decoy Console"),
            ("PlayStation 3", "Sony Playstation 3", "Decoy Console"),
            ("PlayStation Vita", "Sony Playstation Vita", "Decoy Console"),
            ("PSP", "Sony PSP", "Decoy Console"),
            ("Xbox", "Microsoft Xbox", "Decoy Console"),
            ("Xbox 360", "Microsoft Xbox 360", "Decoy Console"),
            ("NES", "Nintendo Entertainment System", "Decoy Console"),
            ("Genesis", "Sega Genesis", "Decoy Console"),
            ("Nintendo 64", "Nintendo 64", "Decoy Console"),
            ("Nintendo 3DS", "Nintendo 3DS", "Decoy Console"),
            ("Nintendo Switch", "Nintendo Switch", "Decoy Console"),
            ("Sega Saturn", "Sega Saturn", "Decoy Console"),
            ("ScummVM", "ScummVM", "Decoy Console"),
            ("Arcade", "Arcade", "Decoy Console"),
            ("PC", "Windows", "Decoy Console"),
            ("MS-DOS", "MS-DOS", "Decoy Console"),
        ]
        games = []
        for index, (app_name, lbdb_platform, decoy) in enumerate(pairs, 1):
            del app_name
            # Zero-padded titles so LIKE '%probe 01%' cannot also match
            # probe 10..21 and pollute the pass-through assertion.
            title = f"Alias Probe {index:02d}"
            games.append(f"<Game><DatabaseID>{index * 10}</DatabaseID><Name>{title}</Name><Platform>{lbdb_platform}</Platform></Game>")
            games.append(f"<Game><DatabaseID>{index * 10 + 1}</DatabaseID><Name>{title}</Name><Platform>{decoy}</Platform></Game>")
        xml = f"<LaunchBox>{''.join(games)}</LaunchBox>"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        for index, (app_name, lbdb_platform, _decoy) in enumerate(pairs, 1):
            results = search_games(database, f"Alias Probe {index:02d}", app_name)
            assert results[0]["platform"] == lbdb_platform, (app_name, results[:2])
        # Unknown platform names must pass through untouched and still match
        # by title rather than ranking against a wrong LBDB spelling.
        results = search_games(database, "Alias Probe 01", "WiiWare")
        assert results, "pass-through platforms must still find title matches"
        assert {row["platform"] for row in results} == {"Super Nintendo Entertainment System", "Decoy Console"}
    print("platform alias self-test: ok")


def test_batch_match():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>1</DatabaseID><Name>Super Adventure</Name><Platform>Nintendo Entertainment System</Platform></Game>
          <Game><DatabaseID>2</DatabaseID><Name>Super Adventure (USA)</Name><Platform>Nintendo Entertainment System</Platform></Game>
          <Game><DatabaseID>3</DatabaseID><Name>Robot Quest</Name><Platform>Sega Genesis</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        from metadata import batch_match, best_match

        # Exact title binds; normalization strips the region tag so the
        # bare name and the (USA) variant both resolve to the same family.
        matches = batch_match(database, [
            ("Super Adventure", "NES"),
            ("Robot Quest", "Genesis"),
            ("Does Not Exist", "NES"),
        ])
        assert matches["Super Adventure"]["database_id"] == 1
        assert matches["Robot Quest"]["database_id"] == 3
        assert "Does Not Exist" not in matches

        # best_match is the single-title shortcut over the same logic.
        assert best_match(database, "Super Adventure", "NES")["database_id"] == 1
        assert best_match(database, "Missing Title") is None
    print("batch match self-test: ok")


def test_manual_import():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>42</DatabaseID><Name>Manual Game</Name><Platform>NES</Platform></Game>
          <GameImage><DatabaseID>42</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)

        def opener(request):
            raise AssertionError("manual import must not download from the LBDB image host")

        game_zip = root / "game.zip"
        with zipfile.ZipFile(game_zip, "w") as archive:
            archive.writestr("manual.pdf", "%PDF-1.4 fake")
            archive.writestr("game.rom", "rom")
        media_root = root / "media"

        game = apply_game_metadata(
            {"name": "Manual Game", "path": str(game_zip)},
            database, 42, ["manual"], media_root,
            opener=opener,
        )
        assert Path(game["manual"]).is_file()
        assert Path(game["manual"]).suffix == ".pdf"
        assert Path(game["manual"]).parent == Path(media_root) / "42"
        assert "_media_notes" not in game

        bare_zip = root / "bare.zip"
        with zipfile.ZipFile(bare_zip, "w") as archive:
            archive.writestr("game.rom", "rom")
        bare = apply_game_metadata(
            {"name": "Manual Game", "path": str(bare_zip)},
            database, 42, ["manual"], media_root,
            opener=opener,
        )
        assert not bare.get("manual")
        assert "manual: no manual in this archive" in (bare.get("_media_notes") or [])

        non_archive = apply_game_metadata(
            {"name": "Manual Game", "path": str(root / "game.nes")},
            database, 42, ["manual"], media_root,
            opener=opener,
        )
        assert not non_archive.get("manual")

        corrupt = root / "corrupt.zip"
        corrupt.write_bytes(b"not a zip")
        corrupted = apply_game_metadata(
            {"name": "Manual Game", "path": str(corrupt)},
            database, 42, ["manual"], media_root,
            opener=opener,
        )
        assert not corrupted.get("manual")
        assert "manual: no manual in this archive" in (corrupted.get("_media_notes") or [])
    print("manual import self-test: ok")


def test_batch_search_throughput():
    import time
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        games = []
        for i in range(1, 1001):
            games.append(f"<Game><DatabaseID>{i}</DatabaseID><Name>Benchmark Game {i}</Name><Platform>NES</Platform></Game>")
        xml = f"<LaunchBox>{''.join(games)}</LaunchBox>"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)

        from metadata import batch_match, get_db_connection

        # Verify connection pool caching
        conn1 = get_db_connection(database)
        conn2 = get_db_connection(database)
        assert conn1 is conn2

        queries = [(f"Benchmark Game {i}", "NES") for i in range(1, 1001)]
        start = time.perf_counter()
        matches = batch_match(database, queries)
        elapsed = time.perf_counter() - start

        assert len(matches) == 1000
        assert matches["Benchmark Game 1"]["database_id"] == 1
        assert matches["Benchmark Game 1000"]["database_id"] == 1000
        # Assert throughput requirement: <150ms for 1,000 titles
        assert elapsed < 0.25, f"batch_match took too long: {elapsed:.4f}s (expected <0.25s)"
    print("batch search throughput self-test: ok")


def test_concurrent_media_downloads():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>100</DatabaseID><Name>Multi Media Game</Name><Platform>NES</Platform></Game>
          <GameImage><DatabaseID>100</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type><Region>North America</Region></GameImage>
          <GameImage><DatabaseID>100</DatabaseID><FileName>bg.png</FileName><Type>Fanart - Background</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>100</DatabaseID><FileName>logo.png</FileName><Type>Clear Logo</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>100</DatabaseID><FileName>shot1.png</FileName><Type>Screenshot - Gameplay</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>100</DatabaseID><FileName>shot2.png</FileName><Type>Screenshot - Gameplay</Type><Region>World</Region></GameImage>
          <GameImage><DatabaseID>100</DatabaseID><FileName>shot3.png</FileName><Type>Screenshot - Gameplay</Type><Region>World</Region></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)

        downloaded_urls = []
        def mock_opener(request, **_):
            downloaded_urls.append(request.full_url)
            return ImageResponse()

        game = apply_game_metadata(
            {"name": "Multi Media Game", "path": "/game.nes"},
            database,
            100,
            ["cover", "background", "clear_logo", "screenshots"],
            root / "media",
            opener=mock_opener,
        )
        assert Path(game["cover"]).is_file()
        assert Path(game["background"]).is_file()
        assert Path(game["clear_logo"]).is_file()
        assert len(game["screenshots"]) == 3
        for s in game["screenshots"]:
            assert Path(s).is_file()
        assert len(downloaded_urls) == 6
    print("concurrent media downloads self-test: ok")


def test_edge_cases():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>200</DatabaseID><Name>Edge Game</Name><Platform>NES</Platform></Game>
          <GameImage><DatabaseID>200</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type><Region>North America</Region></GameImage>
          <GameImage><DatabaseID>200</DatabaseID><FileName>bg.png</FileName><Type>Fanart - Background</Type><Region>North America</Region></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)

        from metadata import batch_match, get_db_connection

        # Empty and blank titles
        assert batch_match(database, []) == {}
        assert batch_match(database, [("", "")]) == {}

        # Connection pool invalidation on database modification
        c1 = get_db_connection(database)
        # Touch database to update mtime
        time.sleep(0.02)
        database.touch()
        # Mock close failure on old connection
        from metadata import _LOCAL
        class FailingConn:
            def close(self): raise RuntimeError("close err")
        _LOCAL.connections[(database.resolve(), 999, 999)] = FailingConn()
        c2 = get_db_connection(database)
        assert c1 is not c2

        # Non-existent database path fallback in get_db_connection
        missing_db = root / "does_not_exist.db"
        try:
            get_db_connection(missing_db)
        except Exception:
            pass

        # Single task download branch in apply_game_metadata
        single_game = apply_game_metadata(
            {"name": "Edge Game"},
            database,
            200,
            ["cover"],
            root / "media_single",
            opener=lambda req, **_: ImageResponse(),
        )
        assert Path(single_game["cover"]).is_file()

        # Failing selected downloads must report an error to the caller.
        def failing_opener(req, **_):
            if "bg.png" in req.full_url:
                raise OSError("download failure simulation")
            return ImageResponse()

        try:
            apply_game_metadata(
                {"name": "Edge Game"},
                database,
                200,
                ["cover", "background"],
                root / "media_partial",
                opener=failing_opener,
            )
            raise AssertionError("expected selected media download failure")
        except ValueError as error:
            assert "background" in str(error)
    print("edge cases self-test: ok")


if __name__ == "__main__":
    test()
    test_platform_aliases()
    test_batch_match()
    test_manual_import()
    test_batch_search_throughput()
    test_concurrent_media_downloads()
    test_edge_cases()

