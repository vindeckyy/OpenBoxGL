#!/usr/bin/env python3
"""P1-18: a broken route registry fails loudly instead of silently 404ing."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import routes  # noqa: E402


def _broken_registry():
    broken = mock.Mock()
    broken.get = mock.Mock(side_effect=RuntimeError("registry corrupt"))
    return broken


class RegistryLookupFailureTests(unittest.TestCase):
    def test_lookup_reraises_registry_error(self):
        with mock.patch("routes.registry._REGISTRY", _broken_registry()):
            with self.assertRaises(RuntimeError):
                routes._lookup_registry("GET", "/api/v2/does-not-exist")

    def test_dispatch_get_surfaces_registry_error_instead_of_404(self):
        handler = SimpleNamespace(authorized=lambda: True)
        parsed = SimpleNamespace(path="/api/v2/does-not-exist")
        with mock.patch("routes.registry._REGISTRY", _broken_registry()):
            with self.assertRaises(RuntimeError):
                routes.dispatch_get(handler, parsed)

    def test_dispatch_post_surfaces_registry_error_instead_of_404(self):
        handler = SimpleNamespace(authorized=lambda: True)
        with mock.patch("routes.registry._REGISTRY", _broken_registry()):
            with self.assertRaises(RuntimeError):
                routes.dispatch_post(handler, "/api/v2/does-not-exist", {})

    def test_healthy_registry_still_resolves_decorator_routes(self):
        import handlers.launch  # noqa: F401  (populates the decorator registry)

        spec = routes._lookup_registry("POST", "/api/v2/launch/preflight")
        self.assertEqual(spec, "_api_post_api_v2_launch_preflight")


if __name__ == "__main__":
    unittest.main()
