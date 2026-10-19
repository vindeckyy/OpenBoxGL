#!/usr/bin/env python3
"""Metadata provider registry tests (P8.4)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pkg" / "parity"))

import pkg.parity  # noqa: F401,E402  # register flat-import finder

from parity_metadata_providers import (
    APPLY_FIELDS,
    BUILTIN_PROVIDER_ORDER,
    BUILTIN_PROVIDERS,
    apply_candidate,
    clean_media_url,
    download_candidate_media,
    match_rank,
    normalize_candidate,
    normalize_title,
    platform_equivalent,
    provider_kind_hint,
    provider_priority,
    rank_candidates,
    search_all,
)


class PriorityTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch(
            "parity_metadata_providers._enabled_plugin_provider_ids", return_value=[]
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_default_priority(self):
        self.assertEqual(provider_priority({"settings": {}}), list(BUILTIN_PROVIDER_ORDER))

    def test_custom_priority_keeps_plugins_and_appends_missing(self):
        priority = provider_priority({
            "settings": {"metadata_provider_priority": ["igdb", "plugin:foo", "bogus", "igdb"]}
        })
        self.assertEqual(priority[:2], ["igdb", "plugin:foo"])
        for name in BUILTIN_PROVIDER_ORDER:
            self.assertIn(name, priority)
        self.assertNotIn("bogus", priority)

    def test_enabled_plugin_providers_append_after_builtins(self):
        with mock.patch(
            "parity_metadata_providers._enabled_plugin_provider_ids",
            return_value=["plugin:extra"],
        ):
            priority = provider_priority({"settings": {}})
        self.assertEqual(priority[-1], "plugin:extra")
        self.assertEqual(priority[0], BUILTIN_PROVIDER_ORDER[0])

    def test_plugin_priority_moves_plugins_first(self):
        with mock.patch(
            "parity_metadata_providers._enabled_plugin_provider_ids",
            return_value=["plugin:extra"],
        ):
            priority = provider_priority({"settings": {"plugin_metadata_priority": True}})
        self.assertEqual(priority[:2], ["plugin:extra", BUILTIN_PROVIDER_ORDER[0]])

    def test_priority_capped(self):
        raw = [f"plugin:{index}" for index in range(30)]
        priority = provider_priority({"settings": {"metadata_provider_priority": raw}})
        self.assertLessEqual(len(priority), 20 + len(BUILTIN_PROVIDER_ORDER))


class NormalizationTests(unittest.TestCase):
    def test_title_articles_and_punctuation(self):
        self.assertEqual(normalize_title("The Legend of Zelda"), normalize_title("Legend of Zelda, The"))
        self.assertEqual(normalize_title("Rock & Roll Racing"), "rock and roll racing")
        self.assertEqual(normalize_title("Sonic: The Hedgehog!"), "sonic the hedgehog")

    def test_title_query_variants(self):
        from parity_metadata_providers import title_query_variants

        variants = title_query_variants("Legend of Zelda, The")
        self.assertIn("The Legend of Zelda", variants)
        self.assertIn("Legend of Zelda", variants)
        self.assertIn("Legend of Zelda", title_query_variants("The Legend of Zelda"))

    def test_launchbox_adapter_matches_article_shifted_title(self):
        import zipfile

        import metadata as metadata_module
        from parity_metadata_providers import LaunchBoxProvider

        xml = (
            "<LaunchBox><Game><DatabaseID>9</DatabaseID><Name>The Legend of Zelda</Name>"
            "<Platform>Nintendo Entertainment System</Platform><ReleaseDate>1986-01-01</ReleaseDate>"
            "<Developer>Nintendo</Developer></Game>"
            "<GameImage><DatabaseID>9</DatabaseID><FileName>cover.png</FileName>"
            "<Type>Box - Front</Type><Region>North America</Region></GameImage></LaunchBox>"
        )
        with tempfile.TemporaryDirectory() as folder:
            package = Path(folder) / "Metadata.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("Metadata.xml", xml)
            database = Path(folder) / "metadata.db"
            metadata_module.build_database(package, database)
            try:
                with mock.patch.object(LaunchBoxProvider, "database_path", return_value=database):
                    results = search_all(
                        {}, {"name": "Legend of Zelda, The", "platform": "NES"}, providers=["launchbox"],
                    )
            finally:
                metadata_module.close_db_connections()
        self.assertEqual(results[0]["id"], "9")
        self.assertIn("https://images.launchbox-app.com/cover.png", results[0]["media"]["cover"])

    def test_platform_alias(self):
        self.assertTrue(platform_equivalent("NES", "Nintendo Entertainment System"))
        self.assertFalse(platform_equivalent("NES", "SNES"))

    def test_rank_exact_before_substring(self):
        candidates = [
            {"id": "2", "name": "Zelda II: The Adventure of Link", "platform": "NES", "fields": {}},
            {"id": "1", "name": "Legend of Zelda, The", "platform": "NES", "fields": {}},
        ]
        ranked = rank_candidates(candidates, "The Legend of Zelda", "NES")
        self.assertEqual([item["id"] for item in ranked], ["1", "2"])
        self.assertEqual(match_rank("The Legend of Zelda", "Legend of Zelda, The"), 0)
        self.assertEqual(match_rank("Zelda", "The Legend of Zelda"), 2)

    def test_normalize_candidate_caps_media_and_urls(self):
        raw = {
            "id": 5,
            "name": "Game",
            "platform": "SNES",
            "fields": {"name": "Game", "path": "/etc/passwd"},
            "media": {
                "cover": [f"https://img.example/{index}.png" for index in range(20)],
                "background": ["http://insecure.example/a.png"],
            },
        }
        candidate = normalize_candidate(raw, "launchbox")
        self.assertEqual(len(candidate["media"]["cover"]), 10)
        self.assertNotIn("background", candidate["media"])
        self.assertEqual(candidate["fields"]["name"], "Game")
        self.assertNotIn("path", candidate["fields"])
        self.assertEqual(candidate["source"], "launchbox")


class ApplyTests(unittest.TestCase):
    def test_preserves_existing_fields_unless_overwrite(self):
        game = {"name": "Old", "description": "", "cover": "/media/old.jpg"}
        candidate = {
            "id": "1",
            "source": "launchbox",
            "name": "New",
            "fields": {"name": "New", "description": "Fresh", "cover": "/remote/cover.jpg"},
            "media": {"cover": ["https://x.example/c.jpg"]},
        }
        apply_candidate(game, candidate)
        self.assertEqual(game["name"], "Old")
        self.assertEqual(game["description"], "Fresh")
        self.assertEqual(game["cover"], "/media/old.jpg")
        apply_candidate(game, candidate, overwrite=True)
        self.assertEqual(game["name"], "New")

    def test_local_media_only_for_known_kinds_and_never_urls(self):
        game = {"screenshots": ["/media/a.png"]}
        candidate = {"fields": {}, "media": {"cover": ["https://x.example/c.png"]}}
        apply_candidate(game, candidate, local_media={
            "cover": "/media/local.png",
            "screenshots": "/media/b.png",
            "bogus": "/media/nope.png",
            "video_recording": "/media/skip.mp4",
        })
        self.assertEqual(game["cover"], "/media/local.png")
        self.assertEqual(game["screenshots"], ["/media/a.png", "/media/b.png"])
        self.assertNotIn("bogus", game)
        self.assertNotIn("video_recording", game)
        self.assertNotIn("https://x.example/c.png", str(game))

    def test_records_provider_for_bulk_media(self):
        game = {}
        apply_candidate(game, {"source": "igdb", "fields": {}})
        self.assertEqual(game["metadata_provider"], "igdb")
        apply_candidate(game, {"source": "bogus", "fields": {}})
        self.assertEqual(game["metadata_provider"], "igdb")

    def test_unknown_fields_ignored(self):
        game = {"path": "/roms/a.zip", "name": ""}
        apply_candidate(game, {"fields": {"path": "/evil", "launch": "evil", "name": "New"}})
        self.assertEqual(game["path"], "/roms/a.zip")
        self.assertNotIn("launch", game)
        self.assertEqual(game["name"], "New")
        self.assertTrue(set(APPLY_FIELDS) >= {"name", "description"})

    def test_provider_kind_hint(self):
        self.assertEqual(provider_kind_hint("launchbox", "poster"), ("Box - Front",))
        self.assertEqual(provider_kind_hint("steamgriddb", "square"), ("512x512",))
        self.assertEqual(provider_kind_hint("igdb", "poster"), ())


class UrlTests(unittest.TestCase):
    def test_rejects_insecure_and_private_urls(self):
        for url in (
            "http://example.com/a.png",
            "https://user:pass@example.com/a.png",
            "https://localhost/a.png",
            "https://127.0.0.1/a.png",
            "https://10.1.2.3/a.png",
            "https://169.254.1.1/a.png",
            "https://[::1]/a.png",
            "",
        ):
            self.assertEqual(clean_media_url(url, resolver=False), "", url)

    def test_accepts_public_https(self):
        self.assertEqual(clean_media_url("https://example.com/a.png", resolver=False), "https://example.com/a.png")

    def test_resolver_rejects_private_answers(self):
        def private(host, _port):
            return [(2, 1, 6, "", ("10.0.0.9", 0))]

        def public(host, _port):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        self.assertEqual(clean_media_url("https://example.com/a.png", resolver=private), "")
        self.assertEqual(clean_media_url("https://example.com/a.png", resolver=public), "https://example.com/a.png")

    def test_download_candidate_media_uses_download_file(self):
        captured = {}

        def fake_download(url, destination, **kwargs):
            captured.update({"url": url, "destination": Path(destination), "kwargs": kwargs})
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_bytes(b"data")
            return destination

        with tempfile.TemporaryDirectory() as folder, \
             mock.patch("parity_metadata_providers.download_file", side_effect=fake_download):
            result = download_candidate_media(
                "https://example.com/art.png", "poster", folder, filename="../../escape.png", resolver=False,
            )
            self.assertEqual(Path(result), Path(folder) / "escape.png")
            self.assertEqual(captured["kwargs"]["expected_types"], ("image/",))
            self.assertEqual(captured["kwargs"]["max_bytes"], 64 * 1024 * 1024)

    def test_download_rejects_http(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                download_candidate_media("http://example.com/art.png", "cover", folder, resolver=False)


class SearchAllTests(unittest.TestCase):
    def test_ranked_search_uses_provider_order_and_skips_failures(self):
        class FakeProvider:
            id = "launchbox"

            def search(self, game, limit, state=None):
                return [
                    {"id": 2, "name": "Zelda II", "platform": "NES", "fields": {"name": "Zelda II"}},
                    {"id": 1, "name": "The Legend of Zelda", "platform": "NES",
                     "fields": {"name": "The Legend of Zelda"}},
                ]

        class BrokenProvider:
            id = "igdb"

            def search(self, game, limit, state=None):
                raise ValueError("offline")

        with mock.patch.dict(BUILTIN_PROVIDERS, {"launchbox": FakeProvider(), "igdb": BrokenProvider()}):
            results = search_all(
                {"settings": {"metadata_provider_priority": ["launchbox", "igdb"]}},
                {"name": "Legend of Zelda, The", "platform": "NES"},
                providers=["launchbox", "igdb"],
            )
        self.assertEqual([item["id"] for item in results], ["1", "2"])

    def test_unknown_provider_is_ignored(self):
        self.assertEqual(search_all({"settings": {}}, {"name": "x"}, providers=["nope"]), [])


if __name__ == "__main__":
    unittest.main()
