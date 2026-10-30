#!/usr/bin/env python3
"""Typed response schemas (plan W2-08).

The schemas in scripts/api_schemas.py must be internally consistent, must name live routes,
and must match the payloads the server actually returns. A schema that drifts from the live
response fails here rather than misleading a client.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import api_schemas  # noqa: E402
import gen_openapi  # noqa: E402


class SchemaCatalogTests(unittest.TestCase):
    def test_the_catalog_holds_forty_schemas_and_they_are_all_declared(self):
        self.assertEqual(len(api_schemas.SCHEMAS), 40)
        self.assertEqual(sorted(api_schemas.SCHEMAS), sorted(api_schemas.SCHEMA_NAMES))

    def test_every_required_field_is_also_a_property(self):
        for name, schema in api_schemas.SCHEMAS.items():
            with self.subTest(schema=name):
                properties = schema.get("properties", {})
                for required in schema.get("required", []):
                    self.assertIn(required, properties, f"{name} requires {required} but does not define it")

    def test_every_reference_resolves(self):
        for name, schema in api_schemas.SCHEMAS.items():
            with self.subTest(schema=name):
                for referenced in json.dumps(schema).split('"$ref":"')[1:]:
                    self.assertIn(referenced.split('"')[0], api_schemas.SCHEMAS)

    def test_every_typed_operation_names_a_published_schema_and_a_live_route(self):
        live = set(gen_openapi.live_operations())
        for (method, path), (name, description) in api_schemas.OPERATIONS.items():
            with self.subTest(operation=f"{method} {path}"):
                self.assertIn((method, path), live)
                self.assertIn(name, api_schemas.SCHEMAS)
                self.assertTrue(description.strip())


class ValidatorTests(unittest.TestCase):
    def test_a_valid_instance_passes(self):
        payload = {"version": "2.0.0", "api_version": 2, "state_schema": 7, "plugin_api_version": 2,
                   "backup_format": 2, "sync_format": 3, "platform": "linux", "python": "3.12.15"}
        self.assertEqual(api_schemas.validate(payload, api_schemas.SCHEMAS["Version"]), [])

    def test_a_missing_required_field_is_reported(self):
        self.assertTrue(api_schemas.validate({"version": "2.0.0"}, api_schemas.SCHEMAS["Version"]))

    def test_a_wrong_type_is_reported(self):
        self.assertTrue(api_schemas.validate({"game_id": 7, "name": "x"}, api_schemas.SCHEMAS["Game"]))

    def test_a_nested_object_is_validated(self):
        problems = api_schemas.validate({"games": [{"game_id": "g1"}]}, api_schemas.SCHEMAS["Library"])
        self.assertTrue(any("name" in problem for problem in problems))


class LivePayloadTests(unittest.TestCase):
    def test_the_version_payload_matches_its_schema(self):
        from handlers.native import version_info

        class Handler:
            def __init__(self):
                self.payload = None

            def send_json(self, status, payload):
                self.payload = payload

        handler = Handler()
        version_info(handler, None)
        self.assertEqual(api_schemas.validate(handler.payload, api_schemas.SCHEMAS["Version"]), [])

    def test_the_integrity_report_matches_its_schema(self):
        from pkg.state import library_v7
        from state_store import default_state

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "library.json").write_text(json.dumps(default_state()), encoding="utf-8")
            report = library_v7.check_library(data)
        self.assertEqual(api_schemas.validate(report, api_schemas.SCHEMAS["IntegrityReport"]), [])

    def test_the_compatibility_report_matches_its_schema(self):
        from pkg.parity import parity_compat

        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "library.json").write_text("{}", encoding="utf-8")
            report = parity_compat.build_report(data, root=ROOT)
        self.assertEqual(api_schemas.validate(report, api_schemas.SCHEMAS["CompatReport"]), [])


class DocumentTests(unittest.TestCase):
    def test_the_document_publishes_every_schema(self):
        document = gen_openapi.build()
        for name in api_schemas.SCHEMAS:
            self.assertIn(name, document["components"]["schemas"])

    def test_a_typed_operation_references_its_schema(self):
        document = gen_openapi.build()
        operation = document["paths"]["/api/v2/version"]["get"]
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/Version"},
        )

    def test_an_untyped_operation_says_so(self):
        document = gen_openapi.build()
        operation = document["paths"]["/api/v2/library/constellation"]["get"]
        self.assertEqual(operation.get("x-openbox-schema"), "untyped")


if __name__ == "__main__":
    unittest.main()
