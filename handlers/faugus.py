"""Faugus Launcher import handlers."""

from api_errors import ApiError
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
            self.send_json(200, {"games": [], "error": str(e)})
            return
        self.send_json(200, {"games": games, "count": len(games)})
        return

    @route("POST", "/api/faugus/import")
    def _api_post_api_faugus_import(self, payload):
        try:
            candidates = scan_faugus_games()
        except Exception as e:
            raise ApiError(str(e)) from None
        games = []
        for cand in candidates:
            game = {
                "game_id": cand.get("source_identity", cand.get("faugus_id", "")),
                "name": cand.get("name", ""),
                "path": cand.get("path", ""),
                "source": "Faugus",
                "faugus_id": cand.get("faugus_id", ""),
                "launch": f"umu-run {cand.get('path','')}" if cand.get("path") else "",
                "wine_prefix": cand.get("prefix", ""),
                "source_identity": cand.get("source_identity", ""),
            }
            games.append(game)
        added, found = merge_imported_games(
            games,
            lambda g: ("faugus", str(g.get("faugus_id") or g.get("source_identity") or g.get("path", ""))),
        )
        clear_file_probe_cache()
        added_names = [g["name"] for g in games[:added]]
        self.send_json(200, {"added": added, "found": found, "imported": added_names, "count": added})
