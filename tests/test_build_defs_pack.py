#!/usr/bin/env python3
"""The pack builder emits what the update channel can actually read, byte-for-byte reproducibly."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pkg.parity  # noqa: E402,F401  (installs the flat parity_* import finder)
import build_defs_pack  # noqa: E402
from parity_emulator_defs_update import read_pack, validate_definition  # noqa: E402


class BuildDefsPackTests(unittest.TestCase):
    def test_pack_round_trips_through_the_channel_reader(self):
        archive = build_defs_pack.build_archive(ROOT / "emulator_defs")
        definitions = read_pack(archive)
        bundled = sorted(p.name for p in (ROOT / "emulator_defs").glob("*.yaml"))
        self.assertEqual(sorted(definitions), bundled)
        for name, text in definitions.items():
            validate_definition(name, text)  # the channel would accept every one

    def test_build_is_byte_deterministic(self):
        first = build_defs_pack.build_archive(ROOT / "emulator_defs")
        second = build_defs_pack.build_archive(ROOT / "emulator_defs")
        self.assertEqual(first, second, "same inputs must give the same bytes, or the signature drifts")

    def test_writes_archive_and_index_without_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(0, build_defs_pack.main(["--version", "9.9.9", "--notes", "n", "--out", directory]))
            out = Path(directory)
            index = json.loads((out / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index, {"version": "9.9.9", "notes": "n"})
            self.assertTrue((out / "community-defs.tar.gz").is_file())
            self.assertFalse((out / "community-defs.tar.gz.sig").exists(), "the builder must never sign")

    def test_refuses_an_empty_definitions_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                build_defs_pack.build_archive(Path(directory))


if __name__ == "__main__":
    unittest.main()
