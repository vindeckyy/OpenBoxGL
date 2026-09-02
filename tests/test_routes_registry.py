"""Tests for the route registry, decorator, and table synchronization."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
    register,
    route,
)  # noqa: E402
from scripts import check_v1_contract  # noqa: E402


class RouteRegistryTests(unittest.TestCase):
    def test_route_table_sizes(self):
        # 110 base GET + 25 v1 aliases = 135 total (1.8.0 adds 2 export + 2 screenscraper GETs)
        # 114 base POST + 42 v1 aliases = 156 total (1.8.0 adds 1 export + 4 screenscraper POSTs)
        self.assertEqual(len(GET_TABLE), 135)
        self.assertEqual(len(POST_TABLE), 156)
        self.assertEqual(len(V1_ALIASED_PREFIXES), 60)

    def test_base_routes_count(self):
        base_get = [p for p in GET_TABLE if not p.startswith("/api/v1")]
        base_post = [p for p in POST_TABLE if not p.startswith("/api/v1")]
        self.assertEqual(len(base_get), 110)
        self.assertEqual(len(base_post), 114)
        self.assertEqual(len(base_get) + len(base_post), 224)

    def test_all_routes_registered(self):
        routes = all_routes()
        self.assertEqual(len(routes), 225)

        get_routes = [r for r in routes if r.method == "GET"]
        post_routes = [r for r in routes if r.method == "POST"]
        self.assertEqual(len(get_routes), 111)
        self.assertEqual(len(post_routes), 114)

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
        self.assertTrue(_is_public_path("/static/setup.js"))
        self.assertTrue(_is_public_path("/static/activity.js"))
        self.assertTrue(_is_static_asset("/static/app.js"))
        self.assertTrue(_is_static_asset("/static/setup.js"))
        self.assertTrue(_is_static_asset("/static/activity.js"))
        self.assertIn("/static/setup.js", GET_TABLE)
        self.assertIn("/static/activity.js", GET_TABLE)
        self.assertFalse(_is_static_asset("/api/library"))
        self.assertFalse(_is_public_path("/api/library"))

    def test_register_requires_spec_or_func(self):
        with self.assertRaises(ValueError) as ctx:
            register("GET", "/api/test/no-spec-or-func")
        self.assertIn("Either spec or func", str(ctx.exception))

    def test_duplicate_register_raises(self):
        test_path = "/api/test/duplicate-endpoint"
        register("GET", test_path, spec="first_handler")
        try:
            with self.assertRaises(ValueError) as ctx:
                register("GET", test_path, spec="second_handler")
            self.assertIn("Duplicate", str(ctx.exception))
        finally:
            del _REGISTRY[("GET", test_path)]

    def test_broken_handler_import_fails_load(self):
        from routes.registry import _ensure_handlers_loaded

        with patch("routes.registry.importlib.import_module", side_effect=ImportError("broken")):
            with self.assertRaises(ImportError):
                _ensure_handlers_loaded()

    def test_missing_base_table_entry_fails_check(self):
        routes = all_routes()
        orphan = Route(
            method="GET",
            path="/api/test/orphan-registry",
            spec="orphan_handler",
        )
        with patch("scripts.check_v1_contract.all_routes", return_value=[*routes, orphan]):
            problems = check_v1_contract.check_registry_consistency()
        self.assertTrue(
            any("MISSING GET /api/test/orphan-registry" in p for p in problems)
        )

    def test_missing_registry_entry_fails_check(self):
        routes = all_routes()
        trimmed = [r for r in routes if r.path != "/api/library"]
        with patch("scripts.check_v1_contract.all_routes", return_value=trimmed):
            problems = check_v1_contract.check_registry_consistency()
        self.assertTrue(any("MISSING GET /api/library in route registry" in p for p in problems))

    def test_reverse_table_check_ignores_v1_aliases(self):
        routes = all_routes()
        alias_only_get = {
            path: spec
            for path, spec in GET_TABLE.items()
            if path.startswith("/api/v1/")
        }
        self.assertTrue(alias_only_get, "expected v1 alias rows in GET_TABLE")
        with patch("scripts.check_v1_contract.all_routes", return_value=routes):
            problems = check_v1_contract.check_registry_consistency()
        for path in alias_only_get:
            self.assertFalse(
                any(path in problem for problem in problems),
                f"v1 alias {path} should not appear in registry consistency errors",
            )


if __name__ == "__main__":
    unittest.main()
