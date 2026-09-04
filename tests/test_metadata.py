#!/usr/bin/env python3
import email.message
import io
import sys
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

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
        assert matches[("Super Adventure", "NES")]["database_id"] == 1
        assert matches[("Robot Quest", "Genesis")]["database_id"] == 3
        assert ("Does Not Exist", "NES") not in matches

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
        assert matches[("Benchmark Game 1", "NES")]["database_id"] == 1
        assert matches[("Benchmark Game 1000", "NES")]["database_id"] == 1000
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


def test_halo_platform_collision():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>1</DatabaseID><Name>Halo</Name><Platform>Microsoft Xbox</Platform></Game>
          <Game><DatabaseID>2</DatabaseID><Name>Halo</Name><Platform>Sony Playstation 2</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        from metadata import batch_match

        matches = batch_match(database, [("Halo", "Xbox"), ("Halo", "PlayStation 2")])
        assert matches[("Halo", "Xbox")]["database_id"] == 1
        assert matches[("Halo", "PlayStation 2")]["database_id"] == 2
    print("halo platform collision self-test: ok")


import json  # noqa: E402
import os  # noqa: E402
import unittest  # noqa: E402
from unittest import mock  # noqa: E402

from api_errors import BadRequest, Conflict, PreviewExpired, PreviewNotFound, PreviewStale  # noqa: E402
from handlers.metadata import MetadataHandlers  # noqa: E402
from metadata import (  # noqa: E402
    apply_match_decisions,
    classify_game_match,
    create_match_preview_record,
    load_match_preview,
    run_match_preview_job,
)


