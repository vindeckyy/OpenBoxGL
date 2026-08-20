"""Versioned API alias tests."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contracts import V1_SCHEMA  # noqa: E402
from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES, _resolve  # noqa: E402
import web_app  # noqa: E402


def expected_v1_routes():
    return {f"/api/v1{path[len('/api'):]}" for path in V1_ALIASED_PREFIXES}


class V1AliasTests(unittest.TestCase):
    def test_every_aliased_prefix_resolves(self):
        for path in V1_ALIASED_PREFIXES:
            table = POST_TABLE if path in POST_TABLE else GET_TABLE
            self.assertIn(path, table, f"source route missing: {path}")
            v1_path = f"/api/v1{path[len('/api'):]}"
            self.assertIn(v1_path, table, f"v1 alias missing: {v1_path}")
            self.assertEqual(table[v1_path], table[path], f"v1 alias diverges: {path}")

    def test_v1_alias_maps_to_a_handler_method(self):
        for path in V1_ALIASED_PREFIXES:
            table = POST_TABLE if path in POST_TABLE else GET_TABLE
            method = table[f"/api/v1{path[len('/api'):]}"]
            if "." in method:
                callable_ = _resolve(method)
                self.assertTrue(callable(callable_), f"dotted handler not callable: {method}")
            else:
                self.assertTrue(hasattr(web_app.Handler, method), f"handler missing: {method}")

    def test_dotted_route_specs_resolve(self):
        all_specs = set(GET_TABLE.values()) | set(POST_TABLE.values())
        dotted_specs = [spec for spec in all_specs if "." in spec]
        self.assertTrue(dotted_specs, "expected at least one dotted spec")
        for spec in dotted_specs:
            callable_ = _resolve(spec)
            self.assertTrue(callable(callable_), f"dotted route spec failed to resolve: {spec}")

    def test_unknown_v1_route_stays_unmapped(self):
        self.assertNotIn("/api/v1/not-a-real-route", GET_TABLE)
        self.assertNotIn("/api/v1/not-a-real-route", POST_TABLE)

    def test_route_tables_match_handler_methods(self):
        for path, spec in GET_TABLE.items():
            if "." not in spec:
                self.assertTrue(spec.startswith("_api_get_"), f"GET route has non-GET handler: {path}")
        for path, spec in POST_TABLE.items():
            if "." not in spec:
                self.assertTrue(spec.startswith("_api_post_"), f"POST route has non-POST handler: {path}")

    def test_frontend_api_v1_constants_match_frozen_aliases(self):
        util_content = (ROOT / "static" / "util.js").read_text(encoding="utf-8")
        found_literals = set(re.findall(r"['\"](/api/v1/[^'\"]+)['\"]", util_content))
        expected = expected_v1_routes()
        self.assertEqual(found_literals, expected)

    def test_contract_schema_paths_are_frozen_routes(self):
        expected = expected_v1_routes()
        for schema_path in V1_SCHEMA:
            self.assertIn(schema_path, expected, f"V1_SCHEMA path not in frozen routes: {schema_path}")

    def test_session_cleanup_route_is_registered(self):
        self.assertEqual(POST_TABLE["/api/session/cleanup"], "_api_post_api_session_cleanup")

    def test_api_docs_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "api-v1.md"
            res = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts" / "gen_api_docs.py"), "--out", str(out_file)],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            content = out_file.read_text(encoding="utf-8")
            self.assertIn("Generated from `routes.py` and `contracts.py`; do not edit by hand.", content)
            self.assertIn("| GET / POST | `/api/v1/settings` |", content)
            self.assertIn("`/api/v1/metadata/match`", content)


if __name__ == "__main__":
    unittest.main()
