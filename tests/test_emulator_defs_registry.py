#!/usr/bin/env python3
"""P1-24: emulator def registry invalidation and malformed-def reporting."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity import parity_emulator_defs as defs


GOOD_DEF = (
    "schema_version: 1\n"
    "adapter_id: {adapter_id}\n"
    "emulator_id: {emulator_id}\n"
    "label: {label}\n"
    "platform: {platform}\n"
    "extensions:\n"
    "  - {extension}\n"
    "startup_args:\n"
    '  - "{{path}}"\n'
)


class RegistryReloadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.original_defs_dir = defs.DEFS_DIR
        defs.DEFS_DIR = self.root
        self.addCleanup(self._restore_defs_dir)

    def _restore_defs_dir(self):
        defs.DEFS_DIR = self.original_defs_dir
        defs.reload_defs()

    def _write(self, name, **values):
        (self.root / name).write_text(GOOD_DEF.format(**values), encoding="utf-8")

    def test_reload_defs_refreshes_registry_and_module_maps(self):
        self._write(
            "one.yaml",
            adapter_id="test-one",
            emulator_id="emu.one",
            label="One",
            platform="OnePlat",
            extension="one",
        )
        registry = defs.reload_defs()
        self.assertEqual([item["adapter_id"] for item in registry["adapters"]], ["test-one"])
        self.assertIn("emu.one", defs.EMULATORS)
        self.assertEqual(defs.PLATFORM_EMULATORS["OnePlat"], [("emu.one", "One")])

        emulators = defs.EMULATORS
        platform_emulators = defs.PLATFORM_EMULATORS
        self._write(
            "one.yaml",
            adapter_id="test-one",
            emulator_id="emu.one",
            label="One Updated",
            platform="OnePlat",
            extension="one",
        )
        # The registry fingerprint notices the rewrite without an explicit call.
        labels = {item["label"] for item in defs._registry()["adapters"]}
        self.assertEqual(labels, {"One Updated"})

        defs.reload_defs()
        self.assertIs(defs.EMULATORS, emulators)
        self.assertIs(defs.PLATFORM_EMULATORS, platform_emulators)
        self.assertEqual(defs.EMULATORS["emu.one"]["name"], "One Updated")

    def test_malformed_defs_are_reported_not_silently_dropped(self):
        self._write(
            "good.yaml",
            adapter_id="test-good",
            emulator_id="emu.good",
            label="Good",
            platform="GoodPlat",
            extension="good",
        )
        (self.root / "broken.yaml").write_text("adapter_id: [unclosed\n", encoding="utf-8")
        (self.root / "invalid.yaml").write_text("platform: only\n", encoding="utf-8")

        registry = defs.load_registry(self.root)
        self.assertEqual([item["adapter_id"] for item in registry["adapters"]], ["test-good"])
        self.assertEqual({item["file"] for item in registry["errors"]}, {"broken.yaml", "invalid.yaml"})
        for item in registry["errors"]:
            self.assertTrue(item["error"])

    def test_unreadable_and_non_mapping_defs_are_reported(self):
        (self.root / "binary.yaml").write_bytes(b"\xff\xfe\x00adapter_id")
        (self.root / "list.yaml").write_text("- just\n- a list\n", encoding="utf-8")
        errors = []
        adapters = defs.load_adapters(self.root, errors=errors)
        self.assertEqual(adapters, [])
        self.assertEqual({item["file"] for item in errors}, {"binary.yaml", "list.yaml"})

    def test_non_mapping_payload_reported(self):
        (self.root / "list.yaml").write_text("not: a mapping\n", encoding="utf-8")
        with mock.patch.object(defs, "_parse_yaml", return_value=["not", "a", "mapping"]):
            registry = defs.load_registry(self.root)
        self.assertEqual(registry["adapters"], [])
        self.assertEqual([item["file"] for item in registry["errors"]], ["list.yaml"])

    def test_definition_errors_follow_reload(self):
        (self.root / "invalid.yaml").write_text("platform: only\n", encoding="utf-8")
        registry = defs.reload_defs()
        self.assertEqual([item["file"] for item in registry["errors"]], ["invalid.yaml"])
        errors = defs.definition_errors()
        self.assertEqual([item["file"] for item in errors], ["invalid.yaml"])
        self.assertIn("adapter_id", errors[0]["error"])
        # The cached registry path reports the same errors when no defs_dir is given.
        self.assertEqual([item["file"] for item in defs.load_registry()["errors"]], ["invalid.yaml"])

    def test_missing_defs_dir_is_tolerated(self):
        defs.DEFS_DIR = self.root / "missing"
        registry = defs.reload_defs()
        self.assertEqual(registry["adapters"], [])
        self.assertEqual(registry["errors"], [])


class DefsChannelTests(unittest.TestCase):
    """F13: signed, versioned emulator definition update channel."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.original_defs_dir = defs.DEFS_DIR
        defs.DEFS_DIR = self.root / "defs"
        self.addCleanup(self._restore)

    def _restore(self):
        defs.DEFS_DIR = self.original_defs_dir
        defs.reload_defs()

    def _manifest_bytes(self):
        import hashlib as _hashlib
        import json as _json

        body = GOOD_DEF.format(
            adapter_id="channel-one",
            emulator_id="emu.channel",
            label="Channel",
            platform="ChannelPlat",
            extension="chd",
        )
        files = {"channel-one.yaml": _hashlib.sha256(body.encode()).hexdigest()}
        payload = {"format": 1, "version": "2026.09.1", "files": files, "yaml": {"channel-one.yaml": body}}
        return _json.dumps(payload).encode()

    def _signed_opener(self, raw, *, tamper=False):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.generate()
        digest = __import__("hashlib").sha256(raw).hexdigest()
        signature = key.sign(bytes.fromhex(digest))
        public = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
        )
        import base64 as _base64
        import json as _json

        if tamper:
            flipped = bytearray(signature)
            flipped[0] ^= 0x01
            signature = bytes(flipped)
        sig_payload = _json.dumps({
            "algorithm": "ed25519", "digest_algorithm": "sha256",
            "digest": digest, "signature": _base64.b64encode(signature).decode(),
        }).encode()

        class Response:
            def __init__(self, body):
                self._body = body
                self.headers = {"Content-Length": str(len(body))}
                self._offset = 0

            def read(self, size=-1):
                if size is None or size < 0:
                    size = len(self._body) - self._offset
                chunk = self._body[self._offset:self._offset + size]
                self._offset += len(chunk)
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(url, timeout=30):
            target = getattr(url, "full_url", url)
            return Response(sig_payload if "sig" in target else raw)

        return opener, public

    def test_validate_manifest_rejects_unsafe_shapes(self):
        with self.assertRaises(ValueError):
            defs.validate_defs_manifest(["not", "an", "object"])
        with self.assertRaises(ValueError):
            defs.validate_defs_manifest({"format": 2, "version": "2026.09.1", "files": {}, "yaml": {}})
        with self.assertRaises(ValueError):
            defs.validate_defs_manifest({"format": 1, "version": "bad", "files": {"a.yaml": "0" * 64}, "yaml": {"a.yaml": "x: 1"}})
        with self.assertRaises(ValueError):
            defs.validate_defs_manifest({
                "format": 1, "version": "2026.09.1",
                "files": {"../evil.yaml": "0" * 64}, "yaml": {"../evil.yaml": "x: 1"},
            })
        with self.assertRaises(ValueError):
            defs.validate_defs_manifest({
                "format": 1, "version": "2026.09.1",
                "files": {"a.yaml": "0" * 64}, "yaml": {"a.yaml": "x: 1"},
            })

    def test_signed_manifest_applies_and_reloads(self):
        raw = self._manifest_bytes()
        opener, public = self._signed_opener(raw)
        result = defs.fetch_defs_channel(
            "https://example.invalid/defs.json",
            "https://example.invalid/defs.json.sig",
            defs_dir=defs.DEFS_DIR, opener=opener, public_key=public,
        )
        self.assertEqual(result["version"], "2026.09.1")
        self.assertEqual(result["applied"], ["channel-one.yaml"])
        self.assertIn("channel-one", {item["adapter_id"] for item in defs._registry()["adapters"]})
        # A second apply keeps the previous file as a .bak.
        result = defs.fetch_defs_channel(
            "https://example.invalid/defs.json",
            "https://example.invalid/defs.json.sig",
            defs_dir=defs.DEFS_DIR, opener=opener, public_key=public,
        )
        self.assertTrue(list(defs.DEFS_DIR.glob("*.yaml.bak")))

    def test_bad_signature_and_http_are_rejected_before_writing(self):
        raw = self._manifest_bytes()
        opener, public = self._signed_opener(raw, tamper=True)
        with self.assertRaises(ValueError):
            defs.fetch_defs_channel(
                "https://example.invalid/defs.json",
                "https://example.invalid/defs.json.sig",
                defs_dir=defs.DEFS_DIR, opener=opener, public_key=public,
            )
        self.assertFalse(defs.DEFS_DIR.exists() and list(defs.DEFS_DIR.glob("*.yaml")))
        opener, public = self._signed_opener(raw)
        with self.assertRaises(ValueError):
            defs.fetch_defs_channel(
                "http://insecure.invalid/defs.json",
                "https://example.invalid/defs.json.sig",
                defs_dir=defs.DEFS_DIR, opener=opener, public_key=public,
            )

    def test_channel_status_reports_counts(self):
        status = defs.defs_channel_status(defs.DEFS_DIR)
        self.assertEqual(status["adapters"], 0)
        self.assertIn("defs_dir", status)


if __name__ == "__main__":
    unittest.main()
