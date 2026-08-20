"""Unit tests for performance benchmark harness."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.perf_bench as pb  # noqa: E402


class FakeProcess:
    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass

    def kill(self):
        pass


class PerfBenchTests(unittest.TestCase):
    def test_benchmark_routes_and_structure(self):
        requested_paths = []

        def fake_request(origin, token, path, method="GET", body=None, timeout=120, gzip=False):
            requested_paths.append((method, path))
            return 0.005, b'{"games":[],"facets":[],"ok":true,"updated":1,"added":0,"found":0}'

        fake_server = (FakeProcess(), "http://127.0.0.1:9999", "test-token")
        with mock.patch.object(pb, "_start_server", return_value=fake_server), \
             mock.patch.object(pb, "_request", side_effect=fake_request):
            results = pb.benchmark("/tmp/fake-data-dir", runs=1)

        self.assertIn("library", results)
        self.assertIn("library_gzip", results)
        self.assertIn("filtered_query", results)
        self.assertIn("facet", results)
        self.assertIn("single_mutation", results)
        self.assertIn("bulk_mutation", results)
        self.assertIn("import_empty_folder", results)
        self.assertIn("media_index", results)

        paths = [p for _, p in requested_paths]
        self.assertIn("/api/library", paths)
        self.assertIn("/api/library?offset=0&limit=500", paths)
        self.assertIn("/api/explorer/facets?field=genre", paths)
        self.assertIn("/api/games/bulk", paths)
        self.assertIn("/api/import", paths)
        self.assertIn("/api/favorite", paths)
        self.assertIn("/api/media?id=0&kind=cover", paths)

        # Legacy / placeholder routes must not be queried
        self.assertNotIn("/api/facets", paths)
        self.assertNotIn("/api/library/facets", paths)
        self.assertNotIn("/api/bulk", paths)
        self.assertNotIn("/api/library/bulk", paths)
        self.assertNotIn("/api/import/preview", paths)
        self.assertNotIn("/api/import/apply", paths)

    def test_safe_request_failure_returns_error_object(self):
        with mock.patch.object(pb, "_request", side_effect=RuntimeError("connection refused")):
            stats, last_bytes, payload = pb._safe_request("http://127.0.0.1:9999", "tok", "/api/test", runs=2)
        self.assertEqual(stats["runs"], 0)
        self.assertIn("connection refused", stats["error"])
        self.assertIsNone(last_bytes)
        self.assertIsNone(payload)

    def test_check_gates(self):
        passing_results = {
            "10000": {
                "library": {"p95_ms": 150.0, "runs": 3},
                "library_gzip": {"p95_ms": 80.0, "runs": 3},
                "favorite_mutation": {"p95_ms": 300.0, "runs": 3},
                "single_mutation": {"p95_ms": 300.0, "runs": 3},
                "filtered_query": {"p95_ms": 50.0, "runs": 3},
                "facet": {"p95_ms": 40.0, "runs": 3},
            }
        }
        self.assertEqual(pb._check_gates(passing_results), [])

        failing_results = {
            "10000": {
                "library": {"p95_ms": 2500.0, "runs": 3},  # exceeds 2000.0 gate
                "library_gzip": {"p95_ms": 80.0, "runs": 3},
                "favorite_mutation": {"p95_ms": 300.0, "runs": 3},
                "single_mutation": {"p95_ms": 300.0, "runs": 3},
                "filtered_query": {"p95_ms": 50.0, "runs": 3},
                "facet": {"p95_ms": 40.0, "runs": 3},
            }
        }
        failures = pb._check_gates(failing_results)
        self.assertEqual(len(failures), 1)
        self.assertIn("library.p95_ms", failures[0])

    def test_check_gates_rejects_missing_or_failed_measurements(self):
        missing_results = {"10000": {"library": {"p95_ms": 150.0, "runs": 3}}}
        missing_failures = pb._check_gates(missing_results)
        self.assertTrue(any("missing 10,000-game benchmark result: library_gzip" in item for item in missing_failures))

        failed_results = {
            "10000": {
                "library": {"p95_ms": 150.0, "runs": 3},
                "library_gzip": {"runs": 0, "error": "connection refused"},
                "favorite_mutation": {"p95_ms": 300.0, "runs": 3},
                "filtered_query": {"p95_ms": 50.0, "runs": 3},
                "facet": {"p95_ms": 40.0, "runs": 3},
            }
        }
        failed_failures = pb._check_gates(failed_results)
        self.assertTrue(any("library_gzip missing p95_ms" in item for item in failed_failures))


if __name__ == "__main__":
    unittest.main()
