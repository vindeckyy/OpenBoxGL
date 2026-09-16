#!/usr/bin/env python3
"""P1-15: only malformed ``Range`` requests map to 416.

Missing files must be 404 and unexpected failures must not be reported as an
unsatisfiable range.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api_errors import MediaNotFound, RangeParseError  # noqa: E402
from handlers.media import MediaHandlers  # noqa: E402


class ServeMediaMappingTests(unittest.TestCase):
    def _handler(self, headers=None):
        handler = object.__new__(MediaHandlers)
        if headers is not None:
            handler.headers = headers
        return handler

    def test_malformed_range_maps_to_416(self):
        handler = self._handler({"Range": "bytes=abc-def"})
        handler.send_file = mock.Mock(side_effect=RangeParseError("Invalid byte range."))
        handler._send_range_unsatisfiable = mock.Mock()
        handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_called_once_with("/tmp/cover.png")

    def test_invalid_number_range_maps_to_416(self):
        handler = self._handler({"Range": "bytes=not-a-range"})
        handler.send_file = mock.Mock(side_effect=RangeParseError("Invalid byte range."))
        handler._send_range_unsatisfiable = mock.Mock()
        handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_called_once_with("/tmp/cover.png")

    def test_unparseable_left_range_maps_to_416(self):
        handler = self._handler({"Range": "bytes=abcdef"})
        handler.send_file = mock.Mock(side_effect=RangeParseError("Invalid byte range."))
        handler._send_range_unsatisfiable = mock.Mock()
        handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_called_once_with("/tmp/cover.png")

    def test_unparseable_right_range_maps_to_416(self):
        handler = self._handler({"Range": "bytes=1-xyz"})
        handler.send_file = mock.Mock(side_effect=RangeParseError("Invalid byte range."))
        handler._send_range_unsatisfiable = mock.Mock()
        handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_called_once_with("/tmp/cover.png")

    def test_handler_without_headers_still_maps_range_message(self):
        handler = self._handler()
        handler.send_file = mock.Mock(side_effect=RangeParseError("Invalid byte range."))
        handler._send_range_unsatisfiable = mock.Mock()
        handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_called_once_with("/tmp/cover.png")

    def test_unreadable_headers_treated_as_no_range(self):
        handler = self._handler()
        handler.headers = mock.Mock()
        handler.headers.get = mock.Mock(side_effect=TypeError("no mapping"))
        handler.send_file = mock.Mock(side_effect=ValueError("unexpected"))
        handler._send_range_unsatisfiable = mock.Mock()
        with self.assertRaises(ValueError):
            handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_not_called()

    def test_value_error_without_range_is_not_416(self):
        handler = self._handler({})
        handler.send_file = mock.Mock(side_effect=ValueError("unexpected"))
        handler._send_range_unsatisfiable = mock.Mock()
        with self.assertRaises(ValueError):
            handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_not_called()

    def test_value_error_with_valid_range_is_not_416(self):
        handler = self._handler({"Range": "bytes=0-10"})
        handler.send_file = mock.Mock(side_effect=ValueError("unexpected"))
        handler._send_range_unsatisfiable = mock.Mock()
        with self.assertRaises(ValueError):
            handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_not_called()

    def test_missing_file_is_not_416(self):
        handler = self._handler({"Range": "bytes=0-10"})
        handler.send_file = mock.Mock(side_effect=FileNotFoundError("rotated away"))
        handler._send_range_unsatisfiable = mock.Mock()
        with self.assertRaises(FileNotFoundError):
            handler._serve_media("/tmp/cover.png")
        handler._send_range_unsatisfiable.assert_not_called()


class MediaRouteMappingTests(unittest.TestCase):
    def _handler(self, headers):
        handler = object.__new__(MediaHandlers)
        handler.headers = headers
        handler.send_file = mock.Mock()
        handler._send_range_unsatisfiable = mock.Mock()
        return handler

    def _patch_media(self, game):
        return (
            mock.patch("handlers.media.load_state_view", return_value={"games": []}),
            mock.patch("handlers.media.game_from_query", return_value=game),
            mock.patch("handlers.media.approved_media_path", return_value=Path("/tmp/cover.png")),
        )

    def test_media_route_missing_file_is_404(self):
        handler = self._handler({"Range": "bytes=0-10"})
        handler.send_file.side_effect = FileNotFoundError("rotated away")
        patches = self._patch_media({"cover": "/tmp/cover.png"})
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(MediaNotFound):
                handler._api_get_api_media(urlparse("/api/media?kind=cover"))
        handler._send_range_unsatisfiable.assert_not_called()

    def test_media_route_malformed_range_is_416(self):
        handler = self._handler({"Range": "bytes=abc"})
        handler.send_file.side_effect = RangeParseError("Invalid byte range.")
        patches = self._patch_media({"cover": "/tmp/cover.png"})
        with patches[0], patches[1], patches[2]:
            handler._api_get_api_media(urlparse("/api/media?kind=cover"))
        handler._send_range_unsatisfiable.assert_called_once()

    def test_memories_route_missing_file_is_404(self):
        handler = self._handler({"Range": "bytes=0-10"})
        handler.send_file.side_effect = FileNotFoundError("rotated away")
        with mock.patch("handlers.media.load_state_view", return_value={"games": [], "unassigned": []}), \
             mock.patch("handlers.media.game_from_query", return_value={"memories": [{"path": "/tmp/m.png"}]}), \
             mock.patch("handlers.media.approved_media_path", return_value=Path("/tmp/m.png")):
            with self.assertRaises(MediaNotFound):
                handler._api_get_api_v2_memories_media(urlparse("/api/v2/memories/media?index=0"))
        handler._send_range_unsatisfiable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
