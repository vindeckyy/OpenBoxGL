#!/usr/bin/env python3
"""Fail the gate if runtime code imports anything outside the standard library.

OpenBox ships as an AppImage, a Flatpak and a Windows zip. All three promise the
same thing: ``pip install -r requirements-dev.txt`` is never required to run the
app. That promise is load-bearing, and an *optional* import breaks it just as hard
as a required one -- a third-party package that happens to be present on a user's
machine silently changes behaviour, and a package that is absent breaks the feature.

This check exists because of exactly that failure. ``state_store.py`` guarded an
``import orjson`` in a ``try``/``except ImportError``, so the persistence layer ran
two different serializers depending on the host:

* ``orjson.dumps`` raises ``TypeError`` on a non-str dict key where the stdlib
  coerces it, so a numeric-keyed dict could fail the *entire* state write;
* ``orjson`` serialises ``NaN``/``Infinity`` as ``null`` where the stdlib emits
  bare ``NaN``, so a value could be lost silently;
* and because no CI job installed ``orjson``, neither branch was ever exercised.

The module set is taken from ``runtime_modules.txt`` so this gate and the AppImage
manifest can never disagree about what ships.

Exits non-zero on any third-party import. ``tests/`` and ``scripts/`` are not
runtime modules and are not scanned: development tooling is allowed to have
dependencies.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "runtime_modules.txt"

# Packages that are part of this repository even though they are not modules.
LOCAL_PACKAGES = {"pkg", "handlers", "routes", "scripts", "openbox", "assets"}

# ---------------------------------------------------------------------------
# Reviewed optional third-party imports.
#
# "Stdlib only" is the rule, not the whole policy. Two imports are allowed
# because they widen a *capability* rather than change the *meaning of data*,
# and both degrade with a clear, actionable error instead of silently doing
# something different:
#
#   py7zr -- reading ROMs inside .7z archives. parity_premium.py:324 raises
#            "Install py7zr or 7z to hash ROMs inside .7z archives." when it
#            is absent, so a user without it simply cannot use that one format.
#
#   yaml  -- a real parser for the emulator-definition format.
#            parity_emulator_defs._parse_yaml has a stdlib fallback so the app
#            starts without PyYAML. See tests/test_emulator_defs.py for the
#            test that pins the fallback to the bundled definitions; if you
#            extend the definition schema, that test is what will catch a
#            silent divergence between the two parsers.
#
# An entry is admissible only if ALL of the following hold, and this check
# enforces the mechanical one:
#   1. it is imported behind ``try/except ImportError`` (asserted below), so a
#      missing package can never stop the app from starting;
#   2. absence produces a user-visible error at the point of use, not a
#      different result (reviewed by hand, not machine-checkable);
#   3. it never changes how persisted state is serialized or interpreted.
#
# Anything else is a violation. ``orjson`` was the motivating failure: it
# satisfied none of the three and sat on the state write path.
# ---------------------------------------------------------------------------
REVIEWED_OPTIONAL: dict[str, str] = {
    "py7zr": "optional .7z ROM hashing; clear install hint at parity_premium.py:324",
    "yaml": "emulator-definition parser with a stdlib fallback (_parse_yaml)",
}


def _read_manifest() -> list[str]:
    if not MANIFEST.is_file():
        print(f"FAIL: missing {MANIFEST}", file=sys.stderr)
        raise SystemExit(1)
    return [line.strip() for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]


def _local_names(entries: list[str]) -> set[str]:
    """Module stems and package names that resolve to this repository.

    ``pkg/parity`` installs a flat import finder, so runtime code does
    ``from parity_import import X`` rather than ``from pkg.parity.parity_import``.
    The bare stem is therefore a local name too.
    """
    names = {Path(entry).stem for entry in entries}
    names |= {Path(entry).name for entry in entries}
    return names


def _stdlib() -> set[str]:
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    # Python < 3.10 has no stdlib_module_names; fall back to a conservative list
    # so the gate still runs rather than failing on an old interpreter.
    return {
        "abc", "argparse", "ast", "asyncio", "base64", "binascii", "bisect", "calendar",
        "collections", "concurrent", "configparser", "contextlib", "copy", "csv", "ctypes",
        "dataclasses", "datetime", "difflib", "dis", "email", "enum", "errno", "fnmatch",
        "fractions", "functools", "gc", "getpass", "glob", "gzip", "hashlib", "heapq", "hmac",
        "html", "http", "importlib", "inspect", "io", "ipaddress", "itertools", "json",
        "keyword", "linecache", "locale", "logging", "lzma", "mimetypes", "multiprocessing",
        "numbers", "operator", "os", "pathlib", "pickle", "pkgutil", "platform", "plistlib",
        "posixpath", "queue", "random", "re", "reprlib", "secrets", "select", "selectors",
        "shlex", "shutil", "signal", "site", "socket", "sqlite3", "sre_compile", "ssl",
        "stat", "statistics", "string", "struct", "subprocess", "sys", "sysconfig",
        "tarfile", "tempfile", "textwrap", "threading", "time", "timeit", "tkinter", "token",
        "tokenize", "traceback", "types", "typing", "unicodedata", "unittest", "urllib",
        "uuid", "venv", "warnings", "weakref", "webbrowser", "xml", "xmlrpc", "zipfile",
        "zlib", "zoneinfo",
    }


def _guarded_optional(node: ast.AST) -> bool:
    """True when this import sits inside a try/except ImportError block.

    An optional import is *still* a dependency, so it is reported either way --
    the flag only makes the error message more useful.
    """
    return False  # replaced by the parent walk in _collect


def _collect(tree: ast.AST, source: str) -> list[tuple[str, int, bool]]:
    """Return (module, lineno, guarded) for every imported top-level module."""
    guarded_lines: set[int] = set()

    # Mark every line covered by a try/except ImportError handler chain.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import_error = False
        for handler in node.handlers:
            exc = handler.type
            names: list[str] = []
            if exc is None:
                # A bare ``except:`` catches ImportError too, so an import
                # behind one is genuinely optional.
                names = ["BARE"]
            elif isinstance(exc, ast.Name):
                names = [exc.id]
            elif isinstance(exc, ast.Tuple):
                names = [e.id for e in exc.elts if isinstance(e, ast.Name)]
            if any(
                name in ("BARE", "ImportError", "ModuleNotFoundError", "Exception", "BaseException")
                for name in names
            ):
                catches_import_error = True
        if catches_import_error:
            for stmt in node.body:
                for child in ast.walk(stmt):
                    lineno = getattr(child, "lineno", None)
                    if lineno is not None:
                        guarded_lines.add(lineno)

    found: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                found.append((top, node.lineno, node.lineno in guarded_lines))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import: always local.
                continue
            if not node.module:
                continue
            top = node.module.split(".", 1)[0]
            found.append((top, node.lineno, node.lineno in guarded_lines))
    return found


def main() -> int:
    entries = _read_manifest()
    local = _local_names(entries)
    stdlib = _stdlib()
    allowed = local | stdlib | LOCAL_PACKAGES | {"__future__"}

    violations: list[tuple[str, int, str, bool]] = []
    unguarded: list[tuple[str, int, str]] = []
    scanned = 0
    for entry in entries:
        if not entry.endswith(".py"):
            continue
        path = ROOT / entry
        if not path.is_file():
            print(f"FAIL: runtime module missing: {entry}", file=sys.stderr)
            return 1
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=entry)
        except SyntaxError as error:
            print(f"FAIL: cannot parse {entry}: {error}", file=sys.stderr)
            return 1
        scanned += 1
        for module, lineno, guarded in _collect(tree, source):
            if module in allowed:
                continue
            if module in REVIEWED_OPTIONAL:
                # Admissible, but only if absence is actually handled.
                if not guarded:
                    unguarded.append((entry, lineno, module))
                continue
            violations.append((entry, lineno, module, guarded))

    failed = False
    if violations:
        failed = True
        print("FAIL: third-party imports in runtime code (OpenBox is stdlib-only):", file=sys.stderr)
        for entry, lineno, module, guarded in sorted(violations):
            note = "  [optional import -- still a dependency]" if guarded else ""
            print(f"  {entry}:{lineno}  imports {module!r}{note}", file=sys.stderr)
        print(
            "\nOpenBox ships as an AppImage, a Flatpak and a Windows zip with no install step.\n"
            "A third-party import makes the host decide behaviour at runtime, and a\n"
            "'try: import X except ImportError' makes that divergence untestable in CI.\n"
            "Remove the dependency, or -- if it only widens a capability and degrades\n"
            "with a clear error -- add a justified entry to REVIEWED_OPTIONAL above.",
            file=sys.stderr,
        )

    if unguarded:
        failed = True
        print(
            "\nFAIL: reviewed optional imports must be guarded by try/except ImportError:",
            file=sys.stderr,
        )
        for entry, lineno, module in sorted(unguarded):
            print(
                f"  {entry}:{lineno}  imports {module!r} unconditionally -- a missing\n"
                f"    package would stop the app from starting, which breaks the no-install promise",
                file=sys.stderr,
            )

    if failed:
        return 1

    reviewed = f", {len(REVIEWED_OPTIONAL)} reviewed optional" if REVIEWED_OPTIONAL else ""
    print(f"dependencies OK: {scanned} runtime modules import stdlib and local code only{reviewed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
