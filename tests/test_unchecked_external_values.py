#!/usr/bin/env python3
"""Two unbounded/unchecked values that reach a subprocess and a filename.

**S5 -- a remote API field became a cache filename.** ``retroachievements``
interpolated ``console["ID"]`` straight into ``system-{ID}.json`` while the
sibling field on the very next block was already coerced with ``int()``. A
non-numeric ID containing a separator would place a file outside the cache
directory.

**S6 -- two subprocess calls had no timeout.** ``flatpak info`` was invoked
from the per-game preflight path with no ``timeout=``, so a stalled D-Bus or a
broken Flatpak installation hangs the request thread indefinitely. Both calls
run per game, so an audit would hang once per game rather than once.

A timeout is resolved in the safe direction: an emulator whose permissions
cannot be read is reported as *not granted*. That can block a launch, never
enable one.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import retroachievements
from pkg.parity import parity_launch_doctor as doctor


class FlatpakTimeoutTests(unittest.TestCase):
    def test_timeout_is_bounded(self):
        self.assertGreater(doctor.FLATPAK_TIMEOUT_SECONDS, 0)
        self.assertLessEqual(doctor.FLATPAK_TIMEOUT_SECONDS, 30, "a preflight must not block for long")

    def test_installed_check_passes_a_timeout(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0)

        doctor._flatpak_installed("org.example.Emu", which=lambda _: "/usr/bin/flatpak", run=fake_run)
        self.assertIn("timeout", seen, "flatpak info had no timeout")
        self.assertEqual(seen["timeout"], doctor.FLATPAK_TIMEOUT_SECONDS)

    def test_permissions_check_passes_a_timeout(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="filesystem=host")

        doctor._flatpak_fs_allowed("org.example.Emu", "/games/x.nes", which=lambda _: "/usr/bin/flatpak", run=fake_run)
        self.assertIn("timeout", seen, "flatpak info --show-permissions had no timeout")

    def test_a_timeout_is_treated_as_not_granted_not_as_allowed(self):
        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        allowed = doctor._flatpak_fs_allowed(
            "org.example.Emu", "/games/x.nes", which=lambda _: "/usr/bin/flatpak", run=hang
        )
        self.assertFalse(allowed, "an unreadable permission set must not be treated as granted")

    def test_a_timeout_does_not_propagate_out_of_preflight(self):
        """The whole point: a hung flatpak must not hang the caller."""

        def hang(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            rom = data_dir / "game.nes"
            rom.write_bytes(b"NES")
            checks = doctor.run_preflight_checks(
                {"path": str(rom), "platform": "NES", "emulator_id": "retroarch"},
                {},
                str(data_dir),
                which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None,
                run=hang,
            )
        self.assertIsInstance(checks, list, "preflight must return rather than raise")
        for check in checks:
            self.assertIn(check.get("severity"), ("error", "warning", "info"))


class RetroAchievementsIdTests(unittest.TestCase):
    """The remote system ID must be an integer before it becomes a filename."""

    def _resolve(self, console_id, cache_dir):
        """Drive the real ``match_game`` with a seeded RetroAchievements reply.

        ``systems.json`` is pre-seeded because that is the shape of the cache the
        remote call populates: a list of ``{"ID": ..., "Name": ...}`` dicts. The
        fetch stub then serves the (empty) per-system game list.
        """
        cache_dir = Path(cache_dir)
        (cache_dir / "systems.json").write_text(
            json.dumps([{"ID": console_id, "Name": "Nintendo Entertainment System"}]),
            encoding="utf-8",
        )
        rom = cache_dir / "game.nes"
        rom.write_bytes(b"NES\x1a" + b"\x00" * 32)
        captured = {"calls": []}

        def fake_fetch(endpoint, params, credentials):
            captured["calls"].append((endpoint, dict(params)))
            return []

        try:
            captured["result"] = retroachievements.match_game(
                {"path": str(rom), "platform": "nes"},
                {},
                str(cache_dir),
                fetch=fake_fetch,
            )
        except ValueError as error:
            captured["error"] = str(error)
        return captured

    def test_a_numeric_id_is_accepted_and_becomes_the_request_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            captured = self._resolve(7, cache_dir)
            written = sorted(path.name for path in cache_dir.glob("system-*.json"))
        self.assertNotIn("unusable", captured.get("error", ""), captured.get("error"))
        # The empty game list means no match, which is a *lookup* outcome, not a
        # rejection of the id itself.
        self.assertEqual(captured.get("error"), "This ROM hash is not linked to a RetroAchievements game.")
        self.assertEqual(written, ["system-7.json"])
        endpoint, params = captured["calls"][-1]
        self.assertEqual(endpoint, "API_GetGameList.php")
        self.assertEqual(params["i"], 7, "the request parameter must be the coerced integer")

    def test_a_traversal_id_is_rejected_before_it_names_a_file(self):
        for hostile in ("../../escape", "1/../../etc", "..", "abc", None, "", "7; rm -rf /"):
            with self.subTest(system_id=hostile):
                with tempfile.TemporaryDirectory() as directory:
                    parent = Path(directory)
                    cache_dir = parent / "cache"
                    cache_dir.mkdir()
                    captured = self._resolve(hostile, cache_dir)
                    escaped = sorted(
                        path.relative_to(parent).as_posix()
                        for path in parent.rglob("*")
                        if path.is_file() and path.name not in {"game.nes", "systems.json"}
                    )
                self.assertIn(
                    "error",
                    captured,
                    f"a non-numeric system ID {hostile!r} was accepted as a cache filename",
                )
                self.assertIn("unusable", captured["error"])
                self.assertEqual(escaped, [], f"system ID {hostile!r} wrote outside the cache directory")

    def test_a_float_id_truncates_rather_than_reaching_the_filename(self):
        """``int()`` is the whole guarantee, so state it as one.

        A float cannot become a path separator, and ``int(7.5)`` is 7 -- the
        filename stays ``system-7.json`` inside the cache directory rather than
        ``system-7.5.json``. The lookup then fails as an ordinary miss, which is
        the correct outcome for a malformed id.
        """
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            captured = self._resolve(7.5, cache_dir)
            written = sorted(path.name for path in cache_dir.glob("system-*"))
        self.assertEqual(written, ["system-7.json"], "a float must not survive into the filename")
        self.assertNotIn("unusable", captured.get("error", ""))

    def test_an_absent_id_is_rejected(self):
        """``console['ID']`` missing entirely is a KeyError, not a crash."""
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            (cache_dir / "systems.json").write_text(
                json.dumps([{"Name": "Nintendo Entertainment System"}]), encoding="utf-8"
            )
            rom = cache_dir / "game.nes"
            rom.write_bytes(b"NES\x1a" + b"\x00" * 32)
            with self.assertRaises(ValueError) as caught:
                retroachievements.match_game(
                    {"path": str(rom), "platform": "nes"}, {}, str(cache_dir), fetch=lambda *a: []
                )
        self.assertIn("unusable", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__, "the original KeyError must not leak")


if __name__ == "__main__":
    unittest.main()
