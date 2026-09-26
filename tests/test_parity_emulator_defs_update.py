#!/usr/bin/env python3
"""Tests for the signed emulator-definition update channel (ADR 0060).

The 1.12.1 release shipped four signature-verification bugs at once, so these
tests are adversarial by design: the interesting cases are the ones that must
be *rejected*, not the one happy path.
"""

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402

from pkg.parity import parity_emulator_defs_update as upd  # noqa: E402

VALID_DEF = """emulator_id: testemu
adapter_id: testemu
label: Test Emulator
platform: SNES
extensions:
  - .tst
executable_patterns:
  - testemu
native_exe: testemu
native_exe_windows: testemu.exe
flatpak_app_id: org.test.TestEmulator
priority: 10
startup_args:
  - "{file}"
state:
  kind: adapter-cli
  template: "-statefile {state_path}"
  capture: ""
  glob: "*.state*"
schema_version: 1
recommended: false
"""


def make_tar(members):
    """Build a gzipped tar from {name: text_or_None}; None means a symlink."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, text in members.items():
            if text is None:
                info = tarfile.TarInfo(name)
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
                continue
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def fake_opener(mapping):
    """Return an opener serving {url_suffix: bytes}; unknown URLs raise."""

    def opener(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for suffix, payload in mapping.items():
            if url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return io.BytesIO(payload)
        raise OSError(f"unexpected URL: {url}")

    return opener


class DefinitionValidationTests(unittest.TestCase):
    def test_valid_definition_passes(self):
        upd.validate_definition("test.yaml", VALID_DEF)

    def test_missing_native_exe_windows_is_rejected(self):
        # The exact ADR 0048 regression: launches on Linux, fails on Windows.
        broken = "\n".join(
            line for line in VALID_DEF.splitlines() if not line.startswith("native_exe_windows")
        )
        with self.assertRaises(upd.DefinitionError) as ctx:
            upd.validate_definition("test.yaml", broken)
        self.assertIn("native_exe_windows", str(ctx.exception))

    def test_each_required_key_is_enforced(self):
        for key in upd.REQUIRED_KEYS:
            broken = "\n".join(
                line for line in VALID_DEF.splitlines() if not line.startswith(f"{key}:")
            )
            with self.assertRaises(upd.DefinitionError, msg=f"{key} was not enforced"):
                upd.validate_definition("test.yaml", broken)

    def test_oversized_definition_is_rejected(self):
        with self.assertRaises(upd.DefinitionError):
            upd.validate_definition("big.yaml", "x" * (upd.MAX_DEFINITION_BYTES + 1))

    def test_hashed_value_is_not_a_comment(self):
        keys = upd.definition_keys('label: "Emu #1"\nnative_exe: e\n')
        self.assertEqual({"label", "native_exe"}, keys)

    def test_indented_keys_are_not_top_level(self):
        keys = upd.definition_keys("emulator_id: a\nstartup_args:\n  - x\n")
        self.assertEqual({"emulator_id", "startup_args"}, keys)


class ReadPackTests(unittest.TestCase):
    def test_reads_definitions(self):
        defs = upd.read_pack(make_tar({"a.yaml": VALID_DEF, "b.yaml": VALID_DEF}))
        self.assertEqual({"a.yaml", "b.yaml"}, set(defs))

    def test_rejects_path_traversal(self):
        with self.assertRaises(upd.DefinitionError) as ctx:
            upd.read_pack(make_tar({"../escape.yaml": VALID_DEF}))
        self.assertIn("unsafe path", str(ctx.exception))

    def test_rejects_absolute_path(self):
        with self.assertRaises(upd.DefinitionError):
            upd.read_pack(make_tar({"/abs.yaml": VALID_DEF}))

    def test_symlink_members_are_ignored(self):
        # A symlink must never be materialized, so it is simply not read.
        defs = upd.read_pack(make_tar({"evil.yaml": None, "ok.yaml": VALID_DEF}))
        self.assertEqual({"ok.yaml"}, set(defs))

    def test_empty_pack_is_rejected(self):
        with self.assertRaises(upd.DefinitionError):
            upd.read_pack(make_tar({"notes.txt": "hello"}))

    def test_non_yaml_members_are_ignored(self):
        defs = upd.read_pack(make_tar({"a.yaml": VALID_DEF, "README.md": "hi"}))
        self.assertEqual({"a.yaml"}, set(defs))

    def test_basename_collision_is_rejected(self):
        members = {
            "defs/a.yaml": VALID_DEF,
            "other/a.yaml": VALID_DEF,
        }
        with self.assertRaises(upd.DefinitionError):
            upd.read_pack(make_tar(members))



class VerificationTests(unittest.TestCase):
    """Signature handling. A bad signature must never install anything."""

    def test_missing_release_key_is_a_signature_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(upd.SignatureError):
                upd.download_and_verify(
                    opener=fake_opener({".tar.gz": b"x", ".tar.gz.sig": b"{}"}),
                    pack_url="https://example.invalid/pack.tar.gz",
                    key_file=Path(directory) / "absent.pub",
                )

    def test_bad_signature_is_rejected(self):
        # A .sig whose digest does not match the artifact must fail closed.
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "k.pub"
            key.write_bytes(b"\x00" * 32)
            bad_sig = json.dumps(
                {"algorithm": "ed25519", "digest_algorithm": "sha256", "digest": "00" * 32}
            ).encode("utf-8")
            with self.assertRaises(upd.SignatureError):
                upd.download_and_verify(
                    opener=fake_opener({".tar.gz": b"payload", ".tar.gz.sig": bad_sig}),
                    pack_url="https://example.invalid/pack.tar.gz",
                    key_file=key,
                )

    def test_malformed_signature_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "k.pub"
            key.write_bytes(b"\x00" * 32)
            with self.assertRaises(upd.SignatureError):
                upd.download_and_verify(
                    opener=fake_opener({".tar.gz": b"payload", ".tar.gz.sig": b"not json"}),
                    pack_url="https://example.invalid/pack.tar.gz",
                    key_file=key,
                )

    def test_signature_error_is_a_definition_error(self):
        # The handler distinguishes the two, so the hierarchy must hold.
        self.assertTrue(issubclass(upd.SignatureError, upd.DefinitionError))


class InstallTests(unittest.TestCase):
    """Install behavior, exercised with verification stubbed out.

    Verification has its own class above; here the signature is bypassed so the
    install/merge/rollback logic can be driven deterministically offline.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _install(self, members, version="2.0.0"):
        """Install a pack with download+verify replaced by a fixed archive."""
        original = upd.download_and_verify
        upd.download_and_verify = lambda opener=None, **kw: make_tar(members)
        index = json.dumps({"version": version}).encode("utf-8")
        self.addCleanup(lambda: setattr(upd, "download_and_verify", original))
        return upd.install(opener=fake_opener({"index.json": index}), data_dir=self.data)

    def test_installs_definitions_into_the_data_dir(self):
        result = self._install({"new-emu.yaml": VALID_DEF})
        self.assertTrue(result["ok"])
        self.assertEqual(["new-emu.yaml"], result["installed"])
        self.assertTrue((self.data / "emulator_defs" / "new-emu.yaml").is_file())

    def test_bundled_definitions_are_untouched(self):
        self._install({"new-emu.yaml": VALID_DEF})
        # The shipped set must still be exactly what the gate validates.
        self.assertTrue((upd.BUNDLED_DEFS / "pcsx2-ps2.yaml").is_file())
        self.assertFalse((upd.BUNDLED_DEFS / "new-emu.yaml").exists())

    def test_local_edit_is_never_clobbered(self):
        local = self.data / "emulator_defs"
        local.mkdir(parents=True)
        mine = local / "new-emu.yaml"
        mine.write_text("# my hand-tuned version\n", encoding="utf-8")
        result = self._install({"new-emu.yaml": VALID_DEF})
        self.assertEqual(["new-emu.yaml"], result["kept_local"])
        self.assertEqual([], result["installed"])
        self.assertIn("hand-tuned", mine.read_text(encoding="utf-8"))

    def test_channel_owned_file_is_refreshed_by_a_later_pack(self):
        # Re-running install refreshes files the channel owns; only user-local
        # files are kept. Without this the channel could never update.
        self._install({"new-emu.yaml": VALID_DEF})
        newer = VALID_DEF.replace("label: Test Emulator", "label: Test Emulator v2")
        result = self._install({"new-emu.yaml": newer}, version="2.1.0")
        self.assertEqual(["new-emu.yaml"], result["updated"])
        self.assertEqual([], result["installed"])
        text = (self.data / "emulator_defs" / "new-emu.yaml").read_text(encoding="utf-8")
        self.assertIn("v2", text)

    def test_install_persists_rollback_ledger(self):
        self._install({"new-emu.yaml": VALID_DEF})
        result = upd.status(self.data)
        self.assertEqual(["new-emu.yaml"], result["installed"])
        self.assertEqual("2.0.0", result["version"])

    def test_partial_pack_is_not_applied(self):
        # One bad definition in the pack means nothing is installed at all.
        broken = "\n".join(
            line for line in VALID_DEF.splitlines() if not line.startswith("native_exe_windows")
        )
        with self.assertRaises(upd.DefinitionError):
            self._install({"good.yaml": VALID_DEF, "bad.yaml": broken})
        self.assertFalse((self.data / "emulator_defs" / "good.yaml").exists())

    def test_verification_failure_installs_nothing(self):
        def boom(**_kwargs):
            raise upd.SignatureError("nope")

        original = upd.download_and_verify
        upd.download_and_verify = boom
        self.addCleanup(lambda: setattr(upd, "download_and_verify", original))
        with self.assertRaises(upd.SignatureError):
            upd.install(data_dir=self.data)
        self.assertFalse((self.data / "emulator_defs").exists())

    def test_rollback_removes_only_installed_files(self):
        self._install({"new-emu.yaml": VALID_DEF})
        local = self.data / "emulator_defs"
        mine = local / "mine.yaml"
        mine.write_text(VALID_DEF, encoding="utf-8")
        result = upd.rollback(self.data)
        self.assertEqual(["new-emu.yaml"], result["removed"])
        self.assertFalse((local / "new-emu.yaml").exists())
        self.assertTrue(mine.is_file(), "rollback must not delete a user's own file")