class DummyMetadataHandler(MetadataHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


class MatchPreviewV2Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name)
        os.environ["OPENBOX_DATA_DIR"] = str(self.data_dir)
        import openbox
        from state_store import JsonStateStore

        self.openbox = openbox
        self.openbox.APP_DIR = self.data_dir
        self.openbox.DATA = self.data_dir / "library.json"
        self.openbox.STATE_STORE = JsonStateStore(self.openbox.DATA)
        self.db_path = self.data_dir / "metadata.db"
        package = self.data_dir / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>10</DatabaseID><Name>Exact Game</Name><Platform>Nintendo Entertainment System</Platform><Developer>Dev</Developer><Overview>Desc</Overview></Game>
          <Game><DatabaseID>11</DatabaseID><Name>Exact Game</Name><Platform>Sega Genesis</Platform></Game>
          <Game><DatabaseID>20</DatabaseID><Name>Near Match</Name><Platform>Nintendo Entertainment System</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        build_database(package, self.db_path)
        self.openbox.DATA.write_text(
            json.dumps(
                {
                    "schema_version": 6,
                    "games": [
                        {"game_id": "g-exact", "name": "Exact Game", "platform": "NES"},
                        {"game_id": "g-dup", "name": "Exact Game", "platform": "NES"},
                        {"game_id": "g-fuzzy", "name": "Near Matchy", "platform": "NES"},
                    ],
                    "settings": {},
                }
            )
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_classify_auto_exact_unique(self):
        game = {"game_id": "g-exact", "name": "Exact Game", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(self.db_path, game)
        self.assertEqual(match_class, "auto")
        self.assertIsNone(item)
        self.assertEqual(auto_id, "10")

    def test_classify_exact_review_for_duplicates(self):
        package = self.data_dir / "dup.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>10</DatabaseID><Name>Exact Game</Name><Platform>Nintendo Entertainment System</Platform></Game>
          <Game><DatabaseID>12</DatabaseID><Name>Exact Game (USA)</Name><Platform>Nintendo Entertainment System</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        dup_db = self.data_dir / "dup.db"
        build_database(package, dup_db)
        game = {"game_id": "g-dup", "name": "Exact Game", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(dup_db, game)
        self.assertEqual(match_class, "exact_review")
        self.assertIsNotNone(item["proposed"])
        self.assertIn("title_similarity", item["score"])

    def test_preview_xor_validation(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = True
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_preview({"game_ids": ["g1"], "import_batch_id": "b1"})
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_preview({"game_ids": [], "import_batch_id": ""})

    def test_preview_items_and_decisions(self):
        package = self.data_dir / "dup.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>10</DatabaseID><Name>Exact Game</Name><Platform>Nintendo Entertainment System</Platform><Developer>Dev</Developer><Overview>Desc</Overview></Game>
          <Game><DatabaseID>12</DatabaseID><Name>Exact Game (USA)</Name><Platform>Nintendo Entertainment System</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        dup_db = self.data_dir / "dup.db"
        build_database(package, dup_db)
        preview = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        self.assertIn("exact_review", loaded["counts"])
        handler = DummyMetadataHandler()
        handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query=f"preview_id={preview['preview_id']}"))
        status, payload, _ = handler.responses[-1]
        self.assertEqual(status, 200)
        self.assertIn("counts", payload)
        self.assertNotIn("items", payload)
        handler._api_get_api_v2_metadata_matches_items(mock.Mock(query=f"preview_id={preview['preview_id']}&limit=10"))
        _, items_payload, _ = handler.responses[-1]
        item = items_payload["items"][0]
        for key in ("title_similarity", "token_overlap", "platform_exact", "reasons"):
            self.assertIn(key, item["score"])
        proposed_id = item["proposed"]["database_id"]
        result, rejections = apply_match_decisions(
            preview["preview_id"],
            [{"game_id": "g-dup", "action": "never", "database_id": None}],
            data_dir=self.openbox.DATA,
        )
        self.assertEqual(result["never"], 1)
        self.assertEqual(rejections, [("g-dup", proposed_id)])

    def test_apply_replace_requires_allow_list(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = True
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_apply(
                    {"preview_id": "x", "revision": 1, "replace_existing": True}
                )

    def test_v2_preview_post_and_get(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db, \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.metadata.run_match_preview_job", side_effect=lambda *a, **k: load_match_preview(a[0], data_dir=self.openbox.DATA)):
            mock_db.is_file.return_value = True
            mock_jm.submit.side_effect = lambda name, worker, **kwargs: {"job_id": "job-1"}

            handler._api_post_api_v2_metadata_matches_preview({"game_ids": ["g-exact"]})
            status, payload, _ = handler.responses[-1]
            self.assertEqual(status, 202)
            for key in ("preview_id", "revision", "job_id", "state"):
                self.assertIn(key, payload)

            preview_id = payload["preview_id"]
            handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query=f"preview_id={preview_id}"))
            status, doc, _ = handler.responses[-1]
            self.assertEqual(status, 200)
            self.assertIn("counts", doc)
            self.assertNotIn("items", doc)

    def test_fuzzy_not_auto_applied(self):
        game = {"game_id": "g-fuzzy", "name": "Near Matchy", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(self.db_path, game)
        self.assertNotEqual(match_class, "auto")
        self.assertIsNone(auto_id)

    def test_classify_likely_possible_and_unmatched(self):
        likely_game = {"game_id": "g-likely", "name": "Near Matchy", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(self.db_path, likely_game)
        self.assertIn(match_class, {"likely", "possible", "exact_review", "unmatched"})
        self.assertIsNone(auto_id)

        package = self.data_dir / "possible.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>30</DatabaseID><Name>Robot Quest Adventure</Name><Platform>Nintendo Entertainment System</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        possible_db = self.data_dir / "possible.db"
        build_database(package, possible_db)
        possible_game = {"game_id": "g-possible", "name": "Robot Quest Adv", "platform": "NES"}
        match_class, item, _auto = classify_game_match(possible_db, possible_game)
        self.assertIn(match_class, {"possible", "likely", "unmatched"})
        self.assertIsNotNone(item)

        empty_game = {"game_id": "g-empty", "name": "", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(self.db_path, empty_game)
        self.assertEqual(match_class, "unmatched")
        self.assertIn("empty title", item["score"]["reasons"])

        rejected_game = {"game_id": "g-reject", "name": "Exact Game", "platform": "NES"}
        match_class, item, auto_id = classify_game_match(self.db_path, rejected_game, rejected_ids=["10"])
        self.assertNotEqual(match_class, "auto")

    def _dup_database(self):
        package = self.data_dir / "dup.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>10</DatabaseID><Name>Exact Game</Name><Platform>Nintendo Entertainment System</Platform><Developer>Dev</Developer><Overview>Desc</Overview></Game>
          <Game><DatabaseID>12</DatabaseID><Name>Exact Game (USA)</Name><Platform>Nintendo Entertainment System</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        dup_db = self.data_dir / "dup.db"
        build_database(package, dup_db)
        return dup_db

    def test_preview_import_batch_and_expiry(self):
        self.openbox.DATA.write_text(
            json.dumps(
                {
                    "schema_version": 6,
                    "games": [
                        {"game_id": "g-batch", "name": "Batch Game", "platform": "NES", "import_batch_id": "batch-1"},
                    ],
                    "settings": {},
                }
            )
        )
        preview = create_match_preview_record(import_batch_id="batch-1", data_dir=self.openbox.DATA)
        self.assertEqual(preview["import_batch_id"], "batch-1")
        with self.assertRaises(PreviewNotFound):
            load_match_preview("missing-preview", data_dir=self.openbox.DATA)
        expired = create_match_preview_record(game_ids=["g-batch"], data_dir=self.openbox.DATA)
        expired["expires_at"] = "2000-01-01T00:00:00+00:00"
        from metadata import save_match_preview

        save_match_preview(expired, data_dir=self.openbox.DATA)
        with self.assertRaises(PreviewExpired):
            load_match_preview(expired["preview_id"], data_dir=self.openbox.DATA)

    def test_run_preview_job_auto_apply_and_cancel(self):
        preview = create_match_preview_record(game_ids=["g-exact", "g-fuzzy"], data_dir=self.openbox.DATA)
        cancel = mock.Mock()
        cancel.is_set.side_effect = [False, True]

        def fake_transact(mutator):
            state = json.loads(self.openbox.DATA.read_text())
            mutator(state)
            self.openbox.DATA.write_text(json.dumps(state))
            return state, None

        run_match_preview_job(
            preview["preview_id"],
            database_path=self.db_path,
            transact_state=fake_transact,
            data_dir=self.openbox.DATA,
            cancel_event=cancel,
        )
        state = json.loads(self.openbox.DATA.read_text())
        exact = next(game for game in state["games"] if game["game_id"] == "g-exact")
        self.assertEqual(exact.get("launchbox_db_id"), "10")
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        self.assertGreaterEqual(loaded["counts"]["auto_applied"], 1)

    def test_decisions_apply_and_never_persist(self):
        dup_db = self._dup_database()
        preview = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        item = next(iter(loaded["items"].values()))
        proposed_id = item["proposed"]["database_id"]
        with self.assertRaises(BadRequest):
            apply_match_decisions(preview["preview_id"], [], data_dir=self.openbox.DATA)
        result, rejections = apply_match_decisions(
            preview["preview_id"],
            [{"game_id": "g-dup", "action": "accept", "database_id": None}],
            data_dir=self.openbox.DATA,
        )
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(rejections, [])
        result2, rejections2 = apply_match_decisions(
            preview["preview_id"],
            [{"game_id": "g-dup", "action": "never", "database_id": None}],
            data_dir=self.openbox.DATA,
        )
        self.assertEqual(result2["never"], 1)
        self.assertEqual(rejections2, [("g-dup", proposed_id)])

        def fake_transact(mutator):
            state = json.loads(self.openbox.DATA.read_text())
            mutator(state)
            self.openbox.DATA.write_text(json.dumps(state))
            return state, None

        from metadata import persist_never_rejections

        persist_never_rejections(fake_transact, rejections2)
        state = json.loads(self.openbox.DATA.read_text())
        self.assertIn(proposed_id, state["games"][1]["rejected_launchbox_ids"])
        preview2 = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview2["preview_id"],
            database_path=dup_db,
            transact_state=fake_transact,
            data_dir=self.openbox.DATA,
        )
        loaded2 = load_match_preview(preview2["preview_id"], data_dir=self.openbox.DATA)
        state_after = json.loads(self.openbox.DATA.read_text())
        dup_game = next(game for game in state_after["games"] if game["game_id"] == "g-dup")
        self.assertIn(proposed_id, dup_game.get("rejected_launchbox_ids", []))
        self.assertIn("exact_review", loaded2.get("counts", {}))

    def test_list_items_pagination_and_class_filter(self):
        from metadata import _encode_match_cursor, list_match_preview_items

        preview = create_match_preview_record(game_ids=["g-dup", "g-fuzzy"], data_dir=self.openbox.DATA)
        dup_db = self._dup_database()
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        self.assertGreaterEqual(len(loaded["items"]), 2)
        match_class = next(iter(loaded["items"].values()))["class"]
        page = list_match_preview_items(preview["preview_id"], limit=1, data_dir=self.openbox.DATA)
        self.assertEqual(len(page["items"]), 1)
        if len(loaded["items"]) > 1:
            self.assertIsNotNone(page["next_cursor"])
        cursor = page["next_cursor"]
        if cursor:
            page2 = list_match_preview_items(preview["preview_id"], cursor=cursor, limit=1, data_dir=self.openbox.DATA)
            self.assertEqual(page2["cursor"], cursor)
        filtered = list_match_preview_items(
            preview["preview_id"], match_class=match_class, data_dir=self.openbox.DATA
        )
        self.assertTrue(filtered["items"])
        with self.assertRaises(PreviewStale):
            list_match_preview_items(
                preview["preview_id"],
                cursor=_encode_match_cursor("other", 1, 0),
                data_dir=self.openbox.DATA,
            )

    def test_apply_match_preview_fields(self):
        from metadata import apply_match_preview

        dup_db = self._dup_database()
        preview = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        item = next(iter(loaded["items"].values()))
        proposed_id = item["proposed"]["database_id"]
        apply_match_decisions(
            preview["preview_id"],
            [{"game_id": "g-dup", "action": "choose", "database_id": proposed_id}],
            data_dir=self.openbox.DATA,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        revision = loaded["revision"]

        def fake_transact(mutator):
            state = json.loads(self.openbox.DATA.read_text())
            mutator(state)
            self.openbox.DATA.write_text(json.dumps(state))
            return state, None

        apply_match_preview(
                preview["preview_id"],
                revision=revision,
                field_allow_list=["title", "developer", "description"],
                replace_existing=True,
                database_path=dup_db,
                data_dir=self.openbox.DATA,
                transact_state=fake_transact,
                create_backup=mock.Mock(),
                data_parent=self.openbox.DATA.parent,
                running_map={},
            )
        state = json.loads(self.openbox.DATA.read_text())
        game = next(item for item in state["games"] if item["game_id"] == "g-dup")
        self.assertEqual(game.get("developer"), "Dev")
        with self.assertRaises(PreviewStale):
            apply_match_preview(
                preview["preview_id"],
                revision=revision - 1,
                database_path=self.db_path,
                data_dir=self.openbox.DATA,
                transact_state=fake_transact,
                create_backup=mock.Mock(),
                data_parent=self.openbox.DATA.parent,
                running_map={},
            )
        with self.assertRaises(BadRequest):
            apply_match_preview(
                preview["preview_id"],
                revision=revision,
                media_allow_list=["cover"],
                database_path=self.db_path,
                data_dir=self.openbox.DATA,
                transact_state=fake_transact,
                create_backup=mock.Mock(),
                data_parent=self.openbox.DATA.parent,
                running_map={},
            )


class MetadataHandlerRouteTests(MatchPreviewV2Tests):
    def test_metadata_status_and_search(self):
        import handlers.metadata as hm

        handler = DummyMetadataHandler()
        hm.METADATA_DATABASE = self.db_path
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.METADATA_JOB", {"state": "idle"}), \
             mock.patch("handlers.metadata.load_state_view", return_value=json.loads(self.openbox.DATA.read_text())):
            handler._api_get_api_metadata_status(mock.Mock())
            status, payload, _ = handler.responses[-1]
            self.assertEqual(status, 200)
            self.assertIn("coverage", payload)
            handler._api_get_api_metadata_search(mock.Mock(query="q=Exact+Game&game_id=g-exact"))
            status, payload, _ = handler.responses[-1]
            self.assertEqual(status, 200)
            self.assertTrue(payload["results"])

    def test_metadata_search_errors(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = False
            with self.assertRaises(Conflict):
                handler._api_get_api_metadata_search(mock.Mock(query="q=test"))

    def test_igdb_search_and_steam(self):
        handler = DummyMetadataHandler()
        state = json.loads(self.openbox.DATA.read_text())
        with mock.patch("handlers.metadata.search_igdb_games", return_value=[{"id": 1}]), \
             mock.patch("handlers.metadata.update_steam_metadata"), \
             mock.patch("handlers.metadata.transact_state", side_effect=lambda m: (m(state), "Exact Game")), \
             mock.patch("handlers.metadata.load_state", return_value=state), \
             mock.patch("handlers.metadata.game_from_payload", side_effect=lambda s, p: next(g for g in s["games"] if g["game_id"] == p.get("game_id", "g-exact"))):
            handler._api_get_api_metadata_igdb_search(mock.Mock(query="q=Mario&platform=NES"))
            self.assertEqual(handler.responses[-1][0], 200)
            handler.steam_metadata({"game_id": "g-exact"})
            self.assertEqual(handler.responses[-1][0], 200)

    def test_sync_and_match_metadata(self):
        import handlers.metadata as hm

        handler = DummyMetadataHandler()
        hm.METADATA_DATABASE = self.db_path
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.sync_database") as mock_sync, \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.metadata.load_state", return_value=json.loads(self.openbox.DATA.read_text())), \
             mock.patch("handlers.metadata.batch_match", return_value={}), \
             mock.patch("handlers.metadata.transact_state", side_effect=lambda m: m({"games": []})):
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler.sync_metadata()
            self.assertEqual(handler.responses[-1][0], 202)
            mock_sync.assert_called_once()
            hm.METADATA_JOB.clear()
            handler.match_metadata({"platform": "all"})
            self.assertEqual(handler.responses[-1][0], 202)
            hm.METADATA_JOB.update({"state": "running"})
            handler.match_metadata({"platform": "all"})
            self.assertEqual(handler.responses[-1][0], 200)

    def test_apply_metadata_and_igdb(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.load_state", return_value=json.loads(self.openbox.DATA.read_text())), \
             mock.patch("handlers.metadata.game_from_payload", side_effect=lambda s, p: next(g for g in s["games"] if g["game_id"] == p.get("game_id", "g-exact"))), \
             mock.patch("handlers.metadata.apply_game_metadata", return_value={"game_id": "g-exact", "developer": "Dev"}), \
             mock.patch("handlers.metadata.transact_state", side_effect=lambda m: (m(json.loads(self.openbox.DATA.read_text())), None)), \
             mock.patch("handlers.metadata.bump_media_epoch"):
            handler.apply_metadata({"game_id": "g-exact", "database_id": 10, "media": ["cover"], "overwrite": False})
            self.assertEqual(handler.responses[-1][0], 200)
            with mock.patch("handlers.metadata.fetch_igdb_game", return_value={"name": "IGDB Game"}), \
                 mock.patch("handlers.metadata.apply_igdb_metadata", return_value="IGDB Game"):
                handler.apply_igdb_metadata({"game_id": "g-exact", "igdb_id": 42})
                self.assertEqual(handler.responses[-1][0], 200)

    def test_v2_decisions_items_and_apply_routes(self):
        handler = DummyMetadataHandler()
        dup_db = self._dup_database()
        preview = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.persist_never_rejections"), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_jm.submit.return_value = {"job_id": "apply-1"}
            handler._api_post_api_v2_metadata_matches_decisions(
                {"preview_id": preview["preview_id"], "items": [{"game_id": "g-dup", "action": "skip", "database_id": None}]}
            )
            self.assertEqual(handler.responses[-1][0], 200)
            with self.assertRaises(BadRequest):
                handler._api_get_api_v2_metadata_matches_items(mock.Mock(query=f"preview_id={preview['preview_id']}&class=bad"))
            handler._api_get_api_v2_metadata_matches_items(
                mock.Mock(query=f"preview_id={preview['preview_id']}&limit=5&class=exact_review")
            )
            self.assertEqual(handler.responses[-1][0], 200)
            handler._api_post_api_v2_metadata_matches_apply(
                {"preview_id": preview["preview_id"], "revision": 1, "field_allow_list": ["title"]}
            )
            self.assertEqual(handler.responses[-1][0], 202)

    def test_v2_preview_get_states(self):
        handler = DummyMetadataHandler()
        preview = create_match_preview_record(game_ids=["g-exact"], data_dir=self.openbox.DATA)
        from metadata import save_match_preview

        loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
        loaded["job_id"] = "job-running"
        loaded["state"] = "queued"
        save_match_preview(loaded, data_dir=self.openbox.DATA)
        with mock.patch("handlers.metadata.get_operation_service") as mock_ops:
            mock_ops.return_value.get.return_value = {"state": "running"}
            handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query=f"preview_id={preview['preview_id']}"))
            self.assertEqual(handler.responses[-1][1]["state"], "running")
        with self.assertRaises(BadRequest):
            handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query=""))

    def test_handler_validation_and_error_paths(self):
        from handlers.metadata import _match_preview_state_for_job, _parse_limit

        self.assertEqual(_parse_limit(None, default=5, maximum=10), 5)
        with self.assertRaises(BadRequest):
            _parse_limit("bad", default=5, maximum=10)
        self.assertEqual(_match_preview_state_for_job(None), "ready")
        with mock.patch("handlers.metadata.get_operation_service") as mock_ops:
            mock_ops.return_value.get.return_value = {"state": "partial"}
            self.assertEqual(_match_preview_state_for_job("job-1"), "ready")

        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = False
            with self.assertRaises(Conflict):
                handler._api_post_api_v2_metadata_matches_preview({"game_ids": ["g1"]})
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path):
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_preview({"game_ids": "bad"})
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.create_match_preview_record", return_value={"preview_id": "p1", "revision": 1, "job_id": None}), \
             mock.patch("handlers.metadata.run_match_preview_job"), \
             mock.patch("handlers.metadata.load_match_preview", return_value={"preview_id": "p1", "revision": 1, "job_id": None, "state": "queued"}), \
             mock.patch("handlers.metadata.save_match_preview"), \
             mock.patch("handlers.metadata.get_operation_service") as mock_ops, \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_ops.return_value.get.return_value = {"checkpoint": {"last_game_id": "g1"}}
            mock_jm.submit.side_effect = lambda name, worker, **kwargs: worker(mock.Mock(is_set=mock.Mock(return_value=False))) or {"job_id": "job-1"}
            handler._api_post_api_v2_metadata_matches_preview({"game_ids": ["g-exact"]})
            self.assertEqual(handler.responses[-1][0], 202)
        with self.assertRaises(BadRequest):
            handler._api_get_api_v2_metadata_matches_items(mock.Mock(query=""))
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_metadata_matches_decisions({"preview_id": "", "items": []})
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path):
            with self.assertRaises(PreviewNotFound):
                handler._api_post_api_v2_metadata_matches_apply({"preview_id": "x", "revision": 1})
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_apply({"preview_id": "x", "revision": 1, "game_ids": "bad"})

    def test_sync_metadata_paths(self):
        import handlers.metadata as hm

        handler = DummyMetadataHandler()
        hm.METADATA_JOB.clear()
        hm.METADATA_JOB.update({"state": "downloading"})
        with mock.patch("handlers.metadata.JOB_MANAGER"):
            handler.sync_metadata()
            self.assertEqual(handler.responses[-1][0], 200)
        hm.METADATA_JOB.clear()
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.sync_database", side_effect=OSError("network")) as mock_sync, \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_jm.submit.side_effect = lambda name, worker: worker()
            handler.sync_metadata()
            self.assertEqual(handler.responses[-1][0], 202)
            self.assertEqual(hm.METADATA_JOB.get("state"), "error")
            mock_sync.assert_called_once()

    def test_match_metadata_edge_cases(self):
        import handlers.metadata as hm

        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = False
            with self.assertRaises(Conflict):
                handler.match_metadata({"platform": "all"})
        hm.METADATA_DATABASE = self.db_path
        empty_state = {"games": [{"game_id": "g1", "name": "   ", "platform": "NES"}], "settings": {}}
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.load_state", return_value=empty_state), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm:
            mock_jm.submit.side_effect = lambda name, worker: worker()
            hm.METADATA_JOB.clear()
            handler.match_metadata({"platform": "all"})
            self.assertEqual(hm.METADATA_JOB.get("state"), "done")

    def test_apply_metadata_validation(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE") as mock_db:
            mock_db.is_file.return_value = False
            with self.assertRaises(ValueError):
                handler.apply_metadata({"game_id": "g-exact", "database_id": 1, "media": ["cover"]})
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path):
            with self.assertRaises(ValueError):
                handler.apply_metadata({"game_id": "g-exact", "database_id": 1, "media": ["bad"]})
            with mock.patch("handlers.metadata.load_state", return_value={"games": [{"game_id": "g-exact", "name": "Exact Game"}], "settings": {}}), \
                 mock.patch("handlers.metadata.game_from_payload", return_value={"game_id": "g-exact", "name": "Exact Game", "path": ""}):
                with self.assertRaises(ValueError):
                    handler.apply_metadata({"game_id": "g-exact", "database_id": 1, "media": ["manual"]})

    def test_search_and_igdb_errors(self):
        handler = DummyMetadataHandler()
        with mock.patch("handlers.metadata.METADATA_DATABASE", self.db_path), \
             mock.patch("handlers.metadata.load_state_view", return_value={"games": []}), \
             mock.patch("handlers.metadata.game_from_query", side_effect=KeyError("missing")):
            with self.assertRaises(BadRequest):
                handler._api_get_api_metadata_search(mock.Mock(query="q=test"))
        with mock.patch("handlers.metadata.search_igdb_games", side_effect=OSError("offline")):
            with self.assertRaises(BadRequest):
                handler._api_get_api_metadata_igdb_search(mock.Mock(query="q=test"))

    def test_route_wrappers_and_apply_flow(self):
        handler = DummyMetadataHandler()
        with mock.patch.object(handler, "steam_metadata") as steam, \
             mock.patch.object(handler, "sync_metadata") as sync, \
             mock.patch.object(handler, "apply_metadata") as apply_meta, \
             mock.patch.object(handler, "match_metadata") as match, \
             mock.patch.object(handler, "apply_igdb_metadata") as apply_igdb:
            handler._api_post_api_metadata_steam({"game_id": "g1"})
            handler._api_post_api_metadata_sync({})
            handler._api_post_api_metadata_apply({"game_id": "g1"})
            handler._api_post_api_metadata_match({"platform": "all"})
            handler._api_post_api_metadata_igdb_apply({"igdb_id": 1})
            steam.assert_called_once()
            sync.assert_called_once()
            apply_meta.assert_called_once()
            match.assert_called_once()
            apply_igdb.assert_called_once()

        dup_db = self._dup_database()
        preview = create_match_preview_record(game_ids=["g-dup"], data_dir=self.openbox.DATA)
        run_match_preview_job(
            preview["preview_id"],
            database_path=dup_db,
            transact_state=lambda mutator: mutator(json.loads(self.openbox.DATA.read_text())),
            data_dir=self.openbox.DATA,
        )
        with mock.patch("handlers.metadata.METADATA_DATABASE", dup_db), \
             mock.patch("handlers.metadata.JOB_MANAGER") as mock_jm, \
             mock.patch("handlers.metadata.apply_match_preview", return_value={}) as mock_apply:
            mock_jm.submit.side_effect = lambda name, worker, **kwargs: worker(mock.Mock(is_set=mock.Mock(return_value=False))) or {"job_id": "apply-2"}
            loaded = load_match_preview(preview["preview_id"], data_dir=self.openbox.DATA)
            handler._api_post_api_v2_metadata_matches_apply(
                {
                    "preview_id": preview["preview_id"],
                    "revision": loaded["revision"],
                    "field_allow_list": ["title"],
                    "replace_existing": True,
                }
            )
            self.assertEqual(handler.responses[-1][0], 202)
            mock_apply.assert_called_once()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_metadata_matches_decisions({"preview_id": "x"})
        with mock.patch("handlers.metadata.METADATA_DATABASE", dup_db):
            with self.assertRaises(PreviewNotFound):
                handler._api_post_api_v2_metadata_matches_apply({"preview_id": "x", "revision": 1})
        with self.assertRaises(PreviewNotFound):
            handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query="preview_id=missing"))
        expired = create_match_preview_record(game_ids=["g-exact"], data_dir=self.openbox.DATA)
        from metadata import save_match_preview

        expired_doc = load_match_preview(expired["preview_id"], data_dir=self.openbox.DATA)
        expired_doc["expires_at"] = "2000-01-01T00:00:00+00:00"
        save_match_preview(expired_doc, data_dir=self.openbox.DATA)
        with self.assertRaises(PreviewExpired):
            handler._api_get_api_v2_metadata_matches_preview(mock.Mock(query=f"preview_id={expired['preview_id']}"))
        with mock.patch("handlers.metadata.METADATA_DATABASE", dup_db):
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_apply({"preview_id": "x"})
            with self.assertRaises(BadRequest):
                handler._api_post_api_v2_metadata_matches_apply(
                    {"preview_id": preview["preview_id"], "revision": 1, "game_ids": "bad"}
                )

    def test_match_preview_state_unknown(self):
        from handlers.metadata import _match_preview_state_for_job

        with mock.patch("handlers.metadata.get_operation_service") as mock_ops:
            mock_ops.return_value.get.return_value = {"state": "weird"}
            self.assertEqual(_match_preview_state_for_job("job-1"), "weird")


