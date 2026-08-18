"""Faugus Launcher import handlers."""

from parity_faugus import find_faugus_data_dirs, scan_faugus_games

try:
    from pkg.parity.parity_faugus import find_faugus_data_dirs as _find, scan_faugus_games as _scan
    HAS_FAUGUS = True
except ImportError:
    HAS_FAUGUS = False


class FaugusHandlers:
    def _api_get_api_faugus_status(self, parsed):
        dirs = find_faugus_data_dirs() if HAS_FAUGUS else _find() if False else []
        # Actually use the shim if pkg import failed
        if not HAS_FAUGUS:
            try:
                from parity_faugus import find_faugus_data_dirs as f
                dirs = f()
            except Exception:
                dirs = []
        else:
            dirs = _find()
        self.send_json(200, {"installed": bool(dirs), "data_dirs": dirs})
        return

    def _api_get_api_faugus_scan(self, parsed):
        try:
            games = scan_faugus_games() if HAS_FAUGUS else _scan() if False else []
            if not HAS_FAUGUS:
                from parity_faugus import scan_faugus_games as s
                games = s()
            else:
                games = _scan()
        except Exception as e:
            self.send_json(200, {"games": [], "error": str(e)})
            return
        self.send_json(200, {"games": games, "count": len(games)})
        return

    def _api_post_api_faugus_import(self, payload):
        try:
            games = scan_faugus_games() if HAS_FAUGUS else []
            if not HAS_FAUGUS:
                from parity_faugus import scan_faugus_games as s
                games = s()
            else:
                games = _scan()
        except Exception as e:
            self.send_json(500, {"error": str(e)})
            return
        # Import via transact_state, reusing identity dedupe
        from webapp_state import transact_state
        from pkg.parity.parity_identity import normalize_identity
        added = []
        def mutate(state):
            existing_ids = {normalize_identity(g) for g in state["games"] if normalize_identity(g)}
            for cand in games:
                ident = cand.get("source_identity", "")
                if ident and ident in existing_ids:
                    continue
                game = {
                    "game_id": cand.get("source_identity", cand.get("faugus_id", "")),
                    "name": cand.get("name", ""),
                    "path": cand.get("path", ""),
                    "source": "Faugus",
                    "faugus_id": cand.get("faugus_id", ""),
                    "launch": f"umu-run {cand.get('path','')}" if cand.get("path") else "",
                    "wine_prefix": cand.get("prefix", ""),
                }
                # Keep identity for future dedupe
                game["source_identity"] = ident
                state["games"].append(game)
                added.append(game["name"])
                existing_ids.add(ident)
            return added
        transact_state(mutate)
        self.send_json(200, {"imported": added, "count": len(added)})
