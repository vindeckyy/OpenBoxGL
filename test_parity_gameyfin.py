#!/usr/bin/env python3
"""Gameyfin and save-tool integration tests."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from parity_gameyfin import (
    DEFAULT_PROVIDER,
    GameyfinClient,
    GameyfinError,
    catalog_gameyfin,
    game_from_gameyfin,
    install_gameyfin_game,
    uninstall_gameyfin_game,
)
from parity_save_tools import run_ludusavi, save_tool_status
from parity_storefront import catalog_entries_to_games, storefront_catalog


class FakeResponse(io.BytesIO):
    def __init__(self, payload, headers=None):
        if isinstance(payload, str):
            payload = payload.encode()
        elif not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode()
        super().__init__(payload)
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class GameyfinTests(unittest.TestCase):
    def test_raw_request_returns_open_response(self):
        class Opener:
            def open(self, request, timeout=0):
                return FakeResponse(b"{\"ok\":true}")

        client = GameyfinClient("http://gameyfin.local", opener=Opener())
        response = client.request("POST", "/login", data=b"user=x", raw=True)
        self.assertFalse(response.closed, "raw responses must reach the caller open")
        with response:
            pass
        self.assertTrue(response.closed)

    def test_game_from_record_marks_uninstalled(self):
        with tempfile.TemporaryDirectory() as directory:
            game = game_from_gameyfin(
                {"id": 42, "title": "Demo Quest", "platforms": ["PC"], "summary": "A test"},
                directory,
                DEFAULT_PROVIDER,
            )
            self.assertEqual(game["gameyfin_id"], "42")
            self.assertFalse(game["store_installed"])
            self.assertTrue(game["owned"])

    def test_catalog_and_install_uninstall(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {
                "gameyfin_url": "http://gameyfin.local",
                "gameyfin_username": "testuser",
                "gameyfin_password": "secret",
                "gameyfin_install_dir": directory,
                "gameyfin_provider": DEFAULT_PROVIDER,
            }
            records = [{"id": 7, "title": "Nebula Runner", "platforms": ["LINUX"], "summary": "space"}]

            class Client(GameyfinClient):
                def __init__(self):
                    super().__init__(settings["gameyfin_url"], settings["gameyfin_username"], settings["gameyfin_password"])
                    self._logged_in = True

                def list_games(self):
                    return records

                def list_providers(self):
                    return [{"key": DEFAULT_PROVIDER, "name": "Direct Download", "priority": 1}]

                def download_game(self, game_id, provider, destination):
                    destination = Path(destination)
                    destination.mkdir(parents=True, exist_ok=True)
                    target = destination / "NebulaRunner.zip"
                    target.write_bytes(b"fake-rom")
                    return target

            catalog, providers = catalog_gameyfin(settings, client=Client())
            self.assertEqual(len(catalog), 1)
            self.assertEqual(providers[0]["key"], DEFAULT_PROVIDER)
            games = catalog_entries_to_games(catalog)
            self.assertEqual(games[0]["source"], "Gameyfin")
            installed = install_gameyfin_game(settings, 7, client=Client())
            self.assertTrue(installed["store_installed"])
            self.assertTrue(Path(installed["path"]).exists())
            result = uninstall_gameyfin_game(installed)
            self.assertFalse(installed["store_installed"])
            self.assertTrue(result["removed"])

    def test_list_providers_ignores_non_dicts(self):
        class Client(GameyfinClient):
            def __init__(self):
                super().__init__("http://gameyfin.local")
                self._logged_in = True

            def connect(self, endpoint, method, payload=None):
                if endpoint == "DownloadProviderEndpoint":
                    return ["bad", 12, {"key": "ok-provider", "name": "OK"}]
                return []

        providers = Client().list_providers()
        self.assertEqual(providers, [{"key": "ok-provider", "name": "OK"}])

    def test_install_keeps_existing_files_on_download_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {
                "gameyfin_url": "http://gameyfin.local",
                "gameyfin_install_dir": directory,
                "gameyfin_provider": DEFAULT_PROVIDER,
            }
            records = [{"id": 9, "title": "Keep Saves", "platforms": ["PC"]}]
            install_dir = Path(directory) / "keep-saves"
            install_dir.mkdir()
            save_file = install_dir / "save.dat"
            save_file.write_text("precious")

            class Client(GameyfinClient):
                def __init__(self):
                    super().__init__(settings["gameyfin_url"])
                    self._logged_in = True

                def list_games(self):
                    return records

                def download_game(self, game_id, provider, destination):
                    raise GameyfinError("download failed")

            with self.assertRaises(GameyfinError):
                install_gameyfin_game(settings, 9, client=Client())
            self.assertTrue(save_file.exists())
            self.assertEqual(save_file.read_text(), "precious")

    def test_storefront_rejects_unknown(self):
        with self.assertRaises(ValueError):
            storefront_catalog("nope")


class SaveToolTests(unittest.TestCase):
    def test_status_and_ludusavi_command(self):
        status = save_tool_status(which=lambda name: "/usr/bin/ludusavi" if name == "ludusavi" else None)
        self.assertTrue(status["ludusavi"])
        self.assertFalse(status["hoard"])

        def run(command, capture_output=True, text=True, timeout=600):
            self.assertIn("--api", command)
            self.assertIn("--force", command)
            return mock.Mock(returncode=0, stdout='{"overall":{"processedGames":1}}', stderr="")

        result = run_ludusavi("backup", game_name="Demo", which=lambda name: "/usr/bin/ludusavi", run=run)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