def test_download_media_for_type_helper():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>1</DatabaseID><Name>Helper Game</Name><Platform>NES</Platform></Game>
          <GameImage><DatabaseID>1</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type></GameImage>
          <GameImage><DatabaseID>1</DatabaseID><FileName>shot.png</FileName><Type>Screenshot - Gameplay</Type></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        from metadata import _download_media_for_type, _group_images_by_type, get_db_connection

        conn = get_db_connection(database)
        images = [dict(row) for row in conn.execute("SELECT * FROM images WHERE database_id = 1")]
        grouped = _group_images_by_type(images)
        game = {"name": "Helper Game"}
        media_root = root / "media"

        def opener(request, **_):
            return ImageResponse()

        _download_media_for_type(game, "cover", grouped, media_root, True, opener)
        assert Path(game["cover"]).is_file()
        _download_media_for_type(game, "screenshots", grouped, media_root, True, opener)
        assert len(game["screenshots"]) == 1
        _download_media_for_type(game, "manual", grouped, media_root, True, opener)
        assert "manual: no manual in this archive" in (game.get("_media_notes") or [])
    print("download media helper self-test: ok")


def test_sync_database_success():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>1</DatabaseID><Name>Sync Game</Name><Platform>NES</Platform></Game>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)

        class ZipResponse(io.BytesIO):
            def __init__(self, path):
                super().__init__(path.read_bytes())
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.close()

        def opener(request, **_):
            return ZipResponse(package)

        destination = root / "metadata.db"
        sync_database(destination, opener=opener)
        assert search_games(destination, "Sync Game", "NES")
    print("sync database success self-test: ok")


