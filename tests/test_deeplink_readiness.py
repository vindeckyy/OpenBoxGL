"""Deep-link readiness uses the public version endpoint, not a route that does not exist."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkg.parity import parity_deeplinks  # noqa: E402


class DeeplinkReadinessTests(unittest.TestCase):
    def test_readiness_probes_the_public_version_route(self):
        calls = []

        def fake_request(host, port, token, path, method="GET", body=None):
            calls.append((host, port, token, path, method))
            return {"version": "2.1.0"}

        with mock.patch.object(parity_deeplinks, "api_request", side_effect=fake_request):
            self.assertTrue(parity_deeplinks._server_responds("127.0.0.1", 8787, "tok"))
        self.assertEqual(calls, [("127.0.0.1", 8787, "tok", "/api/v2/version", "GET")])

    def test_launch_uses_the_public_v2_route(self):
        calls = []

        def fake_request(host, port, token, path, method="GET", body=None):
            calls.append((path, method, body))
            return {"ok": True}

        with mock.patch.object(parity_deeplinks, "api_request", side_effect=fake_request):
            parity_deeplinks.dispatch_uri("openbox://launch/42", "/tmp/unused", port=8787, token="tok")
        self.assertEqual(calls, [("/api/v2/launch", "POST", {"id": 42})])


if __name__ == "__main__":
    unittest.main()
