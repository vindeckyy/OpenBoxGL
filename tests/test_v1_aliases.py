"""Versioned API alias tests."""

import unittest

from routes import GET_TABLE, POST_TABLE, V1_ALIASED_PREFIXES


class V1AliasTests(unittest.TestCase):
    def test_every_aliased_prefix_resolves(self):
        for path in V1_ALIASED_PREFIXES:
            table = POST_TABLE if path in POST_TABLE else GET_TABLE
            self.assertIn(path, table, f"source route missing: {path}")
            v1_path = f"/api/v1{path[len('/api'):]}"
            self.assertIn(v1_path, table, f"v1 alias missing: {v1_path}")
            self.assertEqual(table[v1_path], table[path], f"v1 alias diverges: {path}")

    def test_v1_alias_maps_to_a_handler_method(self):
        import web_app

        for path in V1_ALIASED_PREFIXES:
            table = POST_TABLE if path in POST_TABLE else GET_TABLE
            method = table[f"/api/v1{path[len('/api'):]}"]
            self.assertTrue(hasattr(web_app.Handler, method), f"handler missing: {method}")

    def test_unknown_v1_route_stays_unmapped(self):
        self.assertNotIn("/api/v1/not-a-real-route", GET_TABLE)
        self.assertNotIn("/api/v1/not-a-real-route", POST_TABLE)


if __name__ == "__main__":
    unittest.main()