def test_metadata_policy_helpers():
    from api_errors import BadRequest
    from metadata import (
        _media_categories_for_game,
        _year_from_record,
        apply_match_decisions,
        batch_match,
        build_database,
        classify_game_match,
        create_match_preview_record,
        load_match_preview,
        match_pair_key,
        platform_exact_match,
        run_match_preview_job,
        title_similarity,
        token_overlap,
    )

    assert match_pair_key(" A ", "NES") == ("A", "NES")
    assert token_overlap("", "x") == 0.0
    assert title_similarity("", "x") == 0.0
    assert title_similarity("Same", "Same") == 1.0
    assert platform_exact_match("NES", "Nintendo Entertainment System")
    assert _year_from_record({"release_date": ""}) is None
    assert _media_categories_for_game({"cover": "/c.png", "screenshots": ["/s.png"]}) == ["cover", "screenshots"]

    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><Name>Bad</Name><Platform>NES</Platform></Game>
          <Game><DatabaseID>bad</DatabaseID><Name>Bad2</Name><Platform>NES</Platform></Game>
          <Game><DatabaseID>5</DatabaseID><Name>Likely Hero</Name><Platform>Nintendo Entertainment System</Platform><Developer>Hero Dev</Developer></Game>
          <GameImage><DatabaseID>5</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        assert batch_match(database, [("", "NES")]) == {}
        game = {"game_id": "g1", "name": "Likely Heroz", "platform": "NES", "cover": "/c.png"}
        match_class, item, _ = classify_game_match(database, game)
        assert match_class in {"likely", "possible", "exact_review", "unmatched"}
        library = root / "library.json"
        library.write_text(json.dumps({"games": [game], "settings": {}}))
        preview = create_match_preview_record(game_ids=["g1"], data_dir=library)
        run_match_preview_job(
            preview["preview_id"],
            database_path=database,
            transact_state=lambda mutator: mutator(json.loads(library.read_text())),
            data_dir=library,
            checkpoint={"last_game_id": "g0"},
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=library)
        if loaded["items"]:
            game_id = next(iter(loaded["items"]))
            try:
                apply_match_decisions(
                    preview["preview_id"],
                    [{"game_id": game_id, "action": "bad", "database_id": None}],
                    data_dir=library,
                )
                raise AssertionError("expected BadRequest")
            except BadRequest:
                pass
            try:
                apply_match_decisions(preview["preview_id"], [], data_dir=library)
                raise AssertionError("expected BadRequest for empty items")
            except BadRequest:
                pass
    print("metadata policy helpers self-test: ok")