class StatusTests(unittest.TestCase):
    def test_status_reports_bundled_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            result = upd.status(Path(directory))
            self.assertTrue(result["ok"])
            self.assertIn("pcsx2-ps2.yaml", result["bundled_definitions"])
            self.assertEqual([], result["local_definitions"])

    def test_status_flags_locally_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "emulator_defs"
            local.mkdir(parents=True)
            (local / "pcsx2-ps2.yaml").write_text(VALID_DEF, encoding="utf-8")
            result = upd.status(Path(directory))
            self.assertIn("pcsx2-ps2.yaml", result["locally_modified"])


class CheckTests(unittest.TestCase):
    def test_non_object_index_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = upd.check(
                opener=fake_opener({"index.json": b'["not", "an", "object"]'}),
                data_dir=Path(directory),
            )
            self.assertFalse(result["ok"])
            self.assertIn("not an object", result["error"])

    def test_unreachable_index_reports_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = upd.check(opener=fake_opener({}), data_dir=Path(directory))
            self.assertFalse(result["ok"])

    def test_newer_version_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = json.dumps({"version": "3.0.0", "notes": "hi"}).encode("utf-8")
            result = upd.check(
                opener=fake_opener({"index.json": payload}), data_dir=Path(directory)
            )
            self.assertTrue(result["ok"])
            self.assertTrue(result["update_available"])
            self.assertEqual("3.0.0", result["version"])


