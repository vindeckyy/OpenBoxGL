#!/usr/bin/env python3
"""The dependency gate must be able to fail.

`scripts/check_dependencies.py` exists because a guarded `import orjson` in the
state store made the app's most important write path behave differently
depending on whether a third-party package happened to be installed -- and
because the tests that "covered" it all began with `skipTest`, so CI never ran
any of them.

That is the ADR 0064 rule 3 hazard: a gate whose assertions can be skipped by
their own input is not a gate. So this file tests the gate itself, in both
directions, by driving its real parser over synthetic sources.
"""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_dependencies_under_test", ROOT / "scripts" / "check_dependencies.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()


def _imports(source: str) -> list[tuple[str, int, bool]]:
    return GATE._collect(ast.parse(source), source)


class GuardedDetectionTests(unittest.TestCase):
    def test_import_inside_try_is_marked_guarded(self):
        source = "try:\n    import orjson\nexcept ImportError:\n    orjson = None\n"
        found = _imports(source)
        self.assertEqual([name for name, _, _ in found], ["orjson"])
        self.assertTrue(found[0][2], "an import inside try/except ImportError must be marked guarded")

    def test_bare_import_is_not_marked_guarded(self):
        found = _imports("import orjson\n")
        self.assertEqual(found, [("orjson", 1, False)])

    def test_bare_except_counts_as_guarded(self):
        source = "try:\n    import orjson\nexcept:\n    orjson = None\n"
        self.assertTrue(_imports(source)[0][2])

    def test_except_of_unrelated_error_is_not_guarded(self):
        source = "try:\n    import orjson\nexcept ValueError:\n    orjson = None\n"
        self.assertFalse(_imports(source)[0][2])

    def test_relative_imports_are_always_local(self):
        self.assertEqual(_imports("from . import sibling\nfrom .pkg import thing\n"), [])

    def test_dotted_import_reports_top_level_only(self):
        self.assertEqual([name for name, _, _ in _imports("import os.path\n")], ["os"])

    def test_from_import_reports_module(self):
        self.assertEqual([name for name, _, _ in _imports("from json import dumps\n")], ["json"])


class RepositoryIsCleanTests(unittest.TestCase):
    def test_runtime_code_has_no_unreviewed_third_party_imports(self):
        """The real repository must satisfy the rule the gate enforces."""
        local = GATE._local_names(
            [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
        )
        allowed = local | GATE._stdlib() | GATE.LOCAL_PACKAGES | {"__future__"}

        offenders: list[str] = []
        for entry in [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines() if line.strip()]:
            if not entry.endswith(".py"):
                continue
            path = ROOT / entry
            self.assertTrue(path.is_file(), f"runtime module missing: {entry}")
            source = path.read_text(encoding="utf-8")
            for module, lineno, guarded in GATE._collect(ast.parse(source), source):
                if module in allowed or module in GATE.REVIEWED_OPTIONAL:
                    continue
                offenders.append(f"{entry}:{lineno} imports {module}{' [guarded]' if guarded else ''}")

        self.assertEqual(
            offenders,
            [],
            "unreviewed third-party imports in runtime code:\n  " + "\n  ".join(offenders),
        )

    def test_orjson_is_not_imported_anywhere_in_runtime_code(self):
        """The specific regression that motivated this gate.

        Asserted by module name rather than by reading the gate's allowlist, so
        it still fails if orjson is ever re-added to REVIEWED_OPTIONAL by mistake.
        """
        self.assertNotIn(
            "orjson",
            GATE.REVIEWED_OPTIONAL,
            "orjson must never be a reviewed optional import: it changes the "
            "serializer on the state write path",
        )
        for entry in [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines() if line.strip()]:
            if not entry.endswith(".py"):
                continue
            source = (ROOT / entry).read_text(encoding="utf-8")
            for module, _, _ in GATE._collect(ast.parse(source), source):
                self.assertNotEqual(module, "orjson", f"{entry} imports orjson")

    def test_reviewed_optional_imports_are_actually_guarded(self):
        """A reviewed optional import must never be able to stop the app starting."""
        for entry in [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines() if line.strip()]:
            if not entry.endswith(".py"):
                continue
            source = (ROOT / entry).read_text(encoding="utf-8")
            for module, lineno, guarded in GATE._collect(ast.parse(source), source):
                if module in GATE.REVIEWED_OPTIONAL:
                    self.assertTrue(
                        guarded,
                        f"{entry}:{lineno} imports reviewed optional {module!r} "
                        "without try/except ImportError; a missing package would "
                        "prevent the app from starting",
                    )


if __name__ == "__main__":
    unittest.main()
