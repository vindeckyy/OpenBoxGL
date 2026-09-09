"""Faugus Launcher import handlers."""

from api_errors import BadRequest
from openbox import load_state
from routes.registry import route
from webapp_state import clear_file_probe_cache, merge_imported_games
try:
    from pkg.parity.parity_faugus import find_faugus_data_dirs, scan_faugus_games
except ImportError:
    from parity_faugus import find_faugus_data_dirs, scan_faugus_games

class FaugusHandlers:
    @route("GET", "/api/faugus/status")
    def _api_get_api_faugus_status(self, parsed):
        dirs = find_faugus_data_dirs()
        self.send_json(200, {"installed": bool(dirs), "data_dirs": dirs})
        return

    @route("GET", "/api/faugus/scan")
    def _api_get_api_faugus_scan(self, parsed):
        try:
            games = scan_faugus_games()
        except Exception as e:
            raise BadRequest(str(e)) from None
        self.send_json(200, {"games": games, "count": len(games)})
        return

    @route("POST", "/api/faugus/import")
    def _api_post_api_faugus_import(self, payload):
        try:
            candidates = scan_faugus_games()
        except Exception as e:
            raise BadRequest(str(e)) from None
        games = []
        for cand in candidates:
            game = {
                "game_id": cand.get("source_identity", cand.get("faugus_id", "")),
                "name": cand.get("name", ""),
                "path": cand.get("path", ""),
                "source": "Faugus",
                "faugus_id": cand.get("faugus_id", ""),
                "launch": "umu-run {path}" if cand.get("path") else "",
                "wine_prefix": cand.get("prefix", ""),
                "source_identity": cand.get("source_identity", ""),
            }
            games.append(game)
        state_before = load_state()
        before_count = len(state_before.get("games", [])) if isinstance(state_before, dict) else 0
        added, found = merge_imported_games(
            games,
            lambda g: ("faugus", str(g.get("faugus_id") or g.get("source_identity") or g.get("path", ""))),
        )
        clear_file_probe_cache()
        state_after = load_state()
        after_games = state_after.get("games", []) if isinstance(state_after, dict) else []
        added_games = after_games[before_count:before_count + added]
        if added_games or not added:
            added_names = [g.get("name", "") for g in added_games if isinstance(g, dict)]
        else:
            added_names = [g.get("name", "") for g in games[:added] if isinstance(g, dict)]
        self.send_json(200, {"added": added, "found": found, "imported": added_names, "count": added})
