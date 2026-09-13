#!/usr/bin/env python3
"""Real HTTP regression for review-bound LaunchBox migration."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class LaunchBoxHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["OPENBOX_DATA_DIR"] = cls.tmp.name
        import openbox
        import web_app

        cls.openbox = openbox
        web_app.TOKEN = "launchbox-http-token"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tmp.cleanup()
        os.environ.pop("OPENBOX_DATA_DIR", None)

    def setUp(self):
        self.openbox.STATE_STORE.save({"games": [], "profiles": {}, "settings": {}})
        self.xml = Path(self.tmp.name) / "platform.xml"
        self.xml.write_text("<LaunchBox><Game><ID>lb-1</ID><DatabaseID>9001</DatabaseID><Title>Quake</Title><Platform>PC</Platform></Game></LaunchBox>", encoding="utf-8")

    def post(self, path, body, token="launchbox-http-token"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-OpenBox-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_preview_apply_token_and_auth(self):
        payload = {"xml_path": str(self.xml), "options": {"path_mappings": {}}}
        status, preview = self.post("/api/v2/import/launchbox/preview", payload)
        self.assertEqual(status, 200)
        self.assertEqual(preview["added"], 1)
        status, result = self.post("/api/v2/import/launchbox/apply", {
            "xml_path": str(self.xml), "plan": preview["plan"], "preview_token": preview["preview_token"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.openbox.load_state()["games"][0]["launchbox_source_id"], "lb-1")
        status, body = self.post("/api/v2/import/launchbox/preview", payload, token="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "Unauthorized")

    def test_apply_ignores_tampered_client_operations(self):
        status, preview = self.post("/api/v2/import/launchbox/preview", {
            "xml_path": str(self.xml), "options": {},
        })
        self.assertEqual(status, 200)
        preview["plan"]["operations"][0]["game"]["name"] = "Forged"
        status, result = self.post("/api/v2/import/launchbox/apply", {
            "xml_path": str(self.xml), "plan": preview["plan"],
            "preview_token": preview["preview_token"], "options": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.openbox.load_state()["games"][0]["name"], "Quake")

    def test_apply_without_plan_uses_same_transactional_planner(self):
        status, result = self.post("/api/v2/import/launchbox/apply", {
            "xml_path": str(self.xml), "options": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.openbox.load_state()["games"][0]["name"], "Quake")

    def test_esde_preview_apply_and_source_stale(self):
        esde = Path(self.tmp.name) / "gamelist.xml"
        esde.write_text(
            "<gameList><game><path>./quake.zip</path><name>Quake</name>"
            "<system>pc</system><id>esde-q1</id></game></gameList>",
            encoding="utf-8",
        )
        status, preview = self.post("/api/v2/import/esde/preview", {"xml_path": str(esde)})
        self.assertEqual(status, 200)
        self.assertEqual(preview["counts"]["added"], 1)
        status, result = self.post("/api/v2/import/esde/apply", {
            "xml_path": str(esde), "plan": preview, "preview_token": preview["preview_token"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.openbox.load_state()["games"][0]["source_identity"], "esde:esde-q1")

        esde.write_text(
            "<gameList><game><path>./quake.zip</path><name>Quake changed</name>"
            "<system>pc</system><id>esde-q1</id></game></gameList>",
            encoding="utf-8",
        )
        status, body = self.post("/api/v2/import/esde/apply", {
            "xml_path": str(esde), "plan": preview, "preview_token": preview["preview_token"],
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "ESDE_STALE_PLAN")

    def test_esde_apply_without_plan_and_invalid_source(self):
        esde = Path(self.tmp.name) / "gamelist.xml"
        esde.write_text(
            "<gameList><game><path>./doom.zip</path><name>Doom</name></game></gameList>",
            encoding="utf-8",
        )
        status, result = self.post("/api/v2/import/esde/apply", {"xml_path": str(esde)})
        self.assertEqual(status, 200)
        self.assertEqual(result["added"], 1)

        esde.write_text("<not-a-gamelist/>", encoding="utf-8")
        status, body = self.post("/api/v2/import/esde/preview", {"xml_path": str(esde)})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "ESDE_INVALID_SOURCE")
        status, body = self.post("/api/v2/import/esde/apply", {"xml_path": str(esde)})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "ESDE_INVALID_SOURCE")

        status, body = self.post("/api/v2/import/esde/preview", {
            "xml_path": str(Path(self.tmp.name) / "missing-gamelist.xml"),
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
