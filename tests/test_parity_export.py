"""Tests for the library export (pkg/parity/parity_export + ExportHandlers)."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_export import (  # noqa: E402
    approved_export_file,
    build_export_games,
    export_row,
    export_rows,
    list_exports,
    prune_exports,
    write_export,
)


def sample_state():
    return {
        "games": [
            {"game_id": "game-a", "id": 1, "name": "Alpha", "platform": "SNES", "playtime_seconds": 3600,
             "alternate_names": ["Alt One", "Alt Two"], "favorite": True, "cover": "media/cover.png"},
            {"game_id": "game-b", "id": 2, "name": "Beta", "platform": "PC", "playtime_seconds": 60},
            {"game_id": "game-c", "id": 3, "name": "Gamma", "platform": "SNES"},
        ],
        "playlists": [{"name": "Couch", "type": "manual", "members": ["game-a", "game-c"]}],
    }


class BuildExportGamesTest(unittest.TestCase):
    def test_scope_all(self):
        games = build_export_games(sample_state())
        self.assertEqual(len(games), 3)

    def test_scope_platform(self):
        games = build_export_games(sample_state(), scope="platform", scope_name="SNES")
        self.assertEqual([game["name"] for game in games], ["Alpha", "Gamma"])

    def test_scope_playlist(self):
        games = build_export_games(sample_state(), scope="playlist", scope_name="Couch")
        self.assertEqual([game["name"] for game in games], ["Alpha", "Gamma"])

    def test_scope_validation(self):
        with self.assertRaises(ValueError):
            build_export_games(sample_state(), scope="galaxy")
        with self.assertRaises(ValueError):
            build_export_games(sample_state(), scope="platform")
        with self.assertRaises(ValueError):
            build_export_games(sample_state(), scope="playlist", scope_name="Missing")
        self.assertEqual(build_export_games("not-a-state"), [])


class ExportRowsTest(unittest.TestCase):
    def test_row_fields_and_list_join(self):
        row = export_row(sample_state()["games"][0])
        self.assertEqual(row["name"], "Alpha")
        self.assertEqual(row["alternate_names"], "Alt One;Alt Two")
        self.assertEqual(row["rating"], "")
        self.assertNotIn("cover", row)

    def test_media_paths_opt_in(self):
        game = sample_state()["games"][0]
        self.assertNotIn("cover", export_row(game))
        self.assertEqual(export_row(game, include_media_paths=True)["cover"], "media/cover.png")

    def test_rows_skip_garbage(self):
        rows = export_rows([{"name": "Ok"}, None, 5])
        self.assertEqual(len(rows), 1)


class WriteExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def test_json_export_shape(self):
        rows = export_rows(sample_state()["games"])
        path = write_export(self.data_dir, rows, fmt="json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "library-export")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["games"]), 3)
        self.assertNotIn("cover", payload["games"][0])

    def test_csv_export_headers(self):
        rows = export_rows(sample_state()["games"], include_media_paths=True)
        path = write_export(self.data_dir, rows, fmt="csv", include_media_paths=True)
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows_read = list(reader)
        self.assertIn("game_id", reader.fieldnames)
        self.assertIn("cover", reader.fieldnames)
        self.assertEqual(rows_read[0]["name"], "Alpha")

    def test_bad_format(self):
        with self.assertRaises(ValueError):
            write_export(self.data_dir, [], fmt="xml")

    def test_prune_keeps_newest_ten(self):
        for _ in range(12):
            write_export(self.data_dir, [], fmt="json")
        self.assertEqual(len(list_exports(self.data_dir)), 10)

    def test_prune_zero_clears(self):
        write_export(self.data_dir, [], fmt="json")
        prune_exports(self.data_dir / "exports", keep=0)
        self.assertEqual(list_exports(self.data_dir), [])


class ApprovedExportFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.path = write_export(self.data_dir, [], fmt="json")

    def test_accepts_real_export(self):
        self.assertEqual(approved_export_file(self.data_dir, self.path.name), self.path)

    def test_rejects_bad_names(self):
        self.assertIsNone(approved_export_file(self.data_dir, ""))
        self.assertIsNone(approved_export_file(self.data_dir, "openbox-library-99999999-999999.json"))
        self.assertIsNone(approved_export_file(self.data_dir, "../library.json"))
        self.assertIsNone(approved_export_file(self.data_dir, "openbox-library-20260902-000000.json.bak"))
        missing = approved_export_file(self.data_dir, "openbox-library-20200101-000000.json")
        self.assertIsNone(missing)


class ExportHandlerTest(unittest.TestCase):
    def handler(self):
        import io

        import web_app
        h = web_app.Handler.__new__(web_app.Handler)
        h.responses = []
        h.headers = {}
        h.body = io.BytesIO()
        h.sent_headers = []
        h.send_json = lambda status, payload: h.responses.append((status, payload))
        h.send_response = lambda status: h.sent_headers.append(("status", status))
        h.headers_common = lambda content_type: h.sent_headers.append(("content-type", content_type))
        h.send_header = lambda name, value: h.sent_headers.append((name, value))
        h.end_headers = lambda: None
        h.wfile = h.body
        return h

    def test_download_route_serves_file(self):
        from urllib.parse import urlparse

        import handlers.export as export_module
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            original_data = export_module.DATA
            try:
                export_module.DATA = Path(tmp) / "library.json"
                path = write_export(Path(tmp), export_rows(sample_state()["games"]), fmt="json")
                h = self.handler()
                parsed = urlparse(f"/api/v2/library/export/download?file={path.name}")
                h._api_get_api_v2_library_export_download(parsed)
                self.assertIn(("status", 200), h.sent_headers)
                payload = json.loads(h.body.getvalue().decode("utf-8"))
                self.assertEqual(payload["count"], 3)
            finally:
                export_module.DATA = original_data

    def test_download_route_404_unknown(self):
        from urllib.parse import urlparse

        h = self.handler()
        parsed = urlparse("/api/v2/library/export/download?file=missing.json")
        h._api_get_api_v2_library_export_download(parsed)
        self.assertEqual(h.responses[0][0], 404)

    def test_exports_list_route(self):
        from urllib.parse import urlparse

        import handlers.export as export_module
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as tmp:
            original_data = export_module.DATA
            try:
                export_module.DATA = Path(tmp) / "library.json"
                write_export(Path(tmp), [], fmt="csv")
                h = self.handler()
                h._api_get_api_v2_library_export_exports(urlparse("/api/v2/library/export/exports"))
                status, payload = h.responses[0]
                self.assertEqual(status, 200)
                self.assertEqual(len(payload["exports"]), 1)
                self.assertIn("bytes", payload["exports"][0])
            finally:
                export_module.DATA = original_data

    def test_export_job_roundtrip(self):
        import tempfile as _tempfile
        import handlers.export as export_module
        from pkg.parity.parity_export import EXPORT_DIR_NAME

        with _tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state_path = data_dir / "library.json"
            state_path.write_text(json.dumps({"games": sample_state()["games"], "profiles": {}, "settings": {}, "history": [], "playlists": []}), encoding="utf-8")
            captured = {}
            class FakeJobManager:
                def submit(self, name, worker, **kwargs):
                    captured["name"] = name
                    captured["result"] = worker(None)
                    return {"job_id": "job-1"}
            original_data = export_module.DATA
            original_jobs = export_module.JOB_MANAGER
            original_load = export_module.load_state
            try:
                export_module.DATA = state_path
                export_module.JOB_MANAGER = FakeJobManager()
                export_module.load_state = lambda: sample_state()
                h = self.handler()
                h._api_post_api_v2_library_export({"format": "json", "scope": "all"})
                self.assertEqual(h.responses[0][0], 202)
                self.assertEqual(captured["name"], "library-export")
                self.assertEqual(captured["result"]["count"], 3)
                # The download route serves the written file.
                approved = approved_export_file(data_dir, captured["result"]["file"])
                self.assertIsNotNone(approved)
                self.assertIn(EXPORT_DIR_NAME, str(approved))
            finally:
                export_module.DATA = original_data
                export_module.JOB_MANAGER = original_jobs
                export_module.load_state = original_load

    def test_export_bad_format_raises(self):
        h = self.handler()
        with self.assertRaises(ValueError):
            h._api_post_api_v2_library_export({"format": "xml"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
