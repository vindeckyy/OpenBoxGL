"""Standalone tests for the dependency-free Steam Bridge core."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_steam_bridge import (  # noqa: E402
    MAX_SHORTCUTS_BYTES,
    OPENBOX_APPID_MASK,
    TYPE_END,
    TYPE_INT32,
    TYPE_RECORD,
    TYPE_STRING,
    ShortcutsCorruptError,
    ShortcutsStaleError,
    ShortcutsTooLargeError,
    appid_for,
    apply_shortcuts,
    encode_shortcuts,
    is_bridge_shortcut,
    make_shortcut,
    parse_shortcuts,
    preview_remove,
    preview_shortcuts,
    remove_shortcuts,
    shortcut_from_game,
    write_shortcuts_bytes,
)


def _record(tag: int, key: str, value=b"") -> bytes:
    key_bytes = key.encode("utf-8")
    if tag == TYPE_RECORD:
        return bytes((tag,)) + key_bytes + b"\x00" + value + bytes((TYPE_END,))
    if tag == TYPE_INT32:
        return bytes((tag,)) + key_bytes + b"\x00" + struct.pack("<I", value)
    return bytes((tag,)) + key_bytes + b"\x00" + value + b"\x00"


def _document(*entries: bytes) -> bytes:
    return _record(TYPE_RECORD, "shortcuts", b"".join(entries))


class BinaryCodecTests(unittest.TestCase):
    def test_parses_valve_canonical_tag_order(self):
        # Independent wire fixture: Valve KeyValues uses 0x01 for strings and
        # 0x02 for little-endian uint32 values.
        canonical = (
            b"\x00shortcuts\x00"
            b"\x000\x00"
            b"\x01AppName\x00Fixture\x00"
            b"\x02appid\x00\x2a\x00\x00\x80"
            b"\x08\x08"
        )
        parsed = parse_shortcuts(canonical)
        self.assertEqual(parsed.to_dict()["shortcuts"]["0"]["AppName"], "Fixture")
        self.assertEqual(parsed.to_dict()["shortcuts"]["0"]["appid"], 0x8000002A)
        self.assertEqual(encode_shortcuts(parsed), canonical)

    def test_parses_and_encodes_record_int32_string_tags(self):
        nested = _document(
            _record(TYPE_RECORD, "0", b"".join(
                (
                    _record(TYPE_INT32, "appid", 0x8000002A),
                    _record(TYPE_STRING, "AppName", b"Fixture"),
                    _record(TYPE_STRING, "Exe", b"/usr/bin/openbox"),
                )
            ))
        )
        parsed = parse_shortcuts(nested)
        self.assertEqual(parsed.root_key, "shortcuts")
        entry = parsed.records[0]
        self.assertEqual((entry.key, entry.tag), ("0", TYPE_RECORD))
        self.assertEqual([child.tag for child in entry.children], [TYPE_INT32, TYPE_STRING, TYPE_STRING])
        self.assertEqual(entry.children[0].value, 0x8000002A)
        self.assertEqual(encode_shortcuts(parsed), nested)

    def test_foreign_fields_and_non_utf8_strings_round_trip_exactly(self):
        foreign = _record(TYPE_RECORD, "0", b"".join(
            (
                _record(TYPE_INT32, "appid", 123),
                _record(TYPE_STRING, "AppName", b"Foreign"),
                _record(TYPE_STRING, "SteamFutureField", b"\xff\xfe"),
                _record(TYPE_RECORD, "tags", _record(TYPE_STRING, "0", b"Deck")),
            )
        ))
        data = _document(foreign)
        parsed = parse_shortcuts(data)
        self.assertEqual(encode_shortcuts(parsed), data)
        self.assertEqual(parsed.to_dict()["shortcuts"]["0"]["SteamFutureField"], "\udcff\udcfe")

    def test_empty_fixture_round_trips_without_creating_a_file_shape(self):
        parsed = parse_shortcuts(b"")
        self.assertEqual(parsed.records, [])
        self.assertEqual(encode_shortcuts(parsed), b"")
        self.assertNotEqual(encode_shortcuts(parsed, preserve_empty=False), b"")

    def test_mapping_codec_keeps_nested_tags(self):
        mapping = {"shortcuts": {"0": {"appid": 7, "AppName": "Foreign", "tags": ["Deck", "Tools"]}}}
        parsed = parse_shortcuts(encode_shortcuts(mapping))
        values = parsed.to_dict()["shortcuts"]["0"]
        self.assertEqual(values["appid"], 7)
        self.assertEqual(values["tags"], {"0": "Deck", "1": "Tools"})


class ShortcutIdentityTests(unittest.TestCase):
    def test_appid_is_crc32_with_the_non_steam_high_bit(self):
        exe = "/usr/bin/openbox"
        name = "A Game"
        expected = (zlib.crc32((exe + name).encode("utf-8")) & 0xFFFFFFFF) | OPENBOX_APPID_MASK
        self.assertEqual(appid_for(exe, name), expected)
        self.assertEqual(appid_for(exe, name), appid_for(exe, name))
        self.assertTrue(appid_for(exe, name) & OPENBOX_APPID_MASK)

    def test_shortcut_builder_and_game_projection(self):
        shortcut = make_shortcut("Game", "/usr/bin/openbox", launch_options="--play game-1")
        self.assertEqual(shortcut["appid"], appid_for("/usr/bin/openbox", "Game"))
        self.assertEqual(shortcut["LaunchOptions"], "--play game-1")
        projected = shortcut_from_game(
            {"game_id": "game-1", "name": "Game"},
            launcher_exe="/usr/bin/openbox",
        )
        self.assertEqual(projected["LaunchOptions"], "--play game-1")
        self.assertEqual(projected["Exe"], "/usr/bin/openbox")
        self.assertTrue(is_bridge_shortcut(projected))

    def test_high_bit_foreign_shortcut_is_not_owned_without_marker(self):
        foreign = make_shortcut(
            "Foreign", "/usr/bin/foreign", appid=0x80000001,
        )
        self.assertFalse(is_bridge_shortcut(foreign))


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "shortcuts.vdf"
        self.foreign = make_shortcut("Foreign", "/usr/bin/steam", appid=42, extra={"SteamFutureField": "keep"})
        self.existing = make_shortcut("Existing", "/usr/bin/openbox", launch_options="--play old", openbox_game_id="existing")
        self.path.write_bytes(encode_shortcuts({"shortcuts": {"0": self.foreign, "1": self.existing}}))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_preview_is_read_only_and_json_serializable(self):
        desired = make_shortcut("Existing", "/usr/bin/openbox", launch_options="--play new", openbox_game_id="existing")
        before = self.path.read_bytes()
        before_stat = self.path.stat()
        plan = preview_shortcuts(self.path, [desired])
        self.assertTrue(plan["preview"])
        self.assertEqual(plan["added"], 0)
        self.assertEqual(plan["updated"], 1)
        self.assertTrue(plan["changed"])
        json.dumps(plan)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, before_stat.st_mtime_ns)

    def test_apply_updates_only_target_and_preserves_foreign_fields(self):
        desired = make_shortcut("Existing", "/usr/bin/openbox", launch_options="--play new", openbox_game_id="existing")
        result = apply_shortcuts(self.path, [desired])
        self.assertTrue(result["written"])
        parsed = parse_shortcuts(self.path)
        self.assertEqual(len(parsed.records), 2)
        foreign = next(item for item in parsed.records if item.children[1].value == "Foreign")
        fields = {item.key: item for item in foreign.children}
        self.assertEqual(fields["SteamFutureField"].value, "keep")
        existing = next(item for item in parsed.records if item.children[1].value == "Existing")
        self.assertEqual(
            {item.key: item.value for item in existing.children}["LaunchOptions"],
            "--play new",
        )

    def test_low_bit_foreign_id_collision_is_added_without_overwrite(self):
        colliding = make_shortcut("Replacement", "/usr/bin/other", appid=42, openbox_game_id="replacement")
        result = apply_shortcuts(self.path, [colliding])
        self.assertEqual(result["added"], 1)
        parsed = parse_shortcuts(self.path)
        self.assertEqual(len(parsed.records), 3)
        foreign = next(item for item in parsed.records if item.children[1].value == "Foreign")
        self.assertEqual({item.key: item.value for item in foreign.children}["SteamFutureField"], "keep")

    def test_apply_plan_rejects_stale_file_without_writing(self):
        desired = make_shortcut("Existing", "/usr/bin/openbox", launch_options="--play new", openbox_game_id="existing")
        plan = preview_shortcuts(self.path, [desired])
        original = self.path.read_bytes()
        self.path.write_bytes(original + b"\x00")
        changed = self.path.read_bytes()
        with self.assertRaises(ShortcutsStaleError):
            apply_shortcuts(self.path, plan)
        self.assertEqual(self.path.read_bytes(), changed)

    def test_remove_changes_only_openbox_entry_and_never_unlinks_file(self):
        target = appid_for("/usr/bin/openbox", "Existing")
        before = self.path.read_bytes()
        preview = preview_remove(self.path, target)
        self.assertEqual(preview["removed"], 1)
        self.assertEqual(self.path.read_bytes(), before)
        result = remove_shortcuts(self.path, target)
        self.assertTrue(result["written"])
        self.assertTrue(self.path.exists())
        parsed = parse_shortcuts(self.path)
        self.assertEqual(len(parsed.records), 1)
        self.assertEqual(parsed.records[0].children[1].value, "Foreign")

    def test_remove_last_entry_writes_empty_root_instead_of_deleting_file(self):
        only = make_shortcut("Only", "/usr/bin/openbox", openbox_game_id="only")
        self.path.write_bytes(encode_shortcuts({"shortcuts": {"0": only}}))
        remove_shortcuts(self.path, only["appid"])
        self.assertTrue(self.path.is_file())
        self.assertEqual(parse_shortcuts(self.path).records, [])
        self.assertNotEqual(self.path.read_bytes(), b"")

    def test_noop_apply_does_not_touch_mtime(self):
        existing = parse_shortcuts(self.path)
        before = self.path.stat().st_mtime_ns
        result = apply_shortcuts(self.path, existing)
        self.assertFalse(result["written"])
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_serialized_remove_plan_recomputes_safe_removal(self):
        target = appid_for("/usr/bin/openbox", "Existing")
        plan = preview_remove(self.path, target)
        serialized = json.loads(json.dumps(plan))
        result = apply_shortcuts(self.path, serialized)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(len(parse_shortcuts(self.path).records), 1)

    def test_apply_can_create_a_missing_file_atomically(self):
        missing = Path(self.tempdir.name) / "nested" / "shortcuts.vdf"
        desired = make_shortcut("New", "/usr/bin/openbox", openbox_game_id="new")
        preview = preview_shortcuts(missing, [desired])
        self.assertFalse(preview["base_exists"])
        result = apply_shortcuts(missing, preview)
        self.assertTrue(result["written"])
        self.assertEqual(parse_shortcuts(missing).records[0].children[1].value, "New")

    def test_same_title_batch_keeps_distinct_openbox_identities(self):
        self.path.write_bytes(encode_shortcuts({"shortcuts": {}}))
        first = shortcut_from_game(
            {"game_id": "same-title-a", "name": "Twin", "path": "/games/a"},
            launcher_exe="/usr/bin/openbox",
        )
        second = shortcut_from_game(
            {"game_id": "same-title-b", "name": "Twin", "path": "/games/b"},
            launcher_exe="/usr/bin/openbox",
        )
        plan = preview_shortcuts(self.path, [first, second])
        desired = plan["desired_records"]
        appids = [record["value"] for record in desired[0]["value"] if record["key"] == "appid"]
        appids += [record["value"] for record in desired[1]["value"] if record["key"] == "appid"]
        self.assertEqual(len(appids), 2)
        self.assertEqual(len(set(appids)), 2)
        result = apply_shortcuts(self.path, plan=plan)
        self.assertEqual(result["added"], 2)
        parsed = parse_shortcuts(self.path)
        fields = [{child.key: child.value for child in record.children} for record in parsed.records]
        self.assertEqual({item["OpenBoxGameID"] for item in fields}, {"same-title-a", "same-title-b"})
        self.assertEqual({item["LaunchOptions"] for item in fields}, {"--play same-title-a", "--play same-title-b"})
        repeat = preview_shortcuts(self.path, [first, second])
        self.assertFalse(repeat["changed"])
        self.assertEqual(repeat["added"], 0)
        self.assertEqual(repeat["updated"], 0)
        repeat_appids = [
            next(child["value"] for child in record["value"] if child["key"] == "appid")
            for record in repeat["desired_records"]
        ]
        actual_appids = [
            next(child.value for child in record.children if child.key == "appid")
            for record in parsed.records
        ]
        self.assertEqual(repeat_appids, actual_appids)

    def test_same_title_sequential_exports_do_not_overwrite_first_game(self):
        self.path.write_bytes(encode_shortcuts({"shortcuts": {}}))
        first = shortcut_from_game(
            {"game_id": "sequential-a", "name": "Twin"},
            launcher_exe="/usr/bin/openbox",
        )
        second = shortcut_from_game(
            {"game_id": "sequential-b", "name": "Twin"},
            launcher_exe="/usr/bin/openbox",
        )
        apply_shortcuts(self.path, [first])
        result = apply_shortcuts(self.path, [second])
        self.assertEqual(result["added"], 1)
        fields = [{child.key: child.value for child in record.children} for record in parse_shortcuts(self.path).records]
        self.assertEqual({item["OpenBoxGameID"] for item in fields}, {"sequential-a", "sequential-b"})


class RejectionTests(unittest.TestCase):
    def test_corrupt_streams_are_rejected(self):
        valid = _document(_record(TYPE_STRING, "x", b"y"))
        corrupt = (
            valid[:-1],
            valid + b"\x00",
            b"\x01shortcuts\x00" + struct.pack("<I", 1),
            b"\x00wrong\x00\x08",
            b"\x00shortcuts\x00\x09",
            b"\x00shortcuts\x00\x02field\x00missing",
        )
        for payload in corrupt:
            with self.subTest(payload=payload):
                with self.assertRaises(ShortcutsCorruptError):
                    parse_shortcuts(payload)

    def test_giant_bytes_are_rejected_before_parsing(self):
        with self.assertRaises(ShortcutsTooLargeError):
            parse_shortcuts(b"x" * (MAX_SHORTCUTS_BYTES + 1))

    def test_malformed_file_is_not_replaced_by_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shortcuts.vdf"
            original = b"\x00shortcuts\x00\x09"
            path.write_bytes(original)
            with self.assertRaises(ShortcutsCorruptError):
                apply_shortcuts(path, [make_shortcut("Game", "/usr/bin/openbox", openbox_game_id="game")])
            self.assertEqual(path.read_bytes(), original)

    def test_symlink_destination_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real.vdf"
            link = root / "shortcuts.vdf"
            real.write_bytes(_document())
            try:
                link.symlink_to(real)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                apply_shortcuts(link, [make_shortcut("Game", "/usr/bin/openbox", openbox_game_id="game")])
            self.assertEqual(real.read_bytes(), _document())

    def test_low_level_writer_rejects_corrupt_payload_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shortcuts.vdf"
            original = _document(_record(TYPE_STRING, "foreign", b"keep"))
            path.write_bytes(original)
            with self.assertRaises(ShortcutsCorruptError):
                write_shortcuts_bytes(path, b"\x00shortcuts\x00\x09")
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
