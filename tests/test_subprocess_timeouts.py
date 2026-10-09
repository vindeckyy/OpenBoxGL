#!/usr/bin/env python3
"""Every runtime subprocess.run/call/check_output must be bounded by a timeout.

A probe that waits forever on a wedged Flatpak, D-Bus or taskkill session blocks
the HTTP request that asked for it, and the UI spins with it. The 1.16.0 notes
claimed the Flatpak probes were bounded; only the two Launch Doctor probes were.

The allowance below is a ratchet: it names the calls that are knowingly
unbounded, with a reason, and it must match the code exactly. A new unbounded
call fails the gate, and fixing one fails it too until its allowance is removed.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# file -> number of unbounded subprocess calls allowed in it.
ALLOWED_UNBOUNDED = {
    # The Windows update applier verifies and stages a whole install; a fixed
    # timeout would abort a legitimate slow disk mid-update.
    "updates.py": 1,
}

SUBPROCESS_CALLS = {"run", "call", "check_output", "check_call"}


def _runtime_python_files():
    listed = [line.strip() for line in (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines()]
    for entry in listed:
        if entry and not entry.startswith("#") and entry.endswith(".py"):
            path = ROOT / entry
            if path.is_file():
                yield entry, path


def _unbounded_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess" and func.attr in SUBPROCESS_CALLS):
            continue
        if any(keyword.arg == "timeout" for keyword in node.keywords):
            continue
        found.append((node.lineno, f"subprocess.{func.attr}"))
    return found


class SubprocessTimeoutRatchetTests(unittest.TestCase):
    def test_unbounded_subprocess_calls_match_the_allowance_exactly(self):
        problems = []
        seen = {}
        for rel, path in _runtime_python_files():
            calls = _unbounded_calls(path)
            if calls:
                seen[rel] = calls
            allowed = ALLOWED_UNBOUNDED.get(rel, 0)
            if len(calls) > allowed:
                problems.append(f"{rel}: {len(calls)} unbounded call(s), {allowed} allowed -> {calls}")
            elif len(calls) < allowed:
                problems.append(f"{rel}: now {len(calls)} unbounded call(s); lower ALLOWED_UNBOUNDED to match")
        for rel in ALLOWED_UNBOUNDED:
            if not (ROOT / rel).is_file():
                problems.append(f"{rel} is allowed but no longer exists; remove it from ALLOWED_UNBOUNDED")
        self.assertEqual(problems, [], f"unbounded subprocess calls (seen: {seen})")


class FlatpakProbeTests(unittest.TestCase):
    def test_a_timed_out_probe_counts_as_not_installed(self):
        from pkg.platform_compat import flatpak_app_installed

        def hung(*_args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="flatpak info", timeout=kwargs.get("timeout"))

        self.assertFalse(flatpak_app_installed("org.test.App", run=hung, flatpak="/usr/bin/flatpak"))

    def test_the_probe_is_bounded_and_reads_the_exit_code(self):
        from pkg.platform_compat import FLATPAK_PROBE_TIMEOUT_SECONDS, flatpak_app_installed

        seen = {}

        def ok(args, **kwargs):
            seen.update(kwargs, args=args)
            return subprocess.CompletedProcess(args, 0)

        self.assertTrue(flatpak_app_installed("org.test.App", run=ok, flatpak="/usr/bin/flatpak"))
        self.assertEqual(seen["timeout"], FLATPAK_PROBE_TIMEOUT_SECONDS)
        self.assertEqual(seen["args"], ["/usr/bin/flatpak", "info", "org.test.App"])

        def missing(args, **_kwargs):
            return subprocess.CompletedProcess(args, 1)

        self.assertFalse(flatpak_app_installed("org.test.App", run=missing, flatpak="/usr/bin/flatpak"))

    def test_status_does_not_probe_flatpak_for_an_emulator_that_is_native(self):
        from emulators import EMULATORS, emulator_status

        native_apps = {app_id: item["native"] for app_id, item in EMULATORS.items() if item.get("native")}
        self.assertTrue(native_apps, "fixture needs at least one emulator with a native binary")
        native_app, native_exe = next(iter(native_apps.items()))
        probed = []

        def recorder(args, **_kwargs):
            probed.append(list(args))
            return subprocess.CompletedProcess(args, 1)

        def which(name):
            return f"/usr/bin/{name}" if name in {"flatpak", native_exe} else None

        emulator_status(run=recorder, which=which)
        self.assertNotIn(["/usr/bin/flatpak", "info", native_app], probed)
        self.assertTrue(all(args[:2] == ["/usr/bin/flatpak", "info"] for args in probed), probed)


if __name__ == "__main__":
    unittest.main()
