"""API contract: /api/v2 is public, /api/host is the native channel, retired surfaces are gone."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes  # noqa: E402
from api_errors import ApiVersionRequired, HostApiUnsupported, HostChannelRetired, V1ApiRemoved, problem_payload  # noqa: E402
from routes import registry  # noqa: E402


def _probe(env_overrides):
    """Import routes in a fresh interpreter so the environment is read once."""
    code = (
        "import json, routes\n"
        "print(json.dumps({'get': sorted(routes.GET_TABLE), 'post': sorted(routes.POST_TABLE)}))\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "OPENBOX_ENABLE_V1_API"}
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


class V2AliasTests(unittest.TestCase):
    def test_every_unversioned_base_path_has_a_v2_twin(self):
        for table, name in ((routes.GET_TABLE, "GET"), (routes.POST_TABLE, "POST")):
            for path in table:
                if not path.startswith("/api/") or path.startswith(("/api/v1/", "/api/v2/", "/api/host/")):
                    continue
                twin = "/api/v2" + path[len("/api"):]
                with self.subTest(method=name, path=path):
                    self.assertIn(twin, table)

    def test_native_host_legacy_names_map_to_the_host_channel(self):
        self.assertEqual(routes.RETIRED_HOST_PATHS, {
            "/api/launch": "/api/host/launch",
            "/api/shutdown": "/api/host/shutdown",
            "/api/native/capabilities": "/api/host/capabilities",
        })
        self.assertEqual(routes.POST_TABLE["/api/host/launch"], routes.POST_TABLE["/api/launch"])
        self.assertEqual(routes.POST_TABLE["/api/host/shutdown"], routes.POST_TABLE["/api/shutdown"])
        self.assertEqual(routes.GET_TABLE["/api/host/capabilities"], routes.GET_TABLE["/api/native/capabilities"])

    def test_decorator_routes_resolve_under_v2_and_host_names(self):
        registry._ensure_handlers_loaded()
        self.assertIsNotNone(routes._lookup_registry("POST", "/api/v2/resume"))
        self.assertIsNotNone(routes._lookup_registry("POST", "/api/host/resume"))
        self.assertEqual(routes._lookup_registry("POST", "/api/host/resume"), routes._lookup_registry("POST", "/api/v2/resume"))

    def test_all_four_host_operations_are_registered(self):
        from routes.registry import all_routes
        host = {(route.method, route.path) for route in all_routes() if route.visibility == "host"}
        host.update(("GET", path) for path in routes.GET_TABLE if path.startswith("/api/host/"))
        host.update(("POST", path) for path in routes.POST_TABLE if path.startswith("/api/host/"))
        self.assertEqual(host, {
            ("GET", "/api/host/capabilities"),
            ("POST", "/api/host/launch"),
            ("POST", "/api/host/resume"),
            ("POST", "/api/host/shutdown"),
        })


class RetiredSurfaceTests(unittest.TestCase):
    def test_v1_surface_is_gone_even_with_the_old_environment_variable(self):
        for overrides in ({}, {"OPENBOX_ENABLE_V1_API": "1"}):
            tables = _probe(overrides)
            self.assertFalse([path for path in tables["get"] + tables["post"] if path.startswith("/api/v1/")])

    def test_v1_request_is_gone_with_a_stable_code(self):
        with self.assertRaises(V1ApiRemoved) as raised:
            routes._guard_retired(None, "GET", "/api/v1/library")
        self.assertEqual(raised.exception.status, 410)
        self.assertEqual(raised.exception.code, "V1_API_REMOVED")
        self.assertEqual(raised.exception.detail["use"], "/api/v2/library")

    def test_unversioned_public_request_points_at_v2(self):
        with self.assertRaises(ApiVersionRequired) as raised:
            routes._guard_retired(None, "GET", "/api/library")
        self.assertEqual(raised.exception.status, 410)
        self.assertEqual(raised.exception.code, "API_VERSION_REQUIRED")
        self.assertEqual(raised.exception.detail["use"], "/api/v2/library")

    def test_legacy_host_request_points_at_the_host_channel(self):
        with self.assertRaises(HostChannelRetired) as raised:
            routes._guard_retired(None, "POST", "/api/launch")
        self.assertEqual(raised.exception.status, 410)
        self.assertEqual(raised.exception.code, "HOST_CHANNEL_RETIRED")
        self.assertEqual(raised.exception.detail["use"], "/api/host/launch")


class VisibilityTests(unittest.TestCase):
    def test_public_routes_must_live_under_v2(self):
        with self.assertRaises(ValueError):
            registry.register("GET", "/api/widgets", spec="x", visibility="public")

    def test_host_routes_must_live_under_the_host_channel(self):
        with self.assertRaises(ValueError):
            registry.register("GET", "/api/v2/widgets", spec="x", visibility="host")

    def test_default_visibility_follows_the_path_prefix(self):
        self.assertEqual(registry.default_visibility("/api/v2/library"), "public")
        self.assertEqual(registry.default_visibility("/api/host/launch"), "host")
        self.assertEqual(registry.default_visibility("/api/launch"), "internal")
        self.assertEqual(registry.default_visibility("/"), "internal")


class HostVersionTests(unittest.TestCase):
    class _Handler:
        def __init__(self, version):
            self.headers = {"X-OpenBox-API-Version": version} if version is not None else {}

    def test_host_with_another_major_is_refused(self):
        with self.assertRaises(HostApiUnsupported) as raised:
            routes._guard_host(self._Handler("3"), "/api/host/launch")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "HOST_API_UNSUPPORTED")

    def test_host_with_major_two_or_no_header_is_accepted(self):
        routes._guard_host(self._Handler("2.1"), "/api/host/launch")
        routes._guard_host(self._Handler(None), "/api/host/launch")
        routes._guard_host(self._Handler("3"), "/api/v2/launch")


class ProblemDetailsTests(unittest.TestCase):
    def test_error_body_is_a_problem_document_that_keeps_the_legacy_error_field(self):
        body = problem_payload(409, {"error": "Library is busy.", "code": "STATE_BUSY"}, request_id="req-1", instance="/api/v2/library")
        self.assertEqual(body["type"], "https://openbox.local/problems/state-busy")
        self.assertEqual((body["title"], body["status"], body["code"]), ("Conflict", 409, "STATE_BUSY"))
        self.assertEqual(body["detail"], body["error"])
        self.assertEqual((body["request_id"], body["instance"]), ("req-1", "/api/v2/library"))
        self.assertFalse(body["retryable"])

    def test_service_errors_are_marked_retryable(self):
        body = problem_payload(503, {"error": "Busy.", "code": "STATE_UNAVAILABLE"})
        self.assertTrue(body["retryable"])


class VersionEndpointTests(unittest.TestCase):
    def test_version_endpoint_advertises_every_contract_version(self):
        from handlers import native

        sent = {}

        class _Handler:
            def send_json(self, status, payload, extra_headers=None):
                sent["status"], sent["payload"] = status, payload

        native.version_info(_Handler(), None)
        self.assertEqual(sent["status"], 200)
        payload = sent["payload"]
        self.assertEqual(payload["api_version"], 2)
        self.assertEqual(payload["state_schema"], 7)
        for key in ("version", "plugin_api_version", "backup_format", "sync_format", "platform", "python"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