class LoaderPrecedenceTests(unittest.TestCase):
    """The data directory must shadow the bundled set, without deleting it."""

    def test_data_dir_definition_shadows_bundled(self):
        from pkg.parity import parity_emulator_defs as defs

        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "emulator_defs"
            local.mkdir(parents=True)
            # Same filename as a bundled definition, so the local file must win.
            (local / "pcsx2-ps2.yaml").write_text(VALID_DEF, encoding="utf-8")
            previous = os.environ.get("OPENBOX_DATA_DIR")
            os.environ["OPENBOX_DATA_DIR"] = directory
            try:
                defs._reset_registry_cache()
                adapters = defs.load_adapters()
                ids = {a["adapter_id"] for a in adapters}
                # The local file replaced the bundled one entirely: its
                # adapter_id is now testemu, and pcsx2-ps2 is gone.
                self.assertIn("testemu", ids)
                self.assertNotIn("pcsx2-ps2", ids)
                # Everything else still comes from the bundled set.
                self.assertIn("retroarch-nes", ids)
            finally:
                if previous is None:
                    os.environ.pop("OPENBOX_DATA_DIR", None)
                else:
                    os.environ["OPENBOX_DATA_DIR"] = previous
                defs._reset_registry_cache()

    def test_local_copy_of_a_bundled_name_keeps_its_identity(self):
        """A local override normally keeps the real adapter_id."""
        from pkg.parity import parity_emulator_defs as defs

        bundled_text = (upd.BUNDLED_DEFS / "pcsx2-ps2.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "emulator_defs"
            local.mkdir(parents=True)
            (local / "pcsx2-ps2.yaml").write_text(
                bundled_text.replace("label: PCSX2", "label: My Local PCSX2"), encoding="utf-8"
            )
            previous = os.environ.get("OPENBOX_DATA_DIR")
            os.environ["OPENBOX_DATA_DIR"] = directory
            try:
                defs._reset_registry_cache()
                labels = {a["adapter_id"]: a["label"] for a in defs.load_adapters()}
                self.assertEqual("My Local PCSX2", labels.get("pcsx2-ps2"))
            finally:
                if previous is None:
                    os.environ.pop("OPENBOX_DATA_DIR", None)
                else:
                    os.environ["OPENBOX_DATA_DIR"] = previous
                defs._reset_registry_cache()

    def test_bundled_only_load_is_unchanged(self):
        from pkg.parity import parity_emulator_defs as defs

        adapters = defs.load_adapters(str(upd.BUNDLED_DEFS))
        self.assertGreaterEqual(len(adapters), 24)


class RouteWiringTests(unittest.TestCase):
    def test_routes_are_registered(self):
        from routes.registry import all_routes

        paths = {r.path for r in all_routes()}
        for path in (
            "/api/v2/emulators/defs/status",
            "/api/v2/emulators/defs/update",
            "/api/v2/emulators/defs/rollback",
        ):
            self.assertIn(path, paths)

    def test_v1_surface_is_untouched(self):
        contract = json.loads((ROOT / "v1_contracts.json").read_text(encoding="utf-8"))
        for entry in contract["routes"]:
            self.assertNotIn("/api/v2/", entry["path"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