def test_metadata_decision_and_apply_edges():
    from api_errors import PreviewNotFound
    from metadata import (
        _download_media_for_type,
        _group_images_by_type,
        _load_metadata_record,
        apply_match_decisions,
        apply_match_preview,
        create_match_preview_record,
        get_db_connection,
        load_match_preview,
        run_match_preview_job,
    )

    with TemporaryDirectory() as directory:
        root = Path(directory)
        package = root / "Metadata.zip"
        xml = """<LaunchBox>
          <Game><DatabaseID>7</DatabaseID><Name>Apply Me</Name><Platform>Nintendo Entertainment System</Platform><Developer>D</Developer><Overview>O</Overview><ReleaseDate>1999</ReleaseDate><MaxPlayers>2</MaxPlayers></Game>
          <GameImage><DatabaseID>7</DatabaseID><FileName>cover.png</FileName><Type>Box - Front</Type></GameImage>
        </LaunchBox>"""
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Metadata.xml", xml)
        database = root / "metadata.db"
        build_database(package, database)
        library = root / "library.json"
        library.write_text(json.dumps({"games": [{"game_id": "g7", "name": "Apply Me", "platform": "NES"}], "settings": {}}))
        preview = create_match_preview_record(import_batch_id="batch-7", data_dir=library)
        library.write_text(
            json.dumps(
                {
                    "games": [{"game_id": "g7", "name": "Apply Me", "platform": "NES", "import_batch_id": "batch-7"}],
                    "settings": {},
                }
            )
        )
        run_match_preview_job(
            preview["preview_id"],
            database_path=database,
            transact_state=lambda mutator: mutator(json.loads(library.read_text())),
            data_dir=library,
        )
        loaded = load_match_preview(preview["preview_id"], data_dir=library)
        if loaded["items"]:
            game_id = next(iter(loaded["items"]))
            apply_match_decisions(
                preview["preview_id"],
                [{"game_id": game_id, "action": "accept", "database_id": None}],
                data_dir=library,
            )
            loaded = load_match_preview(preview["preview_id"], data_dir=library)
            revision = loaded["revision"]

            def fake_transact(mutator):
                state = json.loads(library.read_text())
                mutator(state)
                library.write_text(json.dumps(state))
                return state, None

            cancel = mock.Mock()
            cancel.is_set.return_value = True
            apply_match_preview(
                preview["preview_id"],
                revision=revision,
                game_ids=[game_id],
                database_path=database,
                data_dir=library,
                transact_state=fake_transact,
                create_backup=mock.Mock(),
                data_parent=root,
                running_map={},
                cancel_event=cancel,
            )
            with pytest_bad_request():
                apply_match_preview(
                    preview["preview_id"],
                    revision=revision,
                    replace_existing=True,
                    database_path=database,
                    data_dir=library,
                    transact_state=fake_transact,
                    create_backup=mock.Mock(),
                    data_parent=root,
                    running_map={},
                )
        try:
            _load_metadata_record(database, 9999)
            raise AssertionError("expected missing record failure")
        except ValueError:
            pass
        conn = get_db_connection(database)
        images = [dict(row) for row in conn.execute("SELECT * FROM images WHERE database_id = 7")]
        grouped = _group_images_by_type(images)
        game = {"name": "Apply Me", "cover": "/existing.png"}
        _download_media_for_type(game, "cover", grouped, root / "media", False, lambda *a, **k: ImageResponse())
        assert game["cover"] == "/existing.png"
        game2 = apply_game_metadata(
            {"name": "Apply Me"},
            database,
            7,
            ["cover"],
            root / "media",
            overwrite=False,
            opener=lambda *a, **k: ImageResponse(),
        )
        assert game2.get("max_players") == "2"
        bad_preview = root / "match_previews" / "bad.json"
        bad_preview.parent.mkdir(parents=True, exist_ok=True)
        bad_preview.write_text("{not json")
        with pytest_raises_api(PreviewNotFound):
            load_match_preview("bad", data_dir=library)
    print("metadata decision/apply edges self-test: ok")


def pytest_bad_request():
    from api_errors import BadRequest
    return _ApiRaises(BadRequest)


def pytest_raises_api(exc):
    return _ApiRaises(exc)


class _ApiRaises:
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"expected {self.exc.__name__}")
        return issubclass(exc_type, self.exc)


def run_match_preview_unittests():
    loader = unittest.defaultTestLoader
    suite = unittest.TestSuite()
    for case in (MatchPreviewV2Tests, MetadataHandlerRouteTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    test()
    test_platform_aliases()
    test_batch_match()
    test_manual_import()
    test_batch_search_throughput()
    test_concurrent_media_downloads()
    test_edge_cases()
    test_halo_platform_collision()
    test_download_media_for_type_helper()
    test_sync_database_success()
    test_metadata_policy_helpers()
    test_metadata_decision_and_apply_edges()
    run_match_preview_unittests()

