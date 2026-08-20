"""Tests for the route registry, decorator, and table synchronization."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routes import (  # noqa: E402
    GET_TABLE,
    POST_TABLE,
    V1_ALIASED_PREFIXES,
    _is_public_path,
    _is_static_asset,
    _resolve,
)  # noqa: E402
from routes.registry import (  # noqa: E402
    Route,  # noqa: F401
    _REGISTRY,
    all_routes,
    get_routes_for_method,
    route,
)  # noqa: E402


class RouteRegistryTests(unittest.TestCase):
    def test_route_table_sizes(self):
        # 85 base GET + 24 v1 aliases = 109 total
        # 97 base POST + 29 v1 aliases = 126 total
        self.assertEqual(len(GET_TABLE), 109)
        self.assertEqual(len(POST_TABLE), 126)
        self.assertEqual(len(V1_ALIASED_PREFIXES), 46)

    def test_base_routes_count(self):
        base_get = [p for p in GET_TABLE if not p.startswith("/api/v1")]
        base_post = [p for p in POST_TABLE if not p.startswith("/api/v1")]
        self.assertEqual(len(base_get), 85)
        self.assertEqual(len(base_post), 97)
        self.assertEqual(len(base_get) + len(base_post), 182)

    def test_all_routes_registered(self):
        routes = all_routes()
        self.assertEqual(len(routes), 182)
        
        get_routes = [r for r in routes if r.method == "GET"]
        post_routes = [r for r in routes if r.method == "POST"]
        self.assertEqual(len(get_routes), 85)
        self.assertEqual(len(post_routes), 97)

    def test_every_registered_route_matches_live_table(self):
        routes = all_routes()
        for r in routes:
            table = GET_TABLE if r.method == "GET" else POST_TABLE
            self.assertIn(r.path, table, f"Registered route {r.method} {r.path} missing from table")
            self.assertEqual(table[r.path], r.spec, f"Spec mismatch for {r.method} {r.path}")

    def test_route_decorator_custom_function(self):
        test_path = "/api/test/custom-endpoint"
        
        @route("GET", test_path)
        def custom_handler(handler, parsed):
            return "ok"

        self.assertIn(("GET", test_path), _REGISTRY)
        registered_entry = _REGISTRY[("GET", test_path)]
        self.assertEqual(registered_entry.path, test_path)
        self.assertEqual(registered_entry.method, "GET")
        self.assertEqual(registered_entry.spec, "custom_handler")
        # Cleanup: remove test route to avoid polluting global registry for other tests
        del _REGISTRY[("GET", test_path)]
    def test_route_dataclass_access(self):
        r = Route(method="GET", path="/api/example", spec="example_handler", public=True, v1=False)
        self.assertEqual(r.method, "GET")
        self.assertEqual(r["path"], "/api/example")
        self.assertEqual(r["spec"], "example_handler")
        self.assertTrue(r.public)
        self.assertFalse(r.v1)
        self.assertIn("spec", r)
        d = r.to_dict()
        self.assertEqual(d["path"], "/api/example")

    def test_get_routes_for_method(self):
        get_map = get_routes_for_method("GET")
        self.assertIn("/api/library", get_map)
        self.assertIn("/api/settings", get_map)
        
        post_map = get_routes_for_method("POST")
        self.assertIn("/api/launch", post_map)
        self.assertIn("/api/game", post_map)

    def test_dotted_route_specs_resolve(self):
        self.assertTrue(callable(_resolve("handlers.native.capabilities")))
        self.assertTrue(callable(_resolve("handlers.native.dialog")))
        self.assertIsNone(_resolve("bare_method_name"))

    def test_public_and_static_helpers(self):
        self.assertTrue(_is_public_path("/"))
        self.assertTrue(_is_public_path("/index.html"))
        self.assertTrue(_is_public_path("/static/app.js"))
        self.assertTrue(_is_static_asset("/static/app.js"))
        self.assertFalse(_is_static_asset("/api/library"))
        self.assertFalse(_is_public_path("/api/library"))


if __name__ == "__main__":
    unittest.main()
