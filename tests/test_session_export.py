#!/usr/bin/env python3
"""P5: session/journal export tests (markdown/CSV, per game and per member)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

import handlers.data as data_handlers  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from handlers.data import (  # noqa: E402
    DataHandlers,
    member_export_rows,
    render_session_export,
    session_export_rows,
)


class StreamHandler(DataHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))

    def send_file(self, status, path, content_type=None, extra_headers=None, frameable=False):
        text = Path(path).read_text(encoding="utf-8")
        self.responses.append((status, content_type, extra_headers, text))


class SessionExportTests(unittest.TestCase):
    def setUp(self):
        self.history = [
            {"game": "Quake", "game_id": "g1", "started": "2026-01-01T10:00:00", "seconds": 90, "exit_code": 0},
            {"game": "Quake", "game_id": "g1", "started": "2026-01-02T10:00:00", "seconds": 30, "exit_code": 1},
            {"game": "Doom", "game_id": "g2", "started": "2026-01-03T10:00:00", "seconds": 3600, "exit_code": 0},
        ]
        self.state = {
            "games": [{"game_id": "g1", "name": "Quake", "path": "/q"}, {"game_id": "g2", "name": "Doom", "path": "/d"}],
            "history": self.history,
            "settings": {},
            "playlists": [],
        }
        patches = [mock.patch.object(data_handlers, "load_state_view", side_effect=lambda: self.state)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_rows_are_bounded_and_normalized(self):
        rows = session_export_rows(self.state)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["minutes"], 1.5)
        self.assertEqual(rows[0]["started"], "2026-01-01T10:00:00")
        bounded = session_export_rows({"history": [{"seconds": 1} for _ in range(6000)]})
        self.assertEqual(len(bounded), data_handlers.SESSION_EXPORT_MAX_ROWS)

    def test_markdown_escapes_pipes(self):
        text = render_session_export("game", "md", [{"started": "x", "game": "A|B", "minutes": 1, "seconds": 60, "exit_code": 0}])
        self.assertIn("A\\|B", text)
        self.assertTrue(text.startswith("# "))

    def test_csv_quotes_values(self):
        text = render_session_export("game", "csv", [{"started": "x", "game": 'He said "hi"', "minutes": 1, "seconds": 60, "exit_code": 0}])
        self.assertIn('"He said ""hi"""', text)

    def test_member_rows_come_from_household_shares(self):
        board = {
            "entries": [
                {"member_id": "m1", "display_name": "Ada", "sessions": 4, "playtime_seconds": 7200, "games_played": 2, "completions": 1},
            ]
        }
        with mock.patch.object(data_handlers, "compute_leaderboard", return_value=board):
            rows = member_export_rows(self.state)
        self.assertEqual(rows, [{
            "member_id": "m1", "display_name": "Ada", "sessions": 4,
            "playtime_seconds": 7200, "playtime_hours": 2.0, "games_played": 2, "completions": 1,
        }])
        text = render_session_export("member", "md", rows, title="Household playtime")
        self.assertIn("not member-attributed", text)

    def test_export_route_streams_game_csv(self):
        handler = StreamHandler()
        handler._api_get_api_v2_sessions_export(SimpleNamespace(query="scope=game&format=csv&game_id=g1"))
        status, content_type, headers, text = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/csv; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="openbox-sessions-Quake.csv"')
        self.assertIn("Quake", text)
        self.assertNotIn("Doom", text)
        self.assertTrue(text.startswith("Started,Game"))

    def test_export_route_defaults_to_all_game_sessions(self):
        handler = StreamHandler()
        handler._api_get_api_v2_sessions_export(SimpleNamespace(query="scope=game&format=md"))
        _status, _content_type, headers, text = handler.responses[0]
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="openbox-sessions-all.md"')
        self.assertIn("Doom", text)

    def test_export_route_member_scope(self):
        board = {"entries": [{"member_id": "m1", "display_name": "Ada", "sessions": 1, "playtime_seconds": 60}]}
        with mock.patch.object(data_handlers, "compute_leaderboard", return_value=board):
            handler = StreamHandler()
            handler._api_get_api_v2_sessions_export(SimpleNamespace(query="scope=member&format=csv"))
        status, _content_type, headers, text = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="openbox-household-playtime.csv"')
        self.assertIn("Ada", text)

    def test_export_route_rejects_unknown_format_and_scope(self):
        with self.assertRaises(BadRequest):
            StreamHandler()._api_get_api_v2_sessions_export(SimpleNamespace(query="scope=game&format=pdf"))
        with self.assertRaises(BadRequest):
            StreamHandler()._api_get_api_v2_sessions_export(SimpleNamespace(query="scope=everyone&format=md"))

    def test_export_route_registered(self):
        from routes.registry import all_routes
        self.assertTrue(any(route.path == "/api/v2/sessions/export" for route in all_routes()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
