#!/usr/bin/env python3
"""Behavior tests for the docs-link and generated-api gates."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import check_docs_links, gen_api_docs  # noqa: E402


def _capture(func, *args, **kwargs):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = func(*args, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


class DocsLinksTests(unittest.TestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        (tmp / "other.md").write_text("target\n", encoding="utf-8")
        doc = tmp / "doc.md"
        doc.write_text(text, encoding="utf-8")
        return doc

    def test_resolving_link_passes_and_missing_link_fails(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc = self._write(Path(tmp_dir), "[ok](other.md)\n[bad](missing.md)\n")
            failures = check_docs_links.broken_for(doc)
        self.assertEqual(len(failures), 1)
        self.assertIn("missing.md", failures[0])

    def test_external_anchor_and_mailto_targets_are_ignored(self):
        text = (
            "[ext](https://example.com/x)\n"
            "[anchor](#section)\n"
            "[mail](mailto:dev@example.com)\n"
            "[frag](other.md#part)\n"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc = self._write(Path(tmp_dir), text)
            self.assertEqual(check_docs_links.broken_for(doc), [])

    def test_reference_style_definitions_are_checked(self):
        text = "see [target][ref]\n\n[ref]: other.md\n[gone]: nowhere.md\n"
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc = self._write(Path(tmp_dir), text)
            failures = check_docs_links.broken_for(doc)
        self.assertEqual(len(failures), 1)
        self.assertIn("nowhere.md", failures[0])

    def test_main_exits_nonzero_with_broken_links(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc = self._write(Path(tmp_dir), "[bad](missing.md)\n")
            with mock.patch.object(check_docs_links, "markdown_files", return_value=[doc]):
                code, _out, err = _capture(check_docs_links.main)
        self.assertEqual(code, 1)
        self.assertIn("broken link", err)

    def test_main_exits_zero_when_all_links_resolve(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            doc = self._write(Path(tmp_dir), "[ok](other.md)\n")
            with mock.patch.object(check_docs_links, "markdown_files", return_value=[doc]):
                code, _out, _err = _capture(check_docs_links.main)
        self.assertEqual(code, 0)


class ApiDocsTests(unittest.TestCase):
    def test_render_is_deterministic(self):
        self.assertEqual(gen_api_docs.render(), gen_api_docs.render())

    def test_render_contains_v2_section_and_frozen_v1(self):
        text = gen_api_docs.render()
        self.assertIn("## API v2", text)
        self.assertIn("## API v1 (frozen)", text)
        self.assertIn("| Method | Path | Handler |", text)
        self.assertIn("| Method | Path | Handler | Response |", text)
        self.assertIn("`/api/v1/settings`", text)

    def test_check_passes_for_fresh_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "api-v2.md"
            target.write_text(gen_api_docs.render(), encoding="utf-8")
            code, _out, _err = _capture(gen_api_docs.main, ["--out", str(target), "--check"])
        self.assertEqual(code, 0)

    def test_check_fails_for_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "api-v2.md"
            target.write_text("stale\n", encoding="utf-8")
            code, _out, err = _capture(gen_api_docs.main, ["--out", str(target), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("stale", err)

    def test_check_fails_for_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "api-v2.md"
            code, _out, err = _capture(gen_api_docs.main, ["--out", str(target), "--check"])
        self.assertEqual(code, 1)
        self.assertIn("missing", err)

    def test_generated_rows_are_sorted_by_path(self):
        rows = gen_api_docs.v2_routes()
        self.assertTrue(rows)
        paths = [path for _method, path, _spec in rows]
        self.assertEqual(paths, sorted(paths))
        keys = [(method, path) for method, path, _spec in rows]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
