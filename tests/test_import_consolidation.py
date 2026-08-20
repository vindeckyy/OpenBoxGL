import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pkg" / "parity"))


def _reset_modules():
    for name in ("openbox", "webapp_state", "web_app"):
        sys.modules.pop(name, None)


def main():
    with tempfile.TemporaryDirectory() as directory:
        previous = os.environ.get("OPENBOX_DATA_DIR")
        os.environ["OPENBOX_DATA_DIR"] = directory
        try:
            _reset_modules()
            from openbox import load_state, save_state
            from webapp_state import merge_imported_games

            save_state({"games": [], "profiles": {}, "history": [], "settings": {}, "playlists": []})
            steam = {
                "name": "Control",
                "platform": "PC",
                "source": "Steam",
                "path": "/usr/bin/steam",
                "launch": "steam -applaunch {app_id}",
                "steam_app_id": "870780",
            }
            heroic = {
                "name": "Control",
                "platform": "PC",
                "source": "GOG",
                "path": "/usr/bin/xdg-open",
                "launch": "xdg-open heroic://launch/gog/{heroic_app_id}",
                "heroic_app_id": "1207659022",
            }
            added, found = merge_imported_games([steam], lambda game: ("steam", str(game.get("steam_app_id", ""))))
            assert (added, found) == (1, 1)
            added, found = merge_imported_games(
                [heroic],
                lambda game: ("heroic", str(game.get("source", "")), str(game.get("heroic_app_id", ""))),
            )
            state = load_state()
            assert (added, found) == (0, 1)
            assert len(state["games"]) == 1
            game = state["games"][0]
            assert game["steam_app_id"] == "870780"
            assert game["heroic_app_id"] == "1207659022"
            assert "heroic:GOG:1207659022" in game["source_identities"]
            assert game["applications"] == [{
                "name": "Launch with Heroic (GOG)",
                "path": "/usr/bin/xdg-open",
                "command": "xdg-open heroic://launch/gog/1207659022",
            }]

            added, found = merge_imported_games([steam], lambda game: ("steam", str(game.get("steam_app_id", ""))))
            assert (added, found) == (0, 1)
            assert len(load_state()["games"][0]["applications"]) == 1

            same_store_title = dict(steam, steam_app_id="999999")
            added, found = merge_imported_games(
                [same_store_title],
                lambda game: ("steam", str(game.get("steam_app_id", ""))),
            )
            assert (added, found) == (1, 1)
            assert len(load_state()["games"]) == 2

            save_state({
                "games": [dict(steam), dict(heroic)],
                "profiles": {},
                "history": [],
                "settings": {},
                "playlists": [],
            })
            from web_app import Handler

            handler = object.__new__(Handler)
            handler.send_json = mock.Mock()
            Handler.dedupe(handler)
            payload = handler.send_json.call_args[0][1]
            assert payload == {"removed": ["Control"]}
            state = load_state()
            assert len(state["games"]) == 1
            assert state["games"][0]["applications"][0]["command"] == "xdg-open heroic://launch/gog/1207659022"

            # Probe cache clearing assertions across import handlers
            cleared = []
            with mock.patch("handlers.imports.clear_file_probe_cache", side_effect=lambda: cleared.append(True)), \
                 mock.patch("handlers.imports.storefront_catalog", return_value=[]), \
                 mock.patch("handlers.imports.import_scummvm", return_value=[]), \
                 mock.patch("handlers.imports.import_rpcs3_hdd", return_value=[]), \
                 mock.patch("handlers.imports.import_vita3k", return_value=[]):
                handler.import_storefront_catalog({"source": "steam"})
                handler.import_scummvm_games()
                handler.import_rpcs3_games()
                handler.import_vita3k_games()
            assert len(cleared) == 4, f"expected 4 probe cache clear calls, got {len(cleared)}"
        finally:
            if previous is None:
                os.environ.pop("OPENBOX_DATA_DIR", None)
            else:
                os.environ["OPENBOX_DATA_DIR"] = previous
            _reset_modules()
    print("import consolidation self-test: ok")


if __name__ == "__main__":
    main()
