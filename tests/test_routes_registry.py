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

    def test_every_private_api_base_has_a_public_v2_twin(self):
        by_key = {(route.method, route.path) for route in self.routes}
        for route in self.routes:
            if not route.path.startswith("/api/v2/"):
                continue
            if route.path.startswith(("/api/v1/", "/api/v2/", "/api/host/")):
                continue
            twin = "/api/v2/v2" + route.path[len("/api"):]
            with self.subTest(method=route.method, path=route.path):
                self.assertIn((route.method, twin), by_key)

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
