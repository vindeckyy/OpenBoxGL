"""Route registry invariants: every registered route has a visibility that matches its path."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import registry  # noqa: E402


class RouteRegistryInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry._ensure_handlers_loaded()
        cls.routes = registry.all_routes()

    def test_every_public_route_lives_under_the_v2_prefix(self):
        for route in self.routes:
            if route.visibility == "public":
                with self.subTest(method=route.method, path=route.path):
                    self.assertTrue(route.path.startswith("/api/v2/"))

    def test_every_host_route_lives_under_the_host_channel(self):
        for route in self.routes:
            if route.visibility == "host":
                with self.subTest(method=route.method, path=route.path):
                    self.assertTrue(route.path.startswith("/api/host/"))

    def test_no_v1_alias_is_public(self):
        for route in self.routes:
            if route.v1:
                with self.subTest(method=route.method, path=route.path):
                    self.assertNotEqual(route.visibility, "public")

    def test_duplicate_method_and_path_is_rejected(self):
        probe = "/api/v2/registry-duplicate-probe"
        registry.register("GET", probe, spec="probe.spec")
        try:
            with self.assertRaises(ValueError):
                registry.register("GET", probe, spec="probe.spec")
        finally:
            registry._REGISTRY.pop(("GET", probe), None)


if __name__ == "__main__":
    unittest.main()
