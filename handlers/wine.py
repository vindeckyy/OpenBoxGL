"""Wine prefix and Proton discovery HTTP handlers."""

from urllib.parse import parse_qs

from api_errors import GameNotFound
from routes.registry import route
from webapp_state import game_from_query, load_state_view
try:
    from pkg.parity.parity_wine import list_proton_versions, list_wine_prefixes, get_prefix_for_game
    HAS_WINE = True
except ImportError:
    HAS_WINE = False


class WineHandlers:
    @route("GET", "/api/wine/prefixes")
    def _api_get_api_wine_prefixes(self, parsed):
        if not HAS_WINE:
            self.send_json(200, {"prefixes": [], "available": False})
            return
        prefixes = list_wine_prefixes()
        self.send_json(200, {"prefixes": prefixes, "available": True})
        return

    @route("GET", "/api/wine/protons")
    def _api_get_api_wine_protons(self, parsed):
        if not HAS_WINE:
            self.send_json(200, {"protons": [], "available": False})
            return
        protons = list_proton_versions()
        self.send_json(200, {"protons": protons, "available": True})
        return

    @route("GET", "/api/wine/prefix-for-game")
    def _api_get_api_wine_prefix_for_game(self, parsed):
        query = parse_qs(parsed.query)
        state = load_state_view()
        try:
            game = game_from_query(state, query)
        except Exception:
            raise GameNotFound("Game not found") from None
        if not HAS_WINE:
            self.send_json(200, {"prefix": "", "available": False})
            return
        prefix = get_prefix_for_game(game)
        self.send_json(200, {"prefix": prefix, "available": bool(prefix)})
        return
