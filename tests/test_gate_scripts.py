#!/usr/bin/env python3
"""Table-driven behavior tests for the gate scripts.

The gate scripts are the product that keeps regressions out; until 1.13 they
were only asserted by string matching. These tests exercise their actual
behavior: counting functions, failure modes, and CI detection. Scripts that
read the repository tree are pointed at a temporary tree via their ``ROOT``
module global.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_csp, check_frontend, check_runtime_modules, check_tokens, check_version_sync  # noqa: E402


def _capture(func, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


class CheckTokensBehaviorTests(unittest.TestCase):
    def test_count_outside_root_ignores_token_block(self):
        css = ":root { --text: #fff; --bg: #112233; }\n.card { color: #abc; }"
        self.assertEqual(check_tokens.count_outside_root(css), 1)

    def test_count_raw_hex_counts_all_forms(self):
        text = "<b style='color:#fff'>x</b><script>let c='#11223344'; let d='#abcd';</script>"
        self.assertEqual(check_tokens.count_raw_hex(text), 3)

    def test_count_raw_hex_ignores_non_colors(self):
        # `#id` selectors and word fragments must not be counted as colors.
        text = "document.querySelector('#app'); hash = 'deadbeef'; tag = '#fragment';"
        self.assertEqual(check_tokens.count_raw_hex(text), 0)

    def _main_with(self, css_text: str, markup_text: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            css = tmp_path / "app.css"
            css.write_text(css_text, encoding="utf-8")
            markup = tmp_path / "index.html"
            markup.write_text(markup_text, encoding="utf-8")
            with mock.patch.object(check_tokens, "_css_files", return_value=[css]), \
                 mock.patch.object(check_tokens, "_markup_files", return_value=[markup]):
                return _capture(check_tokens.main)

    def test_main_passes_when_values_stay_under_baseline(self):
        code, out, _ = self._main_with(".a { color: var(--text); }", "<p>plain</p>")
        self.assertEqual(code, 0)
        self.assertIn("baseline", out)

    def test_main_fails_when_markup_gains_raw_hex(self):
        code, _, err = self._main_with(".a { color: var(--text); }", "<p style='color:#f00'>x</p>")
        self.assertEqual(code, 1)
        self.assertIn("static/app.css", err)


class CheckCspBehaviorTests(unittest.TestCase):
    WEB_APP = (
        'CSP_DEFAULT = "default-src \'self\'; frame-ancestors \'none\'"\n'
        'CSP_FRAMEABLE = CSP_DEFAULT.replace("frame-ancestors \'none\'", "frame-ancestors \'self\'")\n'
        'HEADERS = ("X-Frame-Options", "SAMEORIGIN" if frameable else "DENY")\n'
    )

    def _failures_with(self, web_app: str, extra_handler: str = "") -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "web_app.py").write_text(web_app, encoding="utf-8")
            handlers = root / "handlers"
            handlers.mkdir()
            (handlers / "data.py").write_text("send(frameable=" + "True)\n", encoding="utf-8")
            if extra_handler:
                (handlers / "other.py").write_text(extra_handler, encoding="utf-8")
            with mock.patch.object(check_csp, "ROOT", root):
                return check_csp._failures()

    def test_clean_contract_passes(self):
        self.assertEqual(self._failures_with(self.WEB_APP), [])

    def test_default_must_keep_frame_ancestors_none(self):
        broken = self.WEB_APP.replace("frame-ancestors 'none'", "frame-ancestors 'self'")
        failures = self._failures_with(broken)
        self.assertTrue(any("CSP_DEFAULT" in failure for failure in failures))

    def test_frameable_call_site_must_live_in_data_handler(self):
        failures = self._failures_with(self.WEB_APP, "send(frameable=" + "True)\n")
        self.assertTrue(any("handlers/other.py" in failure for failure in failures))

    def test_frameable_relaxation_must_stay_single_directive(self):
        broken = self.WEB_APP.replace(
            'CSP_DEFAULT.replace("frame-ancestors \'none\'", "frame-ancestors \'self\'")',
            'CSP_DEFAULT.replace("frame-ancestors \'none\'", "frame-ancestors \'self\'; default-src *")',
        )
        failures = self._failures_with(broken)
        self.assertTrue(any("CSP_FRAMEABLE" in failure for failure in failures))


class CheckVersionSyncBehaviorTests(unittest.TestCase):
    VERSION = "1.2.3"

    def _tree(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / ".github" / "ISSUE_TEMPLATE").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "scripts").mkdir()
        (root / "README.md").write_text(
            "img.shields.io/badge/Release-v1.2.3\nLatest stable: v1.2.3\nVERSION=1.2.3\n",
            encoding="utf-8",
        )
        (root / "openbox.metainfo.xml").write_text('<release version="1.2.3" date="2026-01-01">', encoding="utf-8")
        (root / "docs" / "PARITY.md").write_text("lead\nlead\nlatest is **v1.2.3** here\n", encoding="utf-8")
        (root / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").write_text("version: v1.2.3", encoding="utf-8")
        (root / "docs" / "SUPPORT.md").write_text("OpenBox 1.2.3", encoding="utf-8")
        (root / "docs" / "SECURITY.md").write_text("1.2.x supported", encoding="utf-8")
        (root / "docs" / "flathub-checklist.md").write_text("OpenBox 1.2.3", encoding="utf-8")
        (root / "docs" / "CHANGELOG.md").write_text("## [1.2.3]\n- did things\n", encoding="utf-8")
        (root / "docs" / "README.md").write_text("shipped **v1.2.3**", encoding="utf-8")
        (root / "docs" / "RELEASE_NOTES.md").write_text("compare ...v1.2.3", encoding="utf-8")
        (root / "scripts" / "gen_sbom.py").write_text('DEFAULT_VERSION = "1.2.3"', encoding="utf-8")
        return root

    def test_check_passes_on_consistent_tree(self):
        root = self._tree()
        with mock.patch.object(check_version_sync, "ROOT", root):
            self.assertEqual(check_version_sync.check(self.VERSION), [])

    def test_check_reports_each_stale_surface(self):
        cases = {
            "README badge": ("README.md", "v1.2.3", "v9.9.9"),
            "PARITY lead": ("docs/PARITY.md", "v1.2.3", "v8.8.8"),
            "metainfo latest release": ("openbox.metainfo.xml", "1.2.3", "7.7.7"),
            "CHANGELOG": ("docs/CHANGELOG.md", "1.2.3", "6.6.6"),
            "CHANGELOG section": ("docs/CHANGELOG.md", None, "mentions 1.2.3 but has no heading\n"),
            "bug report template": (".github/ISSUE_TEMPLATE/bug_report.yml", "v1.2.3", "v5.5.5"),
        }
        for label, (rel, old, new) in cases.items():
            with self.subTest(label=label):
                root = self._tree()
                path = root / rel
                if old is None:
                    path.write_text(new, encoding="utf-8")
                else:
                    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                with mock.patch.object(check_version_sync, "ROOT", root):
                    failures = check_version_sync.check(self.VERSION)
                self.assertIn(label, failures)


class CheckRuntimeModulesBehaviorTests(unittest.TestCase):
    REQUIRED = {
        "a.py": "",
        "handlers/b.py": "",
        "pkg/__init__.py": "",
        "pkg/state/s.py": "",
        "pkg/parity/p.py": "",
        "routes/r.py": "",
        "routes/nested/n.py": "",
    }
    MANIFEST = "\n".join(REQUIRED) + "\n"

    def _tree(self, manifest: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel in self.REQUIRED:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        manifest_path = root / "runtime_modules.txt"
        manifest_path.write_text(manifest, encoding="utf-8")
        return root

    def _run(self, manifest: str) -> tuple[int, str]:
        root = self._tree(manifest)
        with mock.patch.object(check_runtime_modules, "ROOT", root), \
             mock.patch.object(check_runtime_modules, "MANIFEST", root / "runtime_modules.txt"):
            code, out, err = _capture(check_runtime_modules.main)
        return code, out + err

    def test_clean_manifest_passes(self):
        code, output = self._run(self.MANIFEST)
        self.assertEqual(code, 0, output)
        self.assertIn("OK", output)

    def test_duplicate_entries_fail(self):
        code, output = self._run(self.MANIFEST + "a.py\n")
        self.assertEqual(code, 1)
        self.assertIn("duplicate", output)

    def test_listed_missing_file_fails(self):
        code, output = self._run(self.MANIFEST + "ghost.py\n")
        self.assertEqual(code, 1)
        self.assertIn("missing files", output)

    def test_unlisted_required_module_fails(self):
        manifest = "\n".join(line for line in self.MANIFEST.splitlines() if line != "handlers/b.py") + "\n"
        code, output = self._run(manifest)
        self.assertEqual(code, 1)
        self.assertIn("handlers/b.py", output)


class CheckFrontendBehaviorTests(unittest.TestCase):
    def test_in_ci_detection(self):
        for value, expected in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"CI": value}, clear=False):
                self.assertEqual(check_frontend._in_ci(), expected)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(check_frontend._in_ci())

    def _main_with(self, static_content: str | None, eslint_ok: bool, tsc_ok: bool) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if static_content is not None:
                static = root / "static"
                static.mkdir()
                (static / "app.js").write_text(static_content, encoding="utf-8")
            with mock.patch.object(check_frontend, "ROOT", root), \
                 mock.patch.object(check_frontend, "check_eslint", return_value=eslint_ok), \
                 mock.patch.object(check_frontend, "check_tsc", return_value=tsc_ok):
                code, _out, _err = _capture(check_frontend.main)
            return code

    def test_missing_static_dir_fails(self):
        self.assertEqual(self._main_with(None, True, True), 1)

    def test_all_checks_pass(self):
        self.assertEqual(self._main_with("const a = 1;\n", True, True), 0)

    def test_lint_failure_fails(self):
        self.assertEqual(self._main_with("const a = 1;\n", False, True), 1)

    def test_typecheck_failure_fails(self):
        self.assertEqual(self._main_with("const a = 1;\n", True, False), 1)


if __name__ == "__main__":
    unittest.main()
