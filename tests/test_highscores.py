"""Tests for pkg/parity/parity_highscores.py (P11.5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity import parity_highscores as hs  # noqa: E402

TABLE = json.dumps([
    {"rank": 1, "score": "100,000", "name": "AAA"},
    {"rank": 2, "score": 500, "name": "BBB"},
    {"rank": "bad", "score": 10, "name": "ignored"},
])


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def ok(stdout=TABLE, stderr=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)


def fail(stderr="boom", stdout=""):
    return SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)


class DiscoverTests(unittest.TestCase):
    def test_discover_lists_files_with_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            hi = home / ".mame" / "hi"
            hi.mkdir(parents=True)
            (hi / "pacman.hi").write_bytes(b"1234")
            (hi / "dkong.hi").write_bytes(b"12")
            (hi / "notes.txt").write_text("x", encoding="utf-8")
            result = hs.discover(home=home)
            self.assertEqual(result["count"], 3)
            self.assertEqual([item["name"] for item in result["files"]],
                             ["dkong.hi", "notes.txt", "pacman.hi"])
            pacman = next(item for item in result["files"] if item["name"] == "pacman.hi")
            self.assertEqual(pacman["size"], 4)
            self.assertEqual(pacman["label"], "pacman")

    def test_missing_root_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            result = hs.discover(home=Path(directory))
            self.assertEqual(result["files"], [])
            self.assertEqual(result["count"], 0)


class ParseTests(unittest.TestCase):
    def test_missing_tool_reports_not_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pacman.hi"
            path.write_bytes(b"\x00\x01")
            result = hs.parse(path, "pacman", which=lambda _name: None)
        self.assertFalse(result["parsed"])
        self.assertEqual(result["scores"], [])
        self.assertIn("not installed", result["message"])
        self.assertEqual(result["name"], "pacman.hi")
        self.assertEqual(result["size"], 2)

    def test_json_table_is_parsed_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pacman.hi"
            path.write_bytes(b"x")
            runner = Runner(ok())
            result = hs.parse(path, "pacman", which=lambda _name: "/usr/bin/hi2txt", run=runner)
        self.assertTrue(result["parsed"])
        self.assertEqual(result["scores"], [
            {"rank": 1, "score": 100000, "name": "AAA"},
            {"rank": 2, "score": 500, "name": "BBB"},
        ])
        command, kwargs = runner.calls[0]
        self.assertEqual(
            command,
            ["/usr/bin/hi2txt", "--format", "json", "pacman", str(path)],
        )
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], hs.PARSE_TIMEOUT)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    def test_rows_are_capped(self):
        rows = json.dumps([
            {"rank": index, "score": index * 10, "name": f"P{index}"}
            for index in range(1, 151)
        ])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"x")
            result = hs.parse(path, run=Runner(ok(stdout=rows)), which=lambda _name: "hi2txt")
        self.assertTrue(result["parsed"])
        self.assertEqual(len(result["scores"]), hs.MAX_ROWS)
        self.assertEqual(result["scores"][-1]["rank"], hs.MAX_ROWS)

    def test_pipe_table_is_parsed_when_json_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"\xde\xad")
            result = hs.parse(
                path,
                run=Runner(ok(stdout="RANK|SCORE|NAME\n1|10|A\n2|9,500|B|C")),
                which=lambda _name: "hi2txt",
            )
        self.assertTrue(result["parsed"])
        self.assertEqual(result["scores"], [
            {"rank": 1, "score": 10, "name": "A"},
            {"rank": 2, "score": 9500, "name": "B|C"},
        ])

    def test_pipe_table_without_header_uses_rank_score_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"x")
            result = hs.parse(path, run=Runner(ok(stdout="1|100|AAA\n2|50|BBB")),
                              which=lambda _name: "hi2txt")
        self.assertTrue(result["parsed"])
        self.assertEqual(result["scores"][0], {"rank": 1, "score": 100, "name": "AAA"})

    def test_output_without_a_table_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"\xde\xad")
            result = hs.parse(path, run=Runner(ok(stdout="hi2txt could not identify this ROM")),
                              which=lambda _name: "hi2txt")
        self.assertFalse(result["parsed"])
        self.assertEqual(result["scores"], [])
        self.assertIn("usable score table", result["message"])

    def test_empty_json_array_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"x")
            result = hs.parse(path, run=Runner(ok(stdout="[]")), which=lambda _name: "hi2txt")
        self.assertFalse(result["parsed"])
        self.assertIn("no usable score rows", result["message"])

    def test_nonzero_exit_reports_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"x")
            result = hs.parse(path, run=Runner(fail("cannot parse")), which=lambda _name: "hi2txt")
        self.assertFalse(result["parsed"])
        self.assertIn("cannot parse", result["message"])

    def test_timeout_is_reported_without_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "game.hi"
            path.write_bytes(b"x")
            runner = Runner(subprocess.TimeoutExpired(["hi2txt"], 10))
            result = hs.parse(path, run=runner, which=lambda _name: "hi2txt")
        self.assertFalse(result["parsed"])
        self.assertIn("timed out", result["message"])

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = hs.parse(Path(directory) / "none.hi")
        self.assertFalse(result["parsed"])
        self.assertIn("not found", result["message"])


class TableTests(unittest.TestCase):
    def test_table_finds_rom_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            hi = home / ".mame" / "hi"
            hi.mkdir(parents=True)
            path = hi / "pacman.hi"
            path.write_bytes(b"x")
            runner = Runner(ok())
            result = hs.table({"rom_name": "pacman"}, home=home, which=lambda _name: "hi2txt", run=runner)
        self.assertTrue(result["parsed"])
        self.assertEqual(result["path"], str(path.resolve()))
        self.assertEqual(result["scores"][0]["score"], 100000)

    def test_table_uses_path_stem(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            hi = home / ".mame" / "hi"
            hi.mkdir(parents=True)
            (hi / "dkong.hi").write_bytes(b"x")
            runner = Runner(ok())
            result = hs.table({"path": "/roms/dkong.zip"}, home=home, which=lambda _name: "hi2txt", run=runner)
        self.assertTrue(result["parsed"])
        self.assertEqual(result["rom_name"], "dkong")

    def test_table_without_file_reports_message(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".mame" / "hi").mkdir(parents=True)
            result = hs.table({"rom_name": "pacman"}, home=home, which=lambda _name: "hi2txt",
                              run=Runner())
        self.assertFalse(result["parsed"])
        self.assertIn("No high score file", result["message"])

    def test_table_without_rom(self):
        with tempfile.TemporaryDirectory() as directory:
            result = hs.table({"name": "No path"}, home=Path(directory))
        self.assertFalse(result["parsed"])
        self.assertEqual(result["rom_name"], "")
        self.assertIn("No ROM name", result["message"])

    def test_table_refuses_symlink_outside_root(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            home = Path(directory)
            hi = home / ".mame" / "hi"
            hi.mkdir(parents=True)
            target = Path(outside) / "secret.hi"
            target.write_bytes(b"secret")
            link = hi / "pacman.hi"
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            runner = Runner(ok())
            result = hs.table({"rom_name": "pacman"}, home=home, which=lambda _name: "hi2txt", run=runner)
        self.assertFalse(result["parsed"])
        self.assertEqual(result["scores"], [])
        self.assertIn("outside the MAME directory", result["message"])
        self.assertEqual(runner.calls, [])


if __name__ == "__main__":
    unittest.main()
