#!/usr/bin/env python3
"""Metadata database verification tests (P8.5)."""

import hashlib
import io
import sys
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import metadata


@contextmanager
def _temp_dir():
    """TemporaryDirectory that drops cached SQLite handles before cleanup."""
    with TemporaryDirectory() as folder:
        try:
            yield Path(folder)
        finally:
            metadata.close_db_connections()


def _package_bytes(title="Verified Game"):
    xml = f"""<LaunchBox>
      <Game><DatabaseID>7</DatabaseID><Name>{title}</Name><Platform>NES</Platform></Game>
    </LaunchBox>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadata.xml", xml)
    return buffer.getvalue()


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SyncVerificationTests(unittest.TestCase):
    def test_pinned_checksum_accepts_and_reports_phases(self):
        payload = _package_bytes()
        digest = hashlib.sha256(payload).hexdigest()

        def opener(request, timeout=0):
            return Response(payload)

        with _temp_dir() as folder, \
             mock.patch.object(metadata, "DATABASE_SHA256", digest):
            destination = Path(folder) / "metadata.db"
            phases = []
            metadata.sync_database(destination, opener=opener, progress=phases.append)
            self.assertTrue(destination.is_file())
            self.assertEqual(metadata.search_games(destination, "Verified Game", "NES")[0]["database_id"], 7)
            names = [event["phase"] for event in phases]
            self.assertIn("download", names)
            self.assertIn("verify", names)
            self.assertIn("build", names)
            self.assertTrue(any(event["bytes"] > 0 for event in phases if event["phase"] == "download"))
            self.assertTrue(any(event["phase"] == "verify" and event["total"] > 0 for event in phases))

    def test_mismatch_retries_and_raises_without_leaking_temp(self):
        payload = _package_bytes()
        calls = []
        sleeps = []

        def opener(request, timeout=0):
            calls.append(request.full_url)
            return Response(payload)

        with _temp_dir() as folder, \
             mock.patch.object(metadata, "DATABASE_SHA256", "0" * 64):
            destination = Path(folder) / "metadata.db"
            with self.assertRaises(ValueError) as caught:
                metadata.sync_database(destination, opener=opener, sleep=sleeps.append)
            self.assertIn("checksum mismatch", str(caught.exception))
            self.assertEqual(len(calls), 3)
            self.assertEqual(sleeps, [2, 8])
            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(folder).glob("*.zip")), [])

    def test_trust_on_first_use_records_digest_then_enforces_it(self):
        payload = _package_bytes()
        digest = hashlib.sha256(payload).hexdigest()

        def good_opener(request, timeout=0):
            return Response(payload)

        with _temp_dir() as folder, \
             mock.patch.object(metadata, "DATABASE_SHA256", ""):
            destination = Path(folder) / "metadata.db"
            checksum = Path(folder) / metadata.METADATA_SHA_FILE
            metadata.sync_database(destination, opener=good_opener, checksum_path=checksum)
            self.assertEqual(checksum.read_text(encoding="utf-8").strip(), digest)

            def bad_opener(request, timeout=0):
                return Response(_package_bytes("Other Game"))

            with self.assertRaises(ValueError):
                metadata.sync_database(
                    destination, opener=bad_opener, sleep=lambda _seconds: None, checksum_path=checksum,
                )

    def test_offline_error_is_clean(self):
        from urllib.error import URLError

        def opener(request, timeout=0):
            raise URLError("Name or service not known")

        with _temp_dir() as folder:
            with self.assertRaises(ValueError) as caught:
                metadata.sync_database(Path(folder) / "metadata.db", opener=opener)
            self.assertIn("connection", str(caught.exception).casefold())

    def test_progress_callback_failure_is_ignored(self):
        payload = _package_bytes()

        def opener(request, timeout=0):
            return Response(payload)

        def broken(_event):
            raise RuntimeError("ui gone")

        with _temp_dir() as folder, \
             mock.patch.object(metadata, "DATABASE_SHA256", hashlib.sha256(payload).hexdigest()):
            destination = Path(folder) / "metadata.db"
            metadata.sync_database(destination, opener=opener, progress=broken)
            self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
