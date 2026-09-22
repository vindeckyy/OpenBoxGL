"""Gapfill coverage for the residual branches left by test_gapfill_f2.py.

Standalone unittest suite (``python3 -B tests/test_gapfill_rest.py``):
isolated OPENBOX_DATA_DIR, no network, dependency-free. Every test executes
real production code; mocks are only used to inject failure at the boundary
(e.g. an OSError from the filesystem, a stubbed timer, or a cancelled job).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="openbox-gapfill-rest-")
os.environ["OPENBOX_DATA_DIR"] = _DATA_DIR

import pkg.parity  # noqa: F401,E402  # register flat-import finder first
import plugins  # noqa: E402
import catalog  # noqa: E402
import openbox  # noqa: E402
import web_app  # noqa: E402
import handlers.discovery as discovery  # noqa: E402
import handlers.extensions  # noqa: E402
import handlers.library as library_mod  # noqa: E402
import handlers.library_health as library_health  # noqa: E402
import handlers.steambridge as steambridge  # noqa: E402
from api_errors import BadRequest  # noqa: E402
from pkg.parity import parity_artwork_hygiene as artwork_hygiene  # noqa: E402
from pkg.parity import parity_dna as dna  # noqa: E402
from pkg.parity import parity_duplicates as duplicates  # noqa: E402
from pkg.parity import parity_library_health as health_engine  # noqa: E402
from pkg.parity import parity_query as parity_query  # noqa: E402
from pkg.parity import parity_repair as repair  # noqa: E402

MAX_SEED = {
    "active_sessions": [],
    "games": [],
    "profiles": {},
    "settings": {},
    "trash": [],
}


# ── helpers ──────────────────────────────────────────────────────────────────

def make_plugin_package(root: Path, plugin_id: str, manifest_extra=None, entry_code: str = "print('ok')") -> Path:
    package = root / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": plugin_id,
        "name": plugin_id.title(),
        "version": "1.0.0",
        "entry": "plugin.py",
        "hooks": [],
    }
    manifest.update(manifest_extra or {})
    (package / "plugin.json").write_text(json.dumps(manifest))
    (package / "plugin.py").write_text(entry_code)
    return package


def seed_state(**overrides):
    state = dict(MAX_SEED)
    state.update(overrides)
    openbox.save_state(state)
    return openbox.load_state_readonly()


class HttpTestBase(unittest.TestCase):
    """One real ThreadingHTTPServer per class, talking to web_app.Handler."""

    TOKEN = "gapfill-rest-token"

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        cls.port = cls.server.server_address[1]
        web_app.TOKEN = cls.TOKEN
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)

    def http(self, path, body=..., method=None):
        data = None
        headers = {"X-OpenBox-Token": self.TOKEN}
        if body is not ...:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method or ("POST" if data is not None else "GET"),
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")
        return 200, json.loads(raw or b"null")


# ── plugins.py ───────────────────────────────────────────────────────────────

class PluginValidationTests(unittest.TestCase):
    def test_sandbox_status(self):
        with mock.patch.dict(os.environ, {"OPENBOX_ALLOW_UNSANDBOXED_PLUGINS": "1"}):
            self.assertEqual(plugins.sandbox_status(), "disabled")
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "OPENBOX_ALLOW_UNSANDBOXED_PLUGINS"
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(plugins.shutil, "which", return_value=None):
                self.assertEqual(plugins.sandbox_status(), "unavailable")
            with mock.patch.object(plugins.shutil, "which", return_value="/usr/bin/bwrap"), \
                 mock.patch.object(plugins, "_sandbox_available", return_value=True):
                self.assertEqual(plugins.sandbox_status(), "ready")
            with mock.patch.object(plugins.shutil, "which", return_value="/usr/bin/bwrap"), \
                 mock.patch.object(plugins, "_sandbox_available", side_effect=OSError("boom")):
                self.assertEqual(plugins.sandbox_status(), "unavailable")

    def test_clean_commands(self):
        with self.assertRaises(ValueError):
            plugins._clean_commands("not-a-list", "p")
        with self.assertRaises(ValueError):
            plugins._clean_commands([123], "p")
        with self.assertRaises(ValueError):
            plugins._clean_commands([{"id": "bad id!", "label": "L"}], "p")
        with self.assertRaises(ValueError):
            plugins._clean_commands(
                [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}], "p"
            )
        commands = plugins._clean_commands([{"id": "do-it", "label": "Do it"}], "p")
        self.assertEqual(commands[0]["id"], "do-it")

    def test_clean_permissions(self):
        with self.assertRaises(ValueError):
            plugins._clean_permissions([123])
        with self.assertRaises(ValueError):
            plugins._clean_permissions(["bogus"])
        self.assertEqual(plugins._clean_permissions(["network"]), ["network"])

    def test_clean_settings_schema(self):
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema("nope")
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema({"type": "array"})
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema({"properties": []})
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema({"properties": {"p": "x"}})
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema({"properties": {"p": {"type": "int"}}})
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema(
                {"properties": {"p": {"type": "string", "enum": 5}}}
            )
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema(
                {"properties": {"p": {"type": "string"}}, "required": "p"}
            )
        with self.assertRaises(ValueError):
            plugins._clean_settings_schema(
                {"properties": {"p": {"type": "string"}}, "required": ["missing"]}
            )
        schema = plugins._clean_settings_schema(
            {"properties": {"p": {"type": "string"}}, "required": ["p"]}
        )
        self.assertEqual(schema["required"], ["p"])

    def test_check_setting_value_number(self):
        with self.assertRaises(ValueError):
            plugins._check_setting_value("p", {"type": "number"}, True)
        with self.assertRaises(ValueError):
            plugins._check_setting_value("p", {"type": "number"}, "abc")
        with self.assertRaises(ValueError):
            plugins._check_setting_value("p", {"type": "number", "minimum": 1}, 0)
        with self.assertRaises(ValueError):
            plugins._check_setting_value("p", {"type": "number", "maximum": 5}, 9)
        self.assertEqual(plugins._check_setting_value("p", {"type": "number"}, 3), 3)

    def test_validate_plugin_settings(self):
        schema = {
            "type": "object",
            "properties": {"level": {"type": "integer"}},
            "required": ["level"],
        }
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, "nope")
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, {})
        cleaned = plugins.validate_plugin_settings(schema, {"level": 4})
        self.assertEqual(cleaned, {"level": 4})
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, {"level": "4", "extra": True})
        optional = {"type": "object", "properties": {"nick": {"type": "string"}}}
        self.assertEqual(plugins.validate_plugin_settings(optional, {}), {})

    def test_validate_plugin_settings_string_bounds(self):
        schema = {
            "type": "object",
            "properties": {
                "nick": {"type": "string", "minLength": 2, "maxLength": 4}
            },
            "required": ["nick"],
        }
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, {"nick": 123})
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, {"nick": "x"})
        with self.assertRaises(ValueError):
            plugins.validate_plugin_settings(schema, {"nick": "toolong"})
        cleaned = plugins.validate_plugin_settings(schema, {"nick": "ok"})
        self.assertEqual(cleaned, {"nick": "ok"})

    def test_trust_status_checksum_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(plugins, "plugin_checksum", side_effect=OSError("boom")):
                status = plugins.plugin_trust_status(tmp, "missing")
        self.assertFalse(status["trusted"])
        self.assertFalse(status["checksum_matches"])

    def test_set_plugin_permissions_undeclared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_plugin_package(root, "permplug")
            with self.assertRaises(ValueError):
                plugins.set_plugin_permissions(root, "permplug", ["network"])

    def test_plugin_settings_values_non_dict_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "id": "setplug",
                "settings": {"properties": {"mode": {"type": "string"}}},
            }
            plugins.save_plugin_state(root, {"settings": {"setplug": "not-a-dict"}})
            self.assertEqual(plugins.plugin_settings_values(root, manifest), {})

    def test_emit_plugin_event_safe_mode_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"OPENBOX_SAFE_MODE": "1"}):
                plugins.emit_plugin_event(tmp, "library_snapshot")
            with mock.patch.object(plugins, "run_plugins", side_effect=RuntimeError("boom")):
                plugins.emit_plugin_event(tmp, "library_snapshot", {"games": []})

    def test_snapshot_library_edge_cases(self):
        self.assertEqual(plugins.snapshot_library({"games": ["nope", {"name": "x"}]}), {})
        snapshot = plugins.snapshot_library({"games": [{"game_id": "g1", "name": "Doom"}]})
        self.assertIn("g1", snapshot)

    def test_emit_library_diff_update_cap(self):
        games = [{"game_id": f"g{i}", "name": f"Game {i}"} for i in range(105)]
        before = plugins.snapshot_library({"games": games})
        changed = [
            {"game_id": f"g{i}", "name": f"Renamed {i}"} for i in range(105)
        ]
        after = plugins.snapshot_library({"games": changed})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(plugins, "run_plugins") as run:
                plugins.emit_library_diff(tmp, before, after)
        self.assertTrue(run.called)

    def test_emit_library_diff_no_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(plugins, "run_plugins") as run:
                plugins.emit_library_diff(tmp, None, {"g1": ()})
        run.assert_not_called()


class PluginMergeTests(unittest.TestCase):
    def _plug(self, valid=True, hooks=("library_source",)):
        return {
            "id": "p1",
            "valid": valid,
            "enabled": True,
            "hooks": list(hooks),
            "name": "P1",
        }

    def test_merge_skips_invalid_plugins(self):
        games = [{"game_id": "g1", "name": "Seed"}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug(valid=False)]
            ):
                self.assertEqual(
                    plugins.merge_library_source_games(games, tmp), games
                )

    def test_merge_skips_plugins_without_hook(self):
        games = [{"game_id": "g1", "name": "Seed"}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug(hooks=[])]
            ):
                self.assertEqual(
                    plugins.merge_library_source_games(games, tmp), games
                )

    def test_merge_hook_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug()]
            ), mock.patch.object(
                plugins, "run_plugin_hook", return_value=(None, "boom")
            ):
                merged = plugins.merge_library_source_games(
                    [{"game_id": "g1", "name": "Seed"}], tmp
                )
        self.assertEqual(len(merged), 1)

    def test_merge_hook_non_dict_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug()]
            ), mock.patch.object(
                plugins, "run_plugin_hook", return_value=(["x"], None)
            ):
                merged = plugins.merge_library_source_games(
                    [{"game_id": "g1", "name": "Seed"}], tmp
                )
        self.assertEqual(len(merged), 1)

    def test_merge_hook_non_list_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug()]
            ), mock.patch.object(
                plugins, "run_plugin_hook", return_value=({"games": "nope"}, None)
            ):
                merged = plugins.merge_library_source_games(
                    [{"game_id": "g1", "name": "Seed"}], tmp
                )
        self.assertEqual(len(merged), 1)

    def test_merge_hook_invalid_entries(self):
        entries = [
            {"id": "a", "name": "From Plugin"},
            "not-a-dict",
            {"name": ""},
            {"id": "bad", "progress": "bogus-progress"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                plugins, "list_plugins", return_value=[self._plug()]
            ), mock.patch.object(
                plugins, "run_plugin_hook", return_value=({"games": entries}, None)
            ):
                merged = plugins.merge_library_source_games(
                    [{"game_id": "g1", "name": "Seed"}], tmp
                )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[1]["game_id"], "plugin:p1:a")

    def test_read_manifest_rejects_bad_api_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_plugin_package(root, "apiverplug", {"api_version": 0})
            with self.assertRaises(ValueError):
                plugins.read_manifest(root / "apiverplug")

    def test_run_manifest_hook_payload_too_large(self):
        manifest = {"id": "hookplug", "entry": "plugin.py"}
        payload = {"data": "x" * (plugins.MAX_PLUGIN_PAYLOAD + 8)}
        with tempfile.TemporaryDirectory() as tmp:
            result, error = plugins._run_manifest_hook(
                Path(tmp), manifest, "library", payload
            )
        self.assertIsNone(result)
        self.assertIn("too large", error)

    def _run_with_stdout(self, payload_bytes):
        manifest = {"id": "hookplug", "entry": "plugin.py"}
        payload = {"ping": True}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_plugin_package(root, "hookplug")

            def fake_run(command, **kwargs):
                kwargs["stdout"].write(payload_bytes)
                return SimpleNamespace(returncode=0)

            with mock.patch.object(
                plugins, "_plugin_command", return_value=["true"]
            ), mock.patch.object(plugins.subprocess, "run", side_effect=fake_run):
                return plugins._run_manifest_hook(root, manifest, "library", payload)

    def test_run_manifest_hook_output_too_large(self):
        result, error = self._run_with_stdout(b"x" * (plugins.MAX_PLUGIN_PAYLOAD + 16))
        self.assertIsNone(result)
        self.assertIn("exceeded", error)

    def test_run_manifest_hook_empty_output(self):
        result, error = self._run_with_stdout(b"   \n")
        self.assertIsNone(result)
        self.assertEqual(error, "")

    def test_run_manifest_hook_non_dict_output(self):
        result, error = self._run_with_stdout(b"[1, 2]")
        self.assertIsNone(result)
        self.assertIn("JSON object", error)


# ── pkg/parity/parity_library_health.py ──────────────────────────────────────

class LibraryHealthEngineTests(unittest.TestCase):
    def _block(self, *names):
        return mock.patch.dict("sys.modules", {name: None for name in names})

    def test_default_probe_import_fallback(self):
        with self._block("pkg.state.media_probe"):
            self.assertFalse(health_engine._default_probe(""))
            self.assertFalse(health_engine._default_probe("/tmp/does-not-exist-xyz"))
            with tempfile.NamedTemporaryFile() as handle:
                self.assertTrue(health_engine._default_probe(handle.name))
                self.assertTrue(
                    health_engine._default_probe(handle.name, file_only=True)
                )
            with mock.patch("os.path.exists", side_effect=OSError("boom")):
                self.assertFalse(health_engine._default_probe("/tmp/x"))

    def test_identity_helpers_fallbacks(self):
        with self._block("pkg.parity.parity_identity"):
            self.assertIsNone(health_engine._identity_helpers())
        with self._block("pkg.state.imports"):
            helpers = health_engine._identity_helpers()
            self.assertIsNotNone(helpers)
            self.assertEqual(
                helpers["legacy_identity"]({"path": "/x"}), ("path", str(Path("/x")))
            )

    def test_media_types_fallback(self):
        with self._block("pkg.state.media_probe"):
            self.assertEqual(health_engine._media_types(), [])

    def test_detect_for_game_legacy_fallback(self):
        game = {
            "game_id": "g1",
            "name": "Doom",
            "path": "/tmp/does-not-exist-xyz",
            "manual_entry": True,
        }
        ctx = {
            "probe": lambda path, file_only=False: True,
            "seen": {},
            "helpers": None,
            "state": {},
            "games": [game],
        }
        with self._block("pkg.state.imports"):
            issues = health_engine._detect_for_game(game, 0, ctx)
        self.assertEqual(issues, [])

    def test_detect_for_game_extras_and_saves(self):
        game = {
            "game_id": "g1",
            "name": "Doom",
            "path": "/tmp/does-not-exist-xyz",
            "applications": [{"path": "/tmp/nope-app"}],
            "save_paths": ["/tmp/nope-save"],
        }
        ctx = {
            "probe": lambda path, file_only=False: False,
            "seen": {},
            "helpers": None,
            "state": {},
            "games": [game],
        }
        with self._block("pkg.state.imports"):
            issues = health_engine._detect_for_game(game, 0, ctx)
        codes = {issue["code"] for issue in issues}
        self.assertIn("missing_extra", codes)
        self.assertIn("missing_save_path", codes)

    def test_detect_issues_duplicate_detector_exception(self):
        real = health_engine._identity_helpers()
        helpers = {
            **real,
            "detect_duplicate_identities": mock.Mock(
                side_effect=RuntimeError("boom")
            ),
        }
        game = {"game_id": "g1", "name": "Doom", "manual_entry": True}
        with mock.patch.object(
            health_engine, "_identity_helpers", return_value=helpers
        ):
            issues = health_engine.detect_issues([game, "nope"], {})
        self.assertEqual(issues, [])

    def test_merge_incremental(self):
        game = {"game_id": "g1", "name": "Doom", "manual_entry": True}
        merged = health_engine.merge_incremental({}, [game], {}, [])
        self.assertFalse(merged["dirty"])
        merged = health_engine.merge_incremental({}, [game], {}, ["ghost"])
        self.assertFalse(merged["dirty"])
        real = health_engine._identity_helpers()
        helpers = {
            **real,
            "detect_duplicate_identities": mock.Mock(
                side_effect=RuntimeError("boom")
            ),
        }
        with mock.patch.object(
            health_engine, "_identity_helpers", return_value=helpers
        ):
            merged = health_engine.merge_incremental({}, [game], {}, ["g1"])
        self.assertFalse(merged["dirty"])

    def test_mark_and_take_dirty_ids(self):
        state = {}
        health_engine.mark_dirty_ids(state, ["g1", "g2"])
        self.assertEqual(health_engine.take_dirty_ids(state), ["g1", "g2"])
        self.assertEqual(health_engine.take_dirty_ids(state), [])
        state = {"health_cache": {"dirty_ids": "nope"}}
        health_engine.mark_dirty_ids(state, ["g3"])
        self.assertEqual(health_engine.take_dirty_ids(state), ["g3"])
        self.assertEqual(health_engine.take_dirty_ids({"health_cache": "nope"}), [])

    def test_rescan_due_edge_cases(self):
        self.assertFalse(health_engine.rescan_due({"health_rescan": "off"}))
        self.assertFalse(health_engine.rescan_due({"health_rescan": "on_startup"}))
        self.assertTrue(health_engine.rescan_due({"health_rescan": "daily"}))
        self.assertTrue(
            health_engine.rescan_due(
                {"health_rescan": "daily", "last_health_rescan": "not-a-date"}
            )
        )
        self.assertFalse(
            health_engine.rescan_due(
                {
                    "health_rescan": "daily",
                    "last_health_rescan": "2030-01-01T00:00:00",
                }
            )
        )

    def test_record_fix_repairs_non_list_journal(self):
        state = {"health_fixes": "not-a-list"}
        record = health_engine.record_fix(state, {"fix_id": "f1"})
        self.assertTrue(record["fix_id"] == "f1")
        self.assertIsInstance(state["health_fixes"], list)
        self.assertEqual(len(state["health_fixes"]), 1)

    def test_mark_fix_undone_non_list_journal(self):
        self.assertFalse(health_engine.mark_fix_undone({"health_fixes": "x"}, "f1"))
        self.assertFalse(health_engine.mark_fix_undone({}, "f1"))
        self.assertFalse(health_engine.mark_fix_undone({"health_fixes": []}, "f1"))
        state = {"health_fixes": [{"fix_id": "f1", "undone": False}]}
        self.assertTrue(health_engine.mark_fix_undone(state, "f1"))
        self.assertTrue(state["health_fixes"][0]["undone"])


# ── pkg/parity/parity_repair.py ──────────────────────────────────────────────

class RepairEngineTests(unittest.TestCase):
    def test_resolve_folder(self):
        with self.assertRaises(ValueError):
            repair.resolve_folder("")
        with self.assertRaises(ValueError):
            repair.resolve_folder("/" + "x" * 5000)
        with self.assertRaises(ValueError):
            repair.resolve_folder("relative/path")
        with self.assertRaises(ValueError):
            repair.resolve_folder("/")
        with self.assertRaises(ValueError):
            repair.resolve_folder("/tmp/does-not-exist-xyz")
        with mock.patch.object(Path, "resolve", side_effect=OSError("boom")):
            with self.assertRaises(ValueError):
                repair.resolve_folder("/tmp")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(repair.resolve_folder(tmp), Path(tmp).resolve())
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaises(ValueError):
                    repair.resolve_folder(tmp)

    def test_is_file(self):
        with tempfile.NamedTemporaryFile() as handle:
            self.assertTrue(repair._is_file(Path(handle.name)))
        self.assertFalse(repair._is_file(Path("/tmp/does-not-exist-xyz")))
        with mock.patch.object(Path, "is_file", side_effect=OSError("boom")):
            self.assertFalse(repair._is_file(Path("/tmp")))

    def test_scan_missing_paths(self):
        result = repair.scan_missing_paths({"games": "nope"})
        self.assertEqual(result["items"], [])
        with tempfile.NamedTemporaryFile() as handle:
            state = {
                "games": [
                    "nope",
                    {"game_id": "g1", "name": "Real", "path": handle.name},
                    {"game_id": "g2", "name": "Missing", "path": "/tmp/does-not-exist-xyz"},
                ]
            }
            result = repair.scan_missing_paths(state)
        self.assertEqual([item["game_id"] for item in result["items"]], ["g2"])

    def test_scan_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            sub = folder / "Doom"
            sub.mkdir()
            (sub / "doom.exe").write_bytes(b"x")
            found = repair.scan_candidates(str(folder))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["name"], "doom.exe")
            with mock.patch("os.scandir", side_effect=OSError("boom")):
                self.assertEqual(repair.scan_candidates(str(folder)), [])

    def test_scan_candidates_weird_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            weird = mock.Mock()
            weird.name = "weird"
            weird.is_symlink.return_value = False
            weird.is_dir.return_value = False
            weird.is_file.return_value = False
            weird.path = str(Path(tmp) / "weird")
            with mock.patch("os.scandir", return_value=[weird]):
                self.assertEqual(repair.scan_candidates(tmp), [])
            broken = mock.Mock()
            broken.name = "broken"
            broken.is_symlink.return_value = False
            broken.is_dir.return_value = False
            broken.is_file.return_value = True
            broken.path = str(Path(tmp) / "broken")
            broken.stat.side_effect = OSError("boom")
            with mock.patch("os.scandir", return_value=[broken]):
                self.assertEqual(repair.scan_candidates(tmp), [])

    def test_plan_repair(self):
        self.assertEqual(
            repair.plan_repair([], [])["counts"],
            {"matched": 0, "ambiguous": 0, "unmatched": 0},
        )
        plan = repair.plan_repair([], ["nope"])
        self.assertEqual(plan["counts"]["matched"], 0)
        plan = repair.plan_repair(["nope"], [])
        self.assertEqual(plan["counts"]["matched"], 0)
        item = {"id": 0, "game_id": "g1", "name": "Doom", "field": "path", "path": "/old/doom"}
        plan = repair.plan_repair([item], [], fields=["cover"])
        self.assertEqual(plan["counts"]["unmatched"], 0)

    def test_apply_repair(self):
        with self.assertRaises(ValueError):
            repair.apply_repair({"games": "nope"}, [])
        result = repair.apply_repair({"games": []}, ["nope"])
        self.assertEqual(result["updated"], 0)
        result = repair.apply_repair(
            {"games": []}, [{"id": "x", "field": "path"}]
        )
        self.assertEqual(result["skipped"][0]["reason"], "invalid_id")
        with tempfile.NamedTemporaryFile() as handle:
            target = handle.name
            state = {
                "games": [
                    {"game_id": "g1", "name": "Doom", "path": "/old/doom"},
                    {"game_id": "g2", "name": "Quake", "path": target},
                ]
            }
            stale = {
                "id": 0,
                "game_id": "g2",
                "field": "path",
                "path": target,
                "from": "/old/doom",
            }
            result = repair.apply_repair(state, [stale])
            self.assertEqual(result["skipped"][0]["reason"], "stale_game")
            noop = {
                "id": 1,
                "game_id": "g2",
                "field": "path",
                "path": target,
                "from": target,
            }
            result = repair.apply_repair(state, [noop])
            self.assertEqual(result["updated"], 0)
            relink = {
                "id": 0,
                "game_id": "g1",
                "field": "path",
                "path": target,
                "from": "/old/doom",
            }
            result = repair.apply_repair(
                state, [relink, stale, noop], selection=[("x", "path"), "abc", 0]
            )
            self.assertEqual(result["updated"], 1)
            self.assertEqual(state["games"][0]["path"], target)


# ── pkg/parity/parity_duplicates.py ──────────────────────────────────────────

class DuplicatesEngineTests(unittest.TestCase):
    def test_normalize_path(self):
        self.assertEqual(duplicates.normalize_path(None), "")
        self.assertEqual(duplicates.normalize_path("  "), "")
        self.assertTrue(duplicates.normalize_path("/Games/Doom"))

    def test_sessions_by_game(self):
        self.assertEqual(duplicates._sessions_by_game({}), {})
        state = {
            "history": [
                {"game_id": "g1"},
                {"game_id": "g1"},
                "nope",
                {"game_id": ""},
            ]
        }
        self.assertEqual(duplicates._sessions_by_game(state), {"g1": 2})

    def test_identity_groups(self):
        self.assertEqual(duplicates._identity_groups(["nope"]), [])
        groups = duplicates._identity_groups(
            [
                {"game_id": "g1", "name": "Doom"},
                {"game_id": "g2", "name": "Doom"},
            ]
        )
        self.assertIsInstance(groups, list)

    def test_find_duplicates(self):
        self.assertEqual(duplicates.find_duplicates({"games": "nope"})["groups"], [])
        self.assertEqual(duplicates.find_duplicates({"games": ["nope"]})["groups"], [])
        games = [
            {"game_id": "g1", "name": "Doom", "path": "/x/doom"},
            {"game_id": "g2", "name": "Doom II", "path": "/x/doom"},
        ]
        result = duplicates.find_duplicates({"games": games})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["groups"][0]["kind"], "identity")

    def test_find_duplicates_truncation(self):
        games = [
            {"game_id": "g1", "name": "Doom", "path": "/x/doom"},
            {"game_id": "g2", "name": "Doom II", "path": "/x/doom"},
        ]
        with mock.patch.object(
            duplicates,
            "detect_duplicate_identities",
            return_value=[{"games": ["g1", "g2"], "identity": "x"}],
        ):
            result = duplicates.find_duplicates({"games": games}, limit=0)
        self.assertTrue(result["truncated"])
        with mock.patch.object(
            duplicates, "detect_duplicate_identities", return_value=[]
        ):
            result = duplicates.find_duplicates(
                {"games": games}, limit=0, include_title=False
            )
        self.assertTrue(result["truncated"])

    def test_primary_choice(self):
        self.assertIsNone(duplicates.primary_choice([]))
        self.assertIsNone(duplicates.primary_choice(["nope"]))
        rows = [
            {
                "id": 0,
                "game_id": "g1",
                "sessions": 0,
                "playtime_seconds": 0,
                "achievements": 0,
                "media": 0,
            },
            {
                "id": 1,
                "game_id": "g2",
                "sessions": 5,
                "playtime_seconds": 0,
                "achievements": 0,
                "media": 0,
            },
        ]
        self.assertEqual(duplicates.primary_choice(rows)["game_id"], "g2")

    def test_as_list_and_union_list(self):
        self.assertEqual(duplicates._as_list("x"), ["x"])
        self.assertEqual(duplicates._as_list(None), [])
        self.assertEqual(
            duplicates._union_list(
                [[{"path": "p", "name": "n", "id": "i", "url": "u"}]]
            ),
            [{"path": "p", "name": "n", "id": "i", "url": "u"}],
        )
        self.assertEqual(duplicates._union_list([["a"], "nope", ["a"]]), ["a"])
        capped = duplicates._union_list([list(range(250))])
        self.assertEqual(len(capped), 200)

    def test_merge_plan(self):
        with self.assertRaises(ValueError):
            duplicates.merge_plan({}, [0, 1])
        with self.assertRaises(ValueError):
            duplicates.merge_plan({"games": ["nope", "nope"]}, [0, 1])
        games = [
            {"game_id": "g1", "name": "Doom", "playtime_seconds": 100, "tags": ["a"]},
            {
                "game_id": "g2",
                "name": "Doom",
                "playtime_seconds": 0,
                "cover": "/x/cover.png",
                "tags": ["b"],
                "developer": "id Software",
            },
        ]
        plan = duplicates.merge_plan({"games": games}, ["x", 0, 1])
        self.assertEqual(plan["primary"]["game_id"], "g1")
        self.assertIn("cover", plan["changed_fields"])
        self.assertIn("tags", plan["changed_fields"])
        self.assertIn("developer", plan["changed_fields"])
        with self.assertRaises(ValueError):
            duplicates.merge_plan({"games": games}, [0])

    def test_apply_merge(self):
        games = [
            {
                "game_id": "g1",
                "id": 0,
                "name": "Doom",
                "playtime_seconds": 100,
                "tags": ["a"],
            },
            {
                "game_id": "g2",
                "id": 1,
                "name": "Doom",
                "playtime_seconds": 0,
                "tags": ["b"],
            },
        ]
        state = {
            "games": games,
            "playlists": [
                {"type": "manual", "members": ["g2", "g9"]},
                {"type": "smart", "members": ["g2"]},
                "nope",
            ],
            "trash": [],
        }

        def trash_game(_state, game):
            return {"trash_id": "t1", "name": game["name"]}

        result = duplicates.apply_merge(state, [0, 1], trash_game=trash_game)
        self.assertEqual(result["merged"], 1)
        self.assertEqual([g["game_id"] for g in state["games"]], ["g1"])
        self.assertEqual(state["games"][0]["tags"], ["a", "b"])
        self.assertEqual(state["playlists"][0]["members"], ["g9"])
        self.assertEqual(state["playlists"][1]["members"], ["g2"])
        self.assertEqual(len(state["trash"]), 1)


# ── pkg/parity/parity_dna.py ─────────────────────────────────────────────────

class DnaEngineTests(unittest.TestCase):
    def _games(self):
        return [
            {
                "game_id": "g1",
                "name": "Doom Eternal",
                "description": "Rip and tear demons in a fast intense shooter.",
                "genre": "Shooter",
                "tags": ["demons"],
            },
            {
                "game_id": "g2",
                "name": "Stardew Valley",
                "description": "Relaxing cozy farming with demons in the mines.",
                "genre": "Simulation",
                "tags": ["cozy"],
            },
        ]

    def test_load_index_bad_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "dna_index.json").write_text(json.dumps({"format": 999}))
            self.assertIsNone(dna.load_index(tmp))
            Path(tmp, "dna_index.json").write_text("not json")
            self.assertIsNone(dna.load_index(tmp))

    def test_empty_and_rebuild_index(self):
        index = dna.empty_index()
        self.assertEqual(index["games"], {})
        index = dna.rebuild_index(["not-a-dict"], "en")
        self.assertEqual(index["games"], {})

    def test_avg_doc_len(self):
        self.assertEqual(dna._avg_doc_len({}), 1.0)
        self.assertEqual(dna._avg_doc_len({"avg_len": 7.5}), 7.5)
        self.assertEqual(dna._avg_doc_len({"games": {"d1": {"l": 4.0, "v": {}}}}), 4.0)

    def test_bm25_candidate_filtering(self):
        games = self._games()
        index = dna.rebuild_index(games, "en")
        d1 = dna.doc_id_for(games[0])
        results = dna.bm25_search(index, {"demons": 1.0}, candidates=[d1])
        self.assertEqual([doc for doc, _ in results], [d1])

    def test_cosine_similarity(self):
        self.assertAlmostEqual(
            dna.cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0
        )
        self.assertAlmostEqual(
            dna.cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0
        )

    def test_evict_hashed(self):
        index = {"_hashed": {"d1": [1.0, 2.0]}}
        dna._evict_hashed(index, "d1")
        self.assertEqual(index["_hashed"], {})
        dna._evict_hashed({}, "d1")

    def test_similarity_search_edge_cases(self):
        self.assertEqual(dna.similarity_search({}, {"a": 1.0}), [])
        games = self._games()
        index = dna.rebuild_index([games[0]], "en")
        doc = dna.doc_id_for(games[0])
        vector = index["games"][doc]["v"]
        self.assertEqual(dna.similarity_search(index, vector, exclude_id=doc), [])

    def test_resolve_anchor(self):
        games = self._games()
        self.assertIsNone(dna.resolve_anchor("x", games))
        self.assertIsNone(dna.resolve_anchor("zzznomatch", games))
        self.assertEqual(
            dna.resolve_anchor("Doom", ["nope", {"game_id": "g9"}, *games])["game_id"],
            "g1",
        )

    def test_taste_boost(self):
        score, chips = dna.taste_boost({"user_rating": "abc"}, 1.0)
        self.assertEqual((score, chips), (1.25, []))
        score, chips = dna.taste_boost({"user_rating": 4}, 2.0)
        self.assertAlmostEqual(score, 2.0 * 1.6 * 1.25)
        self.assertEqual(chips, ["4★ rated"])

    def test_ttb_chip(self):
        self.assertIsNone(dna._ttb_chip({"time_to_beat_hours": "abc"}))
        self.assertIsNone(dna._ttb_chip({}))
        self.assertEqual(dna._ttb_chip({"time_to_beat_hours": 0.5}), "~30m to beat")
        self.assertEqual(dna._ttb_chip({"time_to_beat_hours": 10}), "~10h to beat")

    def test_parse_dna_query_anchor_not_in_index(self):
        games = self._games()
        result = dna.parse_dna_query("like Doom", games, dna.empty_index(), "en")
        self.assertEqual(result["branch"], "similarity")

    def test_parse_dna_query_unresolved_anchor(self):
        games = self._games()
        index = dna.rebuild_index(games, "en")
        result = dna.parse_dna_query("like zzzznomatch cozy", games, index, "en")
        self.assertEqual(result["branch"], "bm25-unresolved-anchor")
        self.assertTrue(result["results"])
        self.assertIn("no match", result["results"][0]["why"][0])

    def test_parse_dna_query_stale_results(self):
        games = self._games()
        index = dna.rebuild_index(games, "en")
        result = dna.parse_dna_query("demons", [], index, "en")
        self.assertEqual(result["results"], [])


# ── pkg/parity/parity_artwork_hygiene.py ─────────────────────────────────────

class ArtworkHygieneTests(unittest.TestCase):
    def _png(self, width, height):
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        chunk = b"IHDR" + ihdr
        crc = struct.pack(">I", 0)
        return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk + crc

    def test_jpeg_size_branches(self):
        self.assertIsNone(artwork_hygiene._jpeg_size(b"not a jpeg"))
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8\xff\xe0"))
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8\xff\xe0\x00\x10" + b"\x00" * 20))
        sof = b"\xff\xd8\xff\xc0\x00\x0b\x08\x01\x00\x00\x64\x01\x01\x11\x00"
        self.assertEqual(artwork_hygiene._jpeg_size(sof), (100, 256))

    def test_jpeg_size_marker_skips(self):
        # non-0xFF bytes are skipped (lines 73-75)
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8" + b"\x00" * 10))
        # standalone markers (SOI/RST) are skipped (lines 77-79)
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8\xff\xd8" + b"\x00" * 10))
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8\xff\xd0" + b"\x00" * 10))
        # truncated SOF segment returns None (line 85)
        self.assertIsNone(artwork_hygiene._jpeg_size(b"\xff\xd8\xff\xc0\x00\x0b\x08\x01"))

    def test_webp_size_branches(self):
        self.assertIsNone(artwork_hygiene._webp_size(b"RIFF"))
        self.assertIsNone(artwork_hygiene._webp_size(b"RIFF\x00\x00\x00\x00WEBPXXXX"))
        vp8x = b"RIFF" + struct.pack("<I", 10) + b"WEBPVP8X" + b"\x00" * 10
        self.assertIsNone(artwork_hygiene._webp_size(vp8x))

    def test_resolve_artwork_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(artwork_hygiene.resolve_artwork_path("", tmp))
            self.assertEqual(
                artwork_hygiene.resolve_artwork_path("/tmp/does-not-exist-xyz.png", tmp),
                Path("/tmp/does-not-exist-xyz.png"),
            )
            image = Path(tmp) / "art.png"
            image.write_bytes(self._png(16, 16))
            self.assertEqual(
                artwork_hygiene.resolve_artwork_path("art.png", tmp), image
            )

    def test_build_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "big.png"
            big.write_bytes(self._png(64, 64))
            small = Path(tmp) / "small.png"
            small.write_bytes(self._png(8, 8))
            games = [
                {"game_id": "g1", "name": "Big", "cover": str(big)},
                {"game_id": "g2", "name": "Small", "cover": str(small)},
                {"game_id": "g3", "name": "None"},
                {"game_id": "g4", "name": "Missing", "cover": "/tmp/does-not-exist-xyz.png"},
            ]
            report = artwork_hygiene.build_report(games, media_root=tmp)
            kinds = {row["issue"] for row in report["issues"]}
            self.assertIn("low_res", kinds)
            self.assertIn("missing_cover", kinds)
            self.assertIn("missing_file", kinds)
            self.assertGreater(report["scanned"], 0)

    def test_build_report_unparseable_image_and_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "bogus.png"
            bogus.write_bytes(b"this is not an image at all")
            games = [
                {"game_id": "g5", "name": "Bogus", "cover": str(bogus)},
                {"game_id": "g6", "name": "NoArt", "artwork_provider": "steamgrid"},
            ]
            report = artwork_hygiene.build_report(games, media_root=tmp)
            self.assertNotIn(
                "g5", {row["game_id"] for row in report["issues"]}
            )
            self.assertEqual(report["providers"].get("steamgrid"), 1)

    def test_select_fixable(self):
        report = {
            "issues": [
                {"issue": "low_res", "game_id": "g1", "name": "Big"},
                {"issue": "missing_file", "game_id": "g2", "name": "Gone"},
                {"issue": "something_else", "game_id": "g3", "name": "Odd"},
            ]
        }
        fixable = artwork_hygiene.select_fixable(report)
        self.assertEqual([row["game_id"] for row in fixable], ["g1"])
        self.assertEqual(artwork_hygiene.select_fixable(None), [])

    def test_select_fixable_missing_file_explicit(self):
        report = {
            "issues": [
                {"issue": "missing_file", "game_id": "g2", "name": "Gone"},
            ]
        }
        self.assertEqual(
            artwork_hygiene.select_fixable(report, issues=["missing_file"]), []
        )

    def test_png_and_bmp_size(self):
        self.assertEqual(
            artwork_hygiene._png_size(self._png(100, 50)), (100, 50)
        )
        self.assertIsNone(artwork_hygiene._png_size(b"nope"))
        blob = b"BM" + b"\x00" * 16 + struct.pack("<ii", 90, 70)
        self.assertEqual(artwork_hygiene._bmp_size(blob), (90, 70))
        self.assertIsNone(artwork_hygiene._bmp_size(b"nope"))

    def test_webp_size_all_variants(self):
        vp8x = (
            b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 8
            + (99).to_bytes(3, "little") + (59).to_bytes(3, "little")
        )
        self.assertEqual(artwork_hygiene._webp_size(vp8x), (100, 60))
        vp8 = (
            b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8 " + b"\x00" * 4
            + b"\x9d\x01\x2a" + struct.pack("<H", 320) + struct.pack("<H", 240)
            + b"\x00" * 3
        )
        self.assertEqual(artwork_hygiene._webp_size(vp8), (320, 240))
        bits = 99 | (59 << 14)
        vp8l = (
            b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8L" + b"\x00" * 5
            + bits.to_bytes(4, "little") + b"\x00" * 5
        )
        self.assertEqual(artwork_hygiene._webp_size(vp8l), (100, 60))

    def test_image_dimensions_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "cover.jpg"
            jpeg.write_bytes(
                b"\xff\xd8" + b"\x00" * 62 + b"\xff\xd8" + b"\xff\xc0"
                + struct.pack(">H", 11) + b"\x08"
                + struct.pack(">HH", 240, 320) + b"\x01\x11\x00"
            )
            self.assertEqual(
                artwork_hygiene.image_dimensions(jpeg), (320, 240)
            )
            self.assertIsNone(
                artwork_hygiene.image_dimensions(Path(tmp) / "missing.jpg")
            )

    def test_resolve_artwork_path_edge_cases(self):
        self.assertIsNone(artwork_hygiene.resolve_artwork_path(None))
        self.assertIsNone(artwork_hygiene.resolve_artwork_path("https://x/y.png"))
        self.assertEqual(
            artwork_hygiene.resolve_artwork_path("/abs/cover.png"),
            Path("/abs/cover.png"),
        )
        self.assertEqual(
            artwork_hygiene.resolve_artwork_path("rel/cover.png", "/media"),
            Path("/media/rel/cover.png"),
        )

    def test_aspect_issue(self):
        self.assertIsNone(artwork_hygiene._aspect_issue("nope", 100, 50))
        self.assertIsNone(artwork_hygiene._aspect_issue("cover", 0, 50))
        self.assertIsNone(artwork_hygiene._aspect_issue("cover", 150, 200))
        issue = artwork_hygiene._aspect_issue("cover", 400, 200)
        self.assertEqual(issue[1], 0.75)


# ── catalog.py ───────────────────────────────────────────────────────────────

class CatalogNormalizeTests(unittest.TestCase):
    def test_canon_progress(self):
        self.assertEqual(catalog.canon_progress("UNPLAYED"), "")
        self.assertEqual(catalog.canon_progress(" unplayed "), "")
        self.assertEqual(catalog.canon_progress("Beaten"), "Beaten")
        self.assertEqual(catalog.canon_progress(None), "")

    def test_normalize_notes(self):
        self.assertEqual(catalog.normalize_notes(None), [])
        self.assertEqual(catalog.normalize_notes(42), [])
        self.assertEqual(catalog.normalize_notes("  hi  "), [{"ts": "", "text": "hi"}])
        self.assertEqual(catalog.normalize_notes("   "), [])
        self.assertEqual(catalog.normalize_notes(["hello"]), [{"ts": "", "text": "hello"}])
        self.assertEqual(catalog.normalize_notes([None]), [])
        self.assertEqual(catalog.normalize_notes([{"text": "  "}]), [])
        self.assertEqual(catalog.normalize_notes([{"text": "hi"}])[0]["text"], "hi")

    def test_normalize_manual_sessions(self):
        self.assertEqual(catalog.normalize_manual_sessions("nope"), [])
        self.assertEqual(catalog.normalize_manual_sessions(["nope"]), [])
        self.assertEqual(catalog.normalize_manual_sessions([{"seconds": "abc"}]), [])
        self.assertEqual(catalog.normalize_manual_sessions([{"seconds": 0}]), [])
        sessions = catalog.normalize_manual_sessions(
            [{"seconds": 3600, "date": " 2026-01-01 ", "note": " x "}]
        )
        self.assertEqual(
            sessions,
            [{"date": "2026-01-01", "seconds": 3600, "note": "x"}],
        )

    def test_total_playtime_seconds(self):
        self.assertEqual(catalog.total_playtime_seconds("nope"), 0)
        self.assertEqual(catalog.total_playtime_seconds({"playtime_seconds": "abc"}), 0)
        self.assertEqual(
            catalog.total_playtime_seconds({"manual_playtime_seconds": "abc"}), 0
        )
        self.assertEqual(
            catalog.total_playtime_seconds(
                {"playtime_seconds": 60, "manual_playtime_seconds": 30}
            ),
            90,
        )

    def test_progress_suggest_due(self):
        self.assertFalse(catalog.progress_suggest_due("nope", {}))
        self.assertFalse(
            catalog.progress_suggest_due({"name": "x"}, {"backlog_progress_suggest": False})
        )
        self.assertFalse(catalog.progress_suggest_due({"name": "x"}, {}))
        self.assertTrue(
            catalog.progress_suggest_due(
                {"name": "x"}, {"progress_on_first_play": ""}
            )
        )


# ── pkg/parity/parity_query.py ───────────────────────────────────────────────

class QueryRatingTests(unittest.TestCase):
    def _clause(self, clause):
        return clause[0]["clause"]

    def test_user_rating_stars(self):
        self.assertIsNone(parity_query._user_rating_stars(["my", "zzz"]))
        clause = self._clause(parity_query._user_rating_stars(["my", "4"]))
        self.assertEqual(clause["kind"], "user_rating_min")
        self.assertEqual(clause["value"], 4)

    def test_user_rating_at_least(self):
        self.assertIsNone(
            parity_query._user_rating_at_least(["my", "rating", "at", "least", "zzz"])
        )
        clause = self._clause(
            parity_query._user_rating_at_least(["my", "rating", "at", "least", "4"])
        )
        self.assertEqual(clause["kind"], "user_rating_min")
        self.assertEqual(clause["value"], 4)

    def test_user_rating_rated(self):
        self.assertIsNone(parity_query._user_rating_rated(["zzz"]))
        clause = self._clause(parity_query._user_rating_rated(["4"]))
        self.assertEqual(clause["kind"], "user_rating_min")
        self.assertEqual(clause["value"], 4)

    def test_user_rating(self):
        self.assertEqual(parity_query._user_rating({"user_rating": 4}), 4)
        self.assertEqual(parity_query._user_rating({"user_rating": "abc"}), 0)
        self.assertEqual(parity_query._user_rating({}), 0)


# ── openbox.py ───────────────────────────────────────────────────────────────

class OpenBoxEventTests(unittest.TestCase):
    def test_library_event_snapshot_exception(self):
        with mock.patch.object(openbox, "load_state_readonly", side_effect=RuntimeError("boom")):
            self.assertIsNone(openbox._library_event_snapshot())

    def test_emit_library_diff(self):
        openbox._emit_library_diff(None)
        with mock.patch.object(openbox, "_library_event_snapshot", side_effect=RuntimeError("boom")):
            openbox._emit_library_diff({"games": []})

    def test_update_state_with_result(self):
        fake = mock.Mock()
        fake.update_with_result.return_value = ("state", "result")
        with mock.patch.object(openbox, "STATE_STORE", fake), \
             mock.patch.object(openbox, "_library_event_snapshot", return_value={}), \
             mock.patch.object(openbox, "_emit_library_diff"):
            result = openbox.update_state_with_result(lambda state: state)
        self.assertEqual(result, ("state", "result"))


# ── handlers/discovery.py (engine) ───────────────────────────────────────────

class DiscoveryEngineTests(unittest.TestCase):
    def test_state_signature_exception(self):
        with mock.patch.object(openbox.STATE_STORE, "signature", side_effect=RuntimeError("boom")):
            self.assertIsNone(discovery._state_signature())

    def test_reconcile_index(self):
        games = [
            {"game_id": "g1", "name": "Doom", "description": "demons shooter"},
            {"game_id": "g2", "name": "Quake", "description": "demons shooter"},
        ]
        index = dna.rebuild_index(games, "en")
        out, changed = discovery.reconcile_index(index, [games[0]], "en", None)
        self.assertEqual(changed, 1)
        self.assertEqual(out, index)
        out, changed = discovery.reconcile_index(None, [], "en", None)
        self.assertEqual((out, changed), (None, 0))

    def test_submit_rebuild_job_cancelled(self):
        submitted = {}

        def fake_submit(name, worker, replace=False):
            submitted["worker"] = worker
            return {"job_id": "j1"}

        fake_jobs = mock.Mock()
        fake_jobs.submit.side_effect = fake_submit
        cancel = mock.Mock()
        cancel.is_cancelled.return_value = True
        with mock.patch.object(discovery, "JOB_MANAGER", fake_jobs), \
             mock.patch.object(
                 discovery, "load_state_readonly",
                 return_value={"games": [{"game_id": "g1", "name": "Doom"}]},
             ):
            discovery._submit_rebuild_job(reason="manual")
            result = submitted["worker"](cancel_event=cancel)
        self.assertEqual(result, {"cancelled": True, "processed": 0})

    def test_dna_index_for_search_threshold(self):
        games = [{"game_id": f"g{i}", "name": f"Game {i}"} for i in range(5000)]
        with mock.patch.object(dna, "load_index", return_value=None), \
             mock.patch.object(discovery, "_submit_rebuild_job") as submit:
            index, degraded, building = discovery.dna_index_for_search(
                {"games": games, "settings": {}}
            )
        self.assertEqual((index, degraded, building), (None, True, True))
        submit.assert_called_once()

    def test_dna_index_for_search_sync_build(self):
        games = [{"game_id": "g1", "name": "Doom", "description": "demons shooter"}]
        with mock.patch.object(dna, "load_index", return_value=None):
            index, degraded, building = discovery.dna_index_for_search(
                {"games": games, "settings": {}}
            )
        self.assertEqual((degraded, building), (False, False))
        self.assertEqual(index["doc_count"], 1)

    def test_dna_index_for_search_save_failure(self):
        games = [{"game_id": "g1", "name": "Doom", "description": "demons shooter"}]
        index = dna.rebuild_index(games, "en")
        changed = [{"game_id": "g1", "name": "Doom", "description": "totally different demons"}]
        with mock.patch.object(dna, "load_index", return_value=index), \
             mock.patch.object(discovery, "_state_signature", return_value="sig-new"), \
             mock.patch.object(dna, "save_index_atomic", side_effect=OSError("boom")), \
             mock.patch.object(discovery, "_submit_rebuild_job") as submit:
            out, degraded, building = discovery.dna_index_for_search(
                {"games": changed, "settings": {}}
            )
        self.assertEqual((degraded, building), (True, False))
        self.assertIs(out, index)
        submit.assert_called_once()

    def test_dna_index_for_search_fresh(self):
        games = [{"game_id": "g1", "name": "Doom", "description": "demons shooter"}]
        index = dna.rebuild_index(games, "en", ["sig"])
        with mock.patch.object(dna, "load_index", return_value=index), \
             mock.patch.object(discovery, "_state_signature", return_value=["sig"]), \
             mock.patch.object(dna, "save_index_atomic") as save, \
             mock.patch.object(discovery, "_submit_rebuild_job") as submit:
            out, degraded, building = discovery.dna_index_for_search(
                {"games": games, "settings": {}}
            )
        self.assertEqual((degraded, building), (False, False))
        self.assertIs(out, index)
        save.assert_not_called()
        submit.assert_not_called()

    def test_dna_search_rejects_non_dict_payload(self):
        handler = discovery.DiscoveryHandlers()
        with self.assertRaises(BadRequest):
            handler._api_post_api_v2_library_dna_search("notadict")

    def test_title_fallback(self):
        games = [
            {"game_id": "a", "name": "Doom"},
            {"game_id": "b", "name": "Doom II"},
        ]
        results = discovery._title_fallback(games, "doom", 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["game_id"], "a")

    def test_apply_filters(self):
        games = [{"game_id": "g1", "name": "Doom Eternal", "genre": "Shooter"}]
        self.assertEqual(discovery._apply_filters([{"game_id": "ghost"}], games, {"genre": "Shooter"}), [])
        self.assertEqual(
            discovery._apply_filters([{"game_id": "g1"}], games, {"bogus": "x"}),
            [{"game_id": "g1"}],
        )
        self.assertEqual(
            discovery._apply_filters([{"game_id": "g1"}], games, {"genre": "shooter"}),
            [{"game_id": "g1"}],
        )


# ── handlers/discovery.py (HTTP) ─────────────────────────────────────────────

class DiscoveryHttpTests(HttpTestBase):
    def test_search_rejects_malformed_body(self):
        status, _ = self.http("/api/v2/library/dna/search", body="notadict")
        self.assertEqual(status, 400)

    def test_status_building_when_threshold_exceeded(self):
        games = [{"game_id": f"g{i}", "name": f"Game {i}"} for i in range(5000)]
        with mock.patch.object(
            discovery, "load_state_readonly",
            return_value={"games": games, "settings": {}},
        ), mock.patch.object(dna, "load_index", return_value=None), \
            mock.patch.object(discovery, "_submit_rebuild_job"):
            status, payload = self.http("/api/v2/library/dna/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "building")

    def test_status_save_failure(self):
        games = [{"game_id": "g1", "name": "Doom", "description": "demons shooter"}]
        index = dna.rebuild_index(games, "en")
        changed = [{"game_id": "g1", "name": "Doom", "description": "totally different demons"}]
        with mock.patch.object(
            discovery, "load_state_readonly",
            return_value={"games": changed, "settings": {}},
        ), mock.patch.object(dna, "load_index", return_value=index), \
            mock.patch.object(dna, "save_index_atomic", side_effect=OSError("boom")), \
            mock.patch.object(discovery, "_submit_rebuild_job"):
            status, payload = self.http("/api/v2/library/dna/status")
        self.assertEqual(status, 200)
        self.assertIn(payload["state"], ("stale", "ready"))


# ── handlers/library.py (DNA hooks + clean) ──────────────────────────────────

class LibraryDnaHookTests(unittest.TestCase):
    def test_dna_note_hooks_index_failure(self):
        with mock.patch.object(dna, "load_index", side_effect=RuntimeError("boom")):
            library_mod._dna_note_upserted([{"game_id": "g1"}])
            library_mod._dna_note_removed(["g1"])

    def test_dna_refresh_ids(self):
        library_mod._dna_refresh_ids([])
        with mock.patch.object(
            library_mod, "load_state_readonly", side_effect=RuntimeError("boom")
        ):
            library_mod._dna_refresh_ids(["g1"])

    def test_clean_game_fields_user_rating(self):
        game = library_mod._clean_game_fields({"name": "Doom", "user_rating": 4})
        self.assertEqual(game["user_rating"], 4)


# ── handlers/library.py (HTTP) ───────────────────────────────────────────────

class LibraryHttpTests(HttpTestBase):
    def setUp(self):
        seed_state()
        state = openbox.load_state_readonly()
        openbox.save_state(
            {**state, "games": [{"game_id": "seed-game", "name": "Seed Game"}]}
        )
        self.game_id = openbox.load_state_readonly()["games"][0]["game_id"]

    def test_save_game_accepts_real_file_path(self):
        with tempfile.NamedTemporaryFile(suffix=".exe") as handle:
            status, payload = self.http(
                "/api/game",
                {"game": {"name": "Pathed", "path": handle.name}},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_duplicates_route_include_title_false(self):
        status, payload = self.http("/api/v2/library/duplicates?title=0")
        self.assertEqual(status, 200)
        self.assertIn("groups", payload)

    def _playtime(self, body):
        return self.http("/api/v2/library/playtime/log", body)

    def test_playtime_log_bad_seconds(self):
        status, _ = self._playtime({"game_id": self.game_id, "seconds": "abc"})
        self.assertEqual(status, 400)

    def test_playtime_log_long_note(self):
        status, _ = self._playtime(
            {"game_id": self.game_id, "seconds": 60, "note": "x" * 201}
        )
        self.assertEqual(status, 400)

    def test_playtime_update_bad_index(self):
        status, _ = self.http(
            "/api/v2/library/playtime/update",
            {"game_id": self.game_id, "index": "x", "seconds": 60},
        )
        self.assertEqual(status, 400)

    def test_playtime_update_unknown_entry(self):
        status, _ = self.http(
            "/api/v2/library/playtime/update",
            {"game_id": self.game_id, "index": 9, "seconds": 60},
        )
        self.assertEqual(status, 400)

    def test_playtime_delete_bad_index(self):
        status, _ = self.http(
            "/api/v2/library/playtime/delete", {"game_id": self.game_id, "index": "x"}
        )
        self.assertEqual(status, 400)

    def test_playtime_delete_unknown_entry(self):
        status, _ = self.http(
            "/api/v2/library/playtime/delete", {"game_id": self.game_id, "index": 5}
        )
        self.assertEqual(status, 400)

    def test_notes_add_empty(self):
        status, _ = self.http(
            "/api/v2/library/notes/add", {"game_id": self.game_id, "text": "   "}
        )
        self.assertEqual(status, 400)

    def test_notes_add_too_long(self):
        status, _ = self.http(
            "/api/v2/library/notes/add", {"game_id": self.game_id, "text": "x" * 2001}
        )
        self.assertEqual(status, 400)

    def test_notes_update_unknown_entry(self):
        status, _ = self.http(
            "/api/v2/library/notes/update",
            {"game_id": self.game_id, "index": 0, "text": "hi"},
        )
        self.assertEqual(status, 400)

    def test_notes_update_bad_index(self):
        status, _ = self.http(
            "/api/v2/library/notes/update",
            {"game_id": self.game_id, "index": "x", "text": "hi"},
        )
        self.assertEqual(status, 400)

    def test_notes_update_empty_text(self):
        status, _ = self.http(
            "/api/v2/library/notes/update",
            {"game_id": self.game_id, "index": 0, "text": "   "},
        )
        self.assertEqual(status, 400)

    def test_notes_update_too_long(self):
        status, _ = self.http(
            "/api/v2/library/notes/update",
            {"game_id": self.game_id, "index": 0, "text": "x" * 2001},
        )
        self.assertEqual(status, 400)

    def test_notes_delete_bad_index(self):
        status, _ = self.http(
            "/api/v2/library/notes/delete", {"game_id": self.game_id, "index": "x"}
        )
        self.assertEqual(status, 400)

    def test_notes_delete_unknown_entry(self):
        status, _ = self.http(
            "/api/v2/library/notes/delete", {"game_id": self.game_id, "index": 3}
        )
        self.assertEqual(status, 400)


# ── handlers/library_health.py ───────────────────────────────────────────────

class LibraryHealthRouteTests(unittest.TestCase):
    def _shim(self):
        return library_health._Shim()

    def test_shim_send_json(self):
        shim = self._shim()
        shim.send_json(200, {"ok": True})
        self.assertEqual((shim.status, shim.payload), (200, {"ok": True}))

    def test_health_issues_invalid_offset(self):
        seed_state()
        shim = self._shim()
        with self.assertRaises(BadRequest):
            library_health.health_issues(shim, SimpleNamespace(query="offset=abc"))

    def test_plan_duplicates_skips(self):
        state = {"games": [{"game_id": "g1", "name": "Doom"}]}
        issues = [{"game_id": "g1"}]
        with mock.patch.object(
            library_health, "_duplicate_groups", return_value=[{"games": ["g1"]}]
        ):
            self.assertEqual(library_health._plan_duplicates(state, issues), [])
        state = {
            "games": [
                {"game_id": "g1", "name": "Doom"},
                {"game_id": "g2", "name": "Doom"},
            ]
        }
        with mock.patch.object(
            library_health, "_duplicate_groups", return_value=[{"games": ["g1", "g2"]}]
        ), mock.patch(
            "pkg.parity.parity_duplicates.merge_plan", side_effect=ValueError("nope")
        ):
            self.assertEqual(library_health._plan_duplicates(state, issues), [])

    def test_execute_duplicates_skips(self):
        state = {"games": [{"game_id": "g1", "name": "Doom"}], "trash": []}
        with mock.patch.object(
            library_health, "_duplicate_groups", return_value=[{"games": ["g1"]}]
        ):
            result = library_health._execute_duplicates(state, [{"game_id": "g1"}])
        self.assertEqual(result, {"merged": 0, "trash_ids": []})

    def test_execute_artwork_refuses(self):
        def refuse(shim, payload):
            shim.send_json(500, {"error": "nope"})

        with mock.patch(
            "handlers.steamgrid.steamgrid_hygiene_fix", side_effect=refuse
        ):
            with self.assertRaises(BadRequest):
                library_health._execute_artwork({"games": []}, [{"game_id": "g1"}], {})

    def test_build_fix_plan_unknown_dimension(self):
        with self.assertRaises(BadRequest):
            library_health._build_fix_plan({}, "weird", [], {})

    def test_health_fix(self):
        seed_state()
        shim = self._shim()
        with self.assertRaises(BadRequest):
            library_health.health_fix(shim, {"dimension": "weird"})
        with self.assertRaises(BadRequest):
            library_health.health_fix(shim, {"dimension": "duplicates", "issue_ids": []})

    def test_health_undo(self):
        seed_state()
        shim = self._shim()
        with self.assertRaises(BadRequest):
            library_health.health_undo(shim, {})
        with self.assertRaises(BadRequest):
            library_health.health_undo(shim, {"fix_id": "ghost"})

        def add_unknown_fix(current):
            health_engine.record_fix(
                current, {"fix_id": "f9", "inverse": {"kind": "weird"}}
            )

        openbox.update_state(add_unknown_fix)
        with self.assertRaises(BadRequest):
            library_health.health_undo(shim, {"fix_id": "f9"})

        def add_field_fix(current):
            current.setdefault("games", []).append({"game_id": "g1", "name": "Doom II"})
            health_engine.record_fix(
                current,
                {
                    "fix_id": "f10",
                    "inverse": {
                        "kind": "field_restore",
                        "changes": [
                            {
                                "game_id": "g1",
                                "field": "name",
                                "before": "Doom",
                                "after": "Doom II",
                            }
                        ],
                    },
                },
            )

        openbox.update_state(add_field_fix)
        shim = self._shim()
        library_health.health_undo(shim, {"fix_id": "f10"})
        self.assertEqual(shim.status, 200)
        self.assertTrue(shim.payload["undone"])

    def test_shim_auth(self):
        shim = self._shim()
        self.assertTrue(shim.authorized())
        with self.assertRaises(BadRequest):
            shim.handle_unauthorized()

    def test_health_fix_bad_issue_ids(self):
        seed_state()
        shim = self._shim()
        with self.assertRaises(BadRequest):
            library_health.health_fix(
                shim, {"dimension": "duplicates", "issue_ids": "nope"}
            )

    def _record_fix(self, fix_id, inverse):
        def mutate(current):
            health_engine.record_fix(
                current, {"fix_id": fix_id, "inverse": inverse}
            )

        openbox.update_state(mutate)

    def test_health_undo_trash_restore_bad_id(self):
        seed_state()
        self._record_fix(
            "f-undo-trash", {"kind": "trash_restore", "trash_ids": ["nope"]}
        )
        shim = self._shim()
        library_health.health_undo(shim, {"fix_id": "f-undo-trash"})
        self.assertEqual(shim.status, 200)
        self.assertTrue(shim.payload["undone"])
        self.assertEqual(shim.payload["restored"], 0)

    def test_health_undo_field_restore_unknown_game(self):
        seed_state()
        self._record_fix(
            "f-undo-ghost",
            {
                "kind": "field_restore",
                "changes": [
                    {
                        "game_id": "ghost",
                        "field": "name",
                        "before": "Doom",
                        "after": "Doom II",
                    }
                ],
            },
        )
        shim = self._shim()
        library_health.health_undo(shim, {"fix_id": "f-undo-ghost"})
        self.assertEqual(shim.status, 200)
        self.assertTrue(shim.payload["undone"])

    def test_health_undo_artwork_doctor_failure(self):
        seed_state()
        self._record_fix(
            "f-undo-art", {"kind": "artwork_doctor", "job_id": "j1"}
        )

        def fail_undo(shim, payload):
            shim.send_json(500, {"error": "nope"})

        with mock.patch.object(
            library_health, "_hygiene_batch_for_job", return_value="b1"
        ), mock.patch(
            "handlers.steamgrid.steamgrid_hygiene_undo", side_effect=fail_undo
        ):
            with self.assertRaises(BadRequest):
                library_health.health_undo(self._shim(), {"fix_id": "f-undo-art"})


# ── handlers/steambridge.py ──────────────────────────────────────────────────

class SteambridgeTests(unittest.TestCase):
    def test_grid_dirs_iterdir_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "userdata").mkdir()
            with mock.patch.object(
                steambridge, "_steam_roots", return_value=[Path(tmp)]
            ), mock.patch.object(Path, "iterdir", side_effect=OSError("boom")):
                self.assertEqual(steambridge._grid_dirs(), [])

    def test_copy_grid_art_copy_error(self):
        field, _suffix = steambridge._GRID_ART_FIELDS[0]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "art.jpg"
            src.write_bytes(b"JPG")
            grid = Path(tmp) / "grid"
            grid.mkdir()
            game = {"name": "Doom", field: str(src)}
            with mock.patch("shutil.copy2", side_effect=OSError("boom")):
                self.assertEqual(
                    steambridge._copy_grid_art(grid, "123", game), 0
                )

    def test_apply_grid_art_no_games(self):
        with mock.patch.object(steambridge, "_games", return_value=[]):
            result = steambridge._apply_grid_art(Path("/tmp/shortcuts.vdf"), {})
        self.assertEqual(result, {"applied": 0, "skipped": 0})

    def test_steambridge_apply_grid_art_exception(self):
        captured = {}

        def send_json(status, payload):
            captured["status"] = status
            captured["payload"] = payload

        shim = SimpleNamespace(send_json=send_json, authorized=lambda: True)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "shortcuts.vdf")
            plan = {"path": path}
            with mock.patch.object(
                steambridge, "apply_shortcuts", return_value={"applied": 1}
            ), mock.patch.object(
                steambridge, "_apply_grid_art", side_effect=RuntimeError("boom")
            ):
                steambridge.steambridge_apply(shim, {"plan": plan, "path": path})
        self.assertEqual(captured["status"], 200)
        self.assertEqual(
            captured["payload"]["grid_art"]["error"], "copy_failed"
        )

    def test_steambridge_apply_exception(self):
        shim = SimpleNamespace(
            send_json=lambda status, payload: None, authorized=lambda: True
        )
        with mock.patch.object(
            steambridge, "apply_shortcuts", side_effect=OSError("boom")
        ):
            with self.assertRaises(BadRequest):
                steambridge.steambridge_apply(shim, {"plan": {}, "path": "/tmp"})


# ── handlers/extensions.py (HTTP) ────────────────────────────────────────────

class ExtensionsHttpTests(HttpTestBase):
    def setUp(self):
        seed_state()

    def test_command_missing_identifiers(self):
        status, _ = self.http("/api/v2/plugins/command", {})
        self.assertEqual(status, 400)

    def test_command_hook_error(self):
        plugin_dir = Path(_DATA_DIR) / "plugins"
        make_plugin_package(
            plugin_dir,
            "cmdplug",
            {"commands": [{"id": "do-it", "label": "Do it"}]},
        )
        with mock.patch.object(
            handlers.extensions, "run_plugin_hook", return_value=(None, "boom")
        ):
            status, _ = self.http(
                "/api/v2/plugins/command",
                {"plugin_id": "cmdplug", "command": "do-it"},
            )
        self.assertEqual(status, 400)


# ── web_app.py ───────────────────────────────────────────────────────────────

class WebAppRangeTests(unittest.TestCase):
    def _handler(self, range_header=None):
        written = []
        handler = SimpleNamespace()
        handler.headers = {"Range": range_header} if range_header else {}
        handler.send_response = lambda *args: None
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        handler.headers_common = lambda *args, **kwargs: None
        handler._cache_headers = lambda path, stat: (
            "etag-x",
            "Mon, 01 Jan 2024 00:00:00 GMT",
        )
        handler.wfile = SimpleNamespace(write=written.append, flush=lambda: None)
        return handler, written

    def test_send_file_suffix_range(self):
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"0123456789")
            tmp_name = tmp.name
        try:
            handler, written = self._handler("bytes=-3")
            web_app.Handler.send_file(handler, 200, tmp_name)
        finally:
            os.unlink(tmp_name)
        self.assertEqual(b"".join(written), b"789")

    def test_send_file_invalid_range(self):
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(b"0123456789")
            tmp.flush()
            handler, _ = self._handler("bytes=abc")
            with self.assertRaises(web_app.RangeParseError):
                web_app.Handler.send_file(handler, 200, tmp.name)


class WebAppRescanTests(unittest.TestCase):
    def test_tick_skipped_when_running(self):
        with mock.patch.object(web_app, "RUNNING", {"launch1": object()}):
            self.assertIsNone(web_app._health_rescan_tick())

    def test_tick_not_due(self):
        with mock.patch.object(web_app, "RUNNING", {}), \
             mock.patch(
                 "pkg.parity.parity_library_health.rescan_due", return_value=False
             ), \
             mock.patch.object(web_app, "load_state", return_value={"settings": {}}):
            self.assertIsNone(web_app._health_rescan_tick())

    def test_tick_queues_rescan(self):
        with mock.patch.object(web_app, "RUNNING", {}), \
             mock.patch(
                 "pkg.parity.parity_library_health.rescan_due", return_value=True
             ), \
             mock.patch.object(web_app, "load_state", return_value={"settings": {}}), \
             mock.patch.object(web_app, "JOB_MANAGER") as jobs:
            web_app._health_rescan_tick()
        jobs.submit.assert_called_once()
        _, kwargs = jobs.submit.call_args
        self.assertTrue(kwargs.get("replace"))

    def test_tick_exception_swallowed(self):
        with mock.patch.object(web_app, "load_state", side_effect=RuntimeError("boom")):
            self.assertIsNone(web_app._health_rescan_tick())

    def test_worker_ticks_until_stop(self):
        calls = []
        with mock.patch.object(
            web_app.WATCH_STOP, "wait", side_effect=[False, True]
        ) as wait, mock.patch.object(
            web_app, "_health_rescan_tick", side_effect=lambda: calls.append(1)
        ):
            web_app._health_rescan_worker()
        self.assertEqual(calls, [1])
        self.assertEqual(wait.call_count, 2)


class WebAppMainTests(unittest.TestCase):
    def test_main_startup_and_shutdown(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 54321)
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(web_app, "handle_cli", return_value=None), \
             mock.patch.object(web_app, "update_state"), \
             mock.patch.object(web_app, "load_state", return_value={"settings": {}}), \
             mock.patch.object(
                 web_app, "reconcile_sessions_on_startup", return_value=([], [])
             ), \
             mock.patch.object(web_app, "ensure_stock_themes"), \
             mock.patch.object(web_app, "threading"), \
             mock.patch.object(web_app, "ThreadingHTTPServer", return_value=server), \
             mock.patch.object(web_app, "run_configured_commands"), \
             mock.patch.object(web_app, "emit_plugin_event") as emit, \
             mock.patch.object(web_app, "secure_text_write"), \
             mock.patch.object(
                 web_app, "bigbox_launch_url", side_effect=lambda url: url
             ), \
             mock.patch.object(web_app, "is_gamescope_guest", return_value=False), \
             mock.patch.object(web_app, "shutdown_webhooks"), \
             mock.patch(
                 "pkg.parity.parity_library_health.normalize_rescan_setting",
                 return_value="on_startup",
             ), \
             mock.patch.object(web_app, "JOB_MANAGER"), \
             mock.patch("sys.argv", ["web_app.py", "--no-browser"]):
            web_app.main()
        events = [call.args[1] for call in emit.call_args_list]
        self.assertIn("app_startup", events)
        self.assertIn("app_shutdown", events)

    def test_main_on_startup_rescan_failure(self):
        server = mock.Mock()
        server.server_address = ("127.0.0.1", 54321)
        server.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(web_app, "handle_cli", return_value=None), \
             mock.patch.object(web_app, "update_state"), \
             mock.patch.object(web_app, "load_state", return_value={"settings": {}}), \
             mock.patch.object(
                 web_app, "reconcile_sessions_on_startup", return_value=([], [])
             ), \
             mock.patch.object(web_app, "ensure_stock_themes"), \
             mock.patch.object(web_app, "threading"), \
             mock.patch.object(web_app, "ThreadingHTTPServer", return_value=server), \
             mock.patch.object(web_app, "run_configured_commands"), \
             mock.patch.object(web_app, "emit_plugin_event"), \
             mock.patch.object(web_app, "secure_text_write"), \
             mock.patch.object(
                 web_app, "bigbox_launch_url", side_effect=lambda url: url
             ), \
             mock.patch.object(web_app, "is_gamescope_guest", return_value=False), \
             mock.patch.object(web_app, "shutdown_webhooks"), \
             mock.patch(
                 "pkg.parity.parity_library_health.normalize_rescan_setting",
                 side_effect=RuntimeError("boom"),
             ), \
             mock.patch.object(web_app, "JOB_MANAGER"), \
             mock.patch("sys.argv", ["web_app.py", "--no-browser"]):
            web_app.main()


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
