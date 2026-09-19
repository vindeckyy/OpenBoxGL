#!/usr/bin/env python3
"""Tests for the cross-platform shim (``pkg/platform_compat.py``).

The module is the single seam for every platform difference, so the Linux gate
and the Windows CI job share these tests: each assertion either holds on both
platforms or is explicitly gated to the platform whose behaviour it pins.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg import platform_compat as pc  # noqa: E402


def _spawn_child(script, *, cwd=None):
    """Start a detached python child that the caller must always terminate."""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **pc.launch_kwargs(),
    )


def _dead_child_pid():
    """Spawn a child, reap it, and return its now-dead pid."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    return child.pid


def _wait_dead(child, *, timeout=10.0):
    """Wait for *child* to exit and reap it.

    A terminated POSIX child stays a zombie - still a live pid, so still
    ``process_alive`` - until its parent collects it, so the assertion has to
    reap through the ``Popen`` rather than poll the pid.
    """
    try:
        child.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


class PlatformFlagTests(unittest.TestCase):
    def test_flags_match_os_name(self):
        self.assertEqual(pc.IS_WINDOWS, os.name == "nt")
        self.assertEqual(pc.IS_POSIX, os.name == "posix")
        self.assertNotEqual(pc.IS_WINDOWS, pc.IS_POSIX)

    def test_windows_suffixes_cover_launchable_files(self):
        self.assertIn(".exe", pc.WINDOWS_EXECUTABLE_SUFFIXES)
        self.assertIn(".cmd", pc.WINDOWS_EXECUTABLE_SUFFIXES)
        self.assertNotIn(".py", pc.WINDOWS_EXECUTABLE_SUFFIXES)

    def test_platform_label(self):
        expected = "Windows" if pc.IS_WINDOWS else "Linux"
        self.assertEqual(pc.platform_label(), expected)


class DirectoryTests(unittest.TestCase):
    def test_home_dir_matches_path_home(self):
        home = pc.home_dir()
        self.assertIsNotNone(home)
        self.assertEqual(home, Path.home())

    def test_default_data_dir_is_under_the_user_profile(self):
        data_dir = pc.default_data_dir()
        self.assertTrue(data_dir.is_absolute())
        self.assertEqual(data_dir.name, "openbox-game-launcher")
        if not pc.IS_WINDOWS:
            self.assertEqual(data_dir.parent.parent.name, ".local")

    def test_env_file_roots_start_at_home(self):
        roots = pc.env_file_roots()
        self.assertTrue(roots)
        self.assertEqual(roots[0], Path.home())
        if not pc.IS_WINDOWS:
            self.assertIn(Path.home() / ".config" / "openbox-game-launcher", roots)

    def test_home_dir_degrades_when_the_environment_has_no_home(self):
        for error in (RuntimeError("no home"), OSError("no home")):
            with mock.patch.object(pc.Path, "home", side_effect=error):
                self.assertIsNone(pc.home_dir())

    def test_env_file_roots_survive_a_missing_home(self):
        with mock.patch.object(pc, "home_dir", return_value=None):
            roots = pc.env_file_roots()
        if pc.IS_WINDOWS:
            for root in roots:
                self.assertTrue(root.is_absolute())
        else:
            self.assertEqual(roots, [])

    def test_windows_install_dir_matches_the_installer_layout(self):
        install_dir = pc.windows_install_dir()
        if not pc.IS_WINDOWS:
            self.assertIsNone(install_dir)
            return
        # scripts/install.ps1 lifts the release tree to <root>\share\openbox and
        # puts the launcher in <root>, so this path is the bin root they agree on.
        self.assertEqual(install_dir, Path(os.environ["LOCALAPPDATA"]) / "OpenBox")

    def test_start_menu_programs_dir_matches_where_the_shortcut_is_written(self):
        programs = pc.start_menu_programs_dir()
        if not pc.IS_WINDOWS:
            self.assertIsNone(programs)
            return
        self.assertEqual(
            programs,
            Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        )


class ShellCommandTests(unittest.TestCase):
    def test_split_command_of_blank_input(self):
        self.assertEqual(pc.split_command(""), [])
        self.assertEqual(pc.split_command("   "), [])
        self.assertEqual(pc.split_command(None), [])

    def test_split_command_keeps_quoted_arguments_together(self):
        self.assertEqual(pc.split_command('emu "My Game.rom" --flag'), ["emu", "My Game.rom", "--flag"])

    def test_join_command_matches_the_platform_dialect(self):
        parts = ["emu", "My Game.rom"]
        if pc.IS_WINDOWS:
            self.assertEqual(pc.join_command(parts), subprocess.list2cmdline(parts))
        else:
            self.assertEqual(pc.join_command(parts), "emu 'My Game.rom'")

    def test_command_round_trips_through_split(self):
        for parts in (
            ["emu"],
            ["emu", "My Game.rom"],
            ['emu', 'a"b'],
            ["emu", "C:\\Games\\My Game\\rom.bin"],
            ["emu", "", "trailing "],
        ):
            self.assertEqual(pc.split_command(pc.join_command(parts)), parts, parts)

    def test_windows_parser_round_trips_msvcrt_quoting(self):
        """The Windows parser is pure Python, so it is pinned from Linux too."""
        for parts in (
            ["emu"],
            ["emu", "My Game.rom"],
            ['emu', 'quoted "inner" arg'],
            ["emu", "back\\\\slash", "C:\\Games\\rom.bin"],
            ["emu", "", "trailing\\"],
            ["emu", "unicode ✓ name"],
            ["emu", "tab\there"],
            ["emu", "even" + "\\" * 2 + '"' + "quote"],
            ["emu", "trailing" + "\\"],
        ):
            self.assertEqual(pc._split_windows_command(subprocess.list2cmdline(parts)), parts, parts)

    def test_windows_parser_handles_empty_and_unterminated_quotes(self):
        self.assertEqual(pc._split_windows_command(""), [])
        self.assertEqual(pc._split_windows_command('""'), [""])
        self.assertEqual(pc._split_windows_command('emu "open'), ["emu", "open"])

    def test_windows_parser_handles_doubled_and_escaped_quotes(self):
        self.assertEqual(pc._split_windows_command('"a""b"'), ['a"b'])
        self.assertEqual(pc._split_windows_command('a\\\\"b c"'), ["a\\b c"])
        self.assertEqual(pc._split_windows_command('x\\"y"'), ['x"y'])

    def test_executable_paths_resolves_real_programs_only(self):
        found = pc.executable_paths([sys.executable, "openbox-no-such-command-9f3a"])
        self.assertEqual(found, [sys.executable])

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX executability is the mode bit")
    def test_is_executable_uses_the_mode_bit(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "tool"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
            self.assertTrue(pc.is_executable(script))
            script.chmod(0o644)
            self.assertFalse(pc.is_executable(script))
            self.assertFalse(pc.is_executable(Path(directory) / "missing"))

    @unittest.skipUnless(pc.IS_WINDOWS, "Windows decides by file extension")
    def test_is_executable_uses_the_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            tool = Path(directory) / "tool.exe"
            tool.write_bytes(b"")
            self.assertTrue(pc.is_executable(tool))
            self.assertFalse(pc.is_executable(Path(directory) / "tool.txt"))
            self.assertFalse(pc.is_executable(Path(directory) / "missing.exe"))


class LockTests(unittest.TestCase):
    def test_file_lock_creates_the_file_and_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "openbox.lock"
            with pc.file_lock(path):
                self.assertTrue(path.is_file())
            # Re-acquiring proves the first holder released on exit.
            with pc.file_lock(path):
                pass

    @unittest.skipIf(pc.IS_WINDOWS, "msvcrt byte locks have no shared mode")
    def test_shared_lock_can_be_taken_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.lock"
            path.touch()
            with pc.file_lock(path, exclusive=False):
                with pc.file_lock(path, exclusive=False):
                    pass

    def test_lock_handle_returns_when_uncontended(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handle.lock"
            path.touch()
            with path.open("a+", encoding="utf-8") as handle:
                with pc.lock_handle(handle, timeout=5.0):
                    pass

    @unittest.skipUnless(pc.IS_WINDOWS, "POSIX flock blocks instead of honouring the timeout")
    def test_lock_handle_times_out_when_held(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "held.lock"
            path.touch()
            with path.open("a+", encoding="utf-8") as held, path.open("a+", encoding="utf-8") as second:
                with pc.lock_handle(held, timeout=5.0):
                    with self.assertRaises(TimeoutError):
                        with pc.lock_handle(second, timeout=0.2, poll=0.05):
                            pass


class ProcessProbeTests(unittest.TestCase):
    def test_process_alive_rejects_invalid_pids(self):
        self.assertFalse(pc.process_alive(-1))
        self.assertFalse(pc.process_alive(0))
        self.assertFalse(pc.process_alive("not-a-pid"))
        self.assertFalse(pc.process_alive(None))

    def test_process_alive_for_this_process(self):
        self.assertTrue(pc.process_alive(os.getpid()))

    def test_process_alive_is_false_after_the_child_exits(self):
        self.assertFalse(pc.process_alive(_dead_child_pid()))

    def test_terminate_process_kills_a_live_child(self):
        child = _spawn_child("import time; time.sleep(30)")
        try:
            self.assertTrue(pc.process_alive(child.pid))
            self.assertTrue(pc.terminate_process(child.pid))
            self.assertTrue(_wait_dead(child))
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_terminate_process_of_a_dead_child_reports_failure(self):
        self.assertFalse(pc.terminate_process(_dead_child_pid()))

    def test_terminate_process_tree_kills_a_live_child(self):
        child = _spawn_child("import time; time.sleep(30)")
        try:
            self.assertTrue(pc.terminate_process_tree(child.pid))
            self.assertTrue(_wait_dead(child))
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_terminate_process_tree_falls_back_to_the_pid(self):
        """A stale/foreign pgid must not stop the tree termination."""
        child = _spawn_child("import time; time.sleep(30)")
        try:
            self.assertTrue(pc.terminate_process_tree(child.pid, pgid=999999999))
            self.assertTrue(_wait_dead(child))
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_suspend_and_resume_fall_back_to_the_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / "ticks"
            counter.touch()
            child = subprocess.Popen(
                [sys.executable, "-c", SuspendResumeTests.CHILD, str(counter)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **pc.launch_kwargs(),
            )
            try:
                deadline = time.monotonic() + 10
                while counter.stat().st_size == 0 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pc.suspend_process_tree(child.pid, pgid=999999999))
                time.sleep(0.2)
                paused = counter.stat().st_size
                time.sleep(0.3)
                self.assertEqual(counter.stat().st_size, paused)
                self.assertTrue(pc.resume_process_tree(child.pid, pgid=999999999))
                deadline = time.monotonic() + 10
                while counter.stat().st_size == paused and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreater(counter.stat().st_size, paused)
            finally:
                child.kill()
                child.wait(timeout=10)

    def test_group_id_of_a_dead_process_falls_back_to_the_pid(self):
        if pc.IS_WINDOWS:
            self.skipTest("Windows process group ids are the pid")
        dead = _dead_child_pid()
        self.assertEqual(pc.process_group_id(dead), dead)

    def test_process_group_id_is_the_pid_on_windows(self):
        if pc.IS_WINDOWS:
            self.assertEqual(pc.process_group_id(os.getpid()), os.getpid())
        else:
            self.assertGreater(pc.process_group_id(os.getpid()), 0)
        self.assertEqual(pc.process_group_id("nope"), 0)

    def test_group_target_prefers_an_explicit_pgid(self):
        self.assertEqual(pc._group_target(os.getpid(), pgid="4242"), 4242)
        self.assertEqual(pc._group_target(os.getpid(), pgid="broken"), pc.process_group_id(os.getpid()))

    def test_group_target_never_returns_group_zero_or_one(self):
        """Group 0 is the caller's own group and group 1 is init.

        Any non-numeric object coerces to 1 through ``int()`` (a bare test
        double does), which used to turn "terminate this game" into SIGTERM for
        the whole CI runner process group.
        """

        class CoercesToOne:
            def __int__(self):
                return 1

        for pgid in (0, 1, -5, True, False, "0", "1", CoercesToOne()):
            target = pc._group_target(os.getpid(), pgid=pgid)
            self.assertNotIn(target, (0, 1), msg=f"pgid={pgid!r} produced {target!r}")
        self.assertEqual(pc._group_target(os.getpid(), pgid="4242"), 4242)
        self.assertEqual(pc._group_target(os.getpid(), pgid=1), pc.process_group_id(os.getpid()))

    def test_process_signals_refuse_the_init_process(self):
        self.assertFalse(pc.terminate_process(1))
        self.assertFalse(pc.terminate_process_tree(1))
        self.assertFalse(pc.suspend_process_tree(1))
        self.assertFalse(pc.resume_process_tree(1))

    def test_launch_kwargs_shape(self):
        kwargs = pc.launch_kwargs()
        if pc.IS_WINDOWS:
            self.assertIn("creationflags", kwargs)
            self.assertIsInstance(kwargs["creationflags"], int)
            self.assertEqual(pc.launch_kwargs(new_group=False, detached=False, no_window=False), {})
        else:
            self.assertEqual(kwargs, {"start_new_session": True})

    def test_child_pids_lists_our_own_child(self):
        child = _spawn_child("import time; time.sleep(30)")
        try:
            deadline = time.monotonic() + 10
            children = pc.child_pids(os.getpid())
            while child.pid not in children and time.monotonic() < deadline:
                time.sleep(0.05)
                children = pc.child_pids(os.getpid())
            self.assertIn(child.pid, children)
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_child_pids_rejects_invalid_input(self):
        self.assertEqual(pc.child_pids("nope"), [])
        self.assertEqual(pc.child_pids(0), [])

    def test_find_pids_by_name_finds_this_interpreter(self):
        needle = Path(sys.executable).stem
        matches = pc.find_pids_by_name(needle)
        self.assertIn(os.getpid(), matches)
        self.assertEqual(pc.find_pids_by_name(""), [])

    def test_process_identity_of_this_process(self):
        pid = os.getpid()
        token = pc.process_start_token(pid)
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        self.assertEqual(token, pc.process_start_token(pid))

    def test_process_name_and_command_line_of_a_live_child(self):
        """The kernel names the file it executed, not the interpreter.

        The gate runs this suite through the ``coverage`` console script, and
        for a shebang wrapper the process is named after the script, so on
        POSIX this process reports ``coverage`` rather than the interpreter.
        A child spawned directly carries its interpreter, which is what the
        runtime probes when it tracks the processes it started.
        """
        child = _spawn_child("import time; time.sleep(30)")
        try:
            self.assertIn("python", pc.process_name(child.pid).casefold())
            self.assertIn("python", pc.process_command_line(child.pid).casefold())
        finally:
            child.kill()
            child.wait(timeout=10)

    def test_process_cwd_of_this_process(self):
        if pc.IS_WINDOWS:
            self.assertEqual(pc.process_cwd(os.getpid()), "")
        else:
            self.assertEqual(Path(pc.process_cwd(os.getpid())).resolve(), Path.cwd().resolve())

    def test_identity_probes_reject_invalid_pids(self):
        self.assertEqual(pc.process_name("nope"), "")
        self.assertEqual(pc.process_command_line(0), "")
        self.assertEqual(pc.process_cwd(-5), "")
        self.assertIsNone(pc.process_start_token("nope"))

    def test_identity_probes_reject_uncoercible_and_nonpositive_pids(self):
        self.assertEqual(pc.process_name(0), "")
        self.assertEqual(pc.process_command_line("nope"), "")
        self.assertEqual(pc.process_cwd("nope"), "")
        self.assertIsNone(pc.process_start_token(0))

    def test_signal_helpers_reject_uncoercible_and_nonpositive_pids(self):
        for pid in ("nope", 0, -3, None):
            self.assertFalse(pc.terminate_process(pid), pid)
            self.assertFalse(pc.terminate_process_tree(pid), pid)
            self.assertFalse(pc.suspend_process_tree(pid), pid)
            self.assertFalse(pc.resume_process_tree(pid), pid)

    def test_identity_probes_of_a_reaped_child_return_nothing(self):
        if pc.IS_WINDOWS:
            # Windows recycles pids and keeps handles open briefly, so only the
            # POSIX /proc lookups can assert "gone" deterministically.
            self.skipTest("POSIX-only /proc semantics")
        dead = _dead_child_pid()
        self.assertEqual(pc.process_name(dead), "")
        self.assertEqual(pc.process_command_line(dead), "")
        self.assertEqual(pc.process_cwd(dead), "")
        self.assertIsNone(pc.process_start_token(dead))

    def test_windows_process_table_is_empty_elsewhere(self):
        table = pc.windows_process_table()
        if pc.IS_WINDOWS:
            self.assertTrue(any(entry["pid"] == os.getpid() for entry in table))
            self.assertIn("parent", table[0])
        else:
            self.assertEqual(table, [])


class SuspendResumeTests(unittest.TestCase):
    """Suspend/resume must actually stop and restart the target."""

    CHILD = (
        "import time, sys\n"
        "path = sys.argv[1]\n"
        "end = time.time() + 30\n"
        "while time.time() < end:\n"
        "    with open(path, 'a', encoding='utf-8') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.02)\n"
    )

    def test_suspend_and_resume_stop_and_restart_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            counter = Path(directory) / "ticks"
            counter.touch()
            child = subprocess.Popen(
                [sys.executable, "-c", self.CHILD, str(counter)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **pc.launch_kwargs(),
            )
            try:
                deadline = time.monotonic() + 10
                while counter.stat().st_size == 0 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreater(counter.stat().st_size, 0, "child never started ticking")

                self.assertTrue(pc.suspend_process_tree(child.pid))
                time.sleep(0.2)  # let any in-flight write land before sampling
                paused = counter.stat().st_size
                time.sleep(0.4)
                self.assertEqual(counter.stat().st_size, paused, "child kept running while suspended")

                self.assertTrue(pc.resume_process_tree(child.pid))
                deadline = time.monotonic() + 10
                while counter.stat().st_size == paused and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreater(counter.stat().st_size, paused, "child never resumed")
            finally:
                child.kill()
                child.wait(timeout=10)

    def test_suspend_and_resume_reject_invalid_pids(self):
        self.assertFalse(pc.suspend_process_tree(0))
        self.assertFalse(pc.resume_process_tree("nope"))
        self.assertFalse(pc.terminate_process_tree(0))


class FinderTests(unittest.TestCase):
    def test_find_pids_in_folder_finds_a_child_started_there(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            if pc.IS_WINDOWS:
                # Windows has no cheap per-process working directory; the image
                # path is what the folder tracker compares there.
                folder = Path(sys.executable).parent
            child = _spawn_child("import time; time.sleep(30)", cwd=directory)
            try:
                deadline = time.monotonic() + 10
                matches = pc.find_pids_in_folder(folder)
                while child.pid not in matches and time.monotonic() < deadline:
                    time.sleep(0.05)
                    matches = pc.find_pids_in_folder(folder)
                self.assertIn(child.pid, matches)
            finally:
                child.kill()
                child.wait(timeout=10)

    def test_find_pids_in_folder_survives_an_unresolvable_folder(self):
        with mock.patch.object(pc.Path, "resolve", side_effect=OSError("unresolvable")):
            self.assertEqual(pc.find_pids_in_folder("Z:/no/such/folder"), [])


class OpenerTests(unittest.TestCase):
    @unittest.skipIf(pc.IS_WINDOWS, "POSIX desktop openers shell out to xdg-open")
    def test_open_path_uses_xdg_open(self):
        with mock.patch.object(pc.shutil, "which", return_value="/usr/bin/xdg-open"), mock.patch.object(
            pc.subprocess, "Popen"
        ) as popen:
            pc.open_path("/tmp/game.rom")
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/xdg-open", "/tmp/game.rom"])
        self.assertEqual(popen.call_args.kwargs, {"start_new_session": True})

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX desktop openers shell out to xdg-open")
    def test_open_path_without_xdg_open_raises(self):
        with mock.patch.object(pc.shutil, "which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                pc.open_path("/tmp/game.rom")

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX desktop openers shell out to xdg-open")
    def test_reveal_path_opens_the_containing_folder(self):
        with mock.patch.object(pc, "open_path") as opened:
            pc.reveal_path("/tmp/games/game.rom")
        opened.assert_called_once_with(Path("/tmp/games"))

    @unittest.skipUnless(pc.IS_WINDOWS, "Windows hands the URI to os.startfile")
    def test_open_path_hands_the_target_to_the_shell(self):
        with mock.patch.object(pc.os, "startfile", create=True) as startfile:
            pc.open_path("C:/games/game.rom")
        startfile.assert_called_once_with("C:/games/game.rom")

    @unittest.skipUnless(pc.IS_WINDOWS, "Windows reveals files through explorer")
    def test_reveal_path_selects_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "game.rom"
            target.write_bytes(b"rom")
            with mock.patch.object(pc.subprocess, "Popen") as popen:
                pc.reveal_path(target)
        self.assertEqual(popen.call_args.args[0][:2], ["explorer", "/select,"])
        self.assertEqual(popen.call_args.args[0][2], os.path.normpath(str(target)))

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX desktop openers shell out to xdg-open")
    def test_open_url_uses_xdg_open(self):
        with mock.patch.object(pc.shutil, "which", return_value="/usr/bin/xdg-open"), mock.patch.object(
            pc.subprocess, "Popen"
        ) as popen:
            pc.open_url("http://127.0.0.1:1/")
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/xdg-open", "http://127.0.0.1:1/"])

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX falls back to the webbrowser module")
    def test_open_url_falls_back_to_webbrowser(self):
        with mock.patch.object(pc.shutil, "which", return_value=None), mock.patch(
            "webbrowser.open", return_value=True
        ) as opened:
            pc.open_url("http://127.0.0.1:1/")
        opened.assert_called_once_with("http://127.0.0.1:1/")

    @unittest.skipIf(pc.IS_WINDOWS, "POSIX falls back to the webbrowser module")
    def test_open_url_raises_when_nothing_can_open_links(self):
        with mock.patch.object(pc.shutil, "which", return_value=None), mock.patch(
            "webbrowser.open", return_value=False
        ):
            with self.assertRaises(OSError):
                pc.open_url("http://127.0.0.1:1/")

    @unittest.skipUnless(pc.IS_WINDOWS, "Windows hands the URI to os.startfile")
    def test_open_url_hands_the_uri_to_the_shell(self):
        with mock.patch.object(pc.os, "startfile", create=True) as startfile:
            pc.open_url("http://127.0.0.1:1/")
        startfile.assert_called_once_with("http://127.0.0.1:1/")

    def test_browser_candidates_are_launchable(self):
        candidates = pc.browser_application_commands()
        self.assertTrue(candidates)
        if pc.IS_WINDOWS:
            self.assertTrue(all(word.casefold().endswith(".exe") for word in candidates), candidates)
        else:
            self.assertIn("chromium", candidates)

    def test_resolve_browser_application_uses_which_for_bare_names(self):
        with mock.patch.object(pc, "browser_application_commands", return_value=["chromium"]), mock.patch.object(
            pc.shutil, "which", return_value="/usr/bin/chromium"
        ):
            self.assertEqual(pc.resolve_browser_application(), "/usr/bin/chromium")

    def test_resolve_browser_application_returns_none_when_absent(self):
        with mock.patch.object(pc, "browser_application_commands", return_value=["chromium"]), mock.patch.object(
            pc.shutil, "which", return_value=None
        ):
            self.assertIsNone(pc.resolve_browser_application())

    def test_resolve_browser_application_uses_an_absolute_install(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Path(directory) / "chrome"
            browser.write_bytes(b"")
            with mock.patch.object(pc, "browser_application_commands", return_value=[str(browser)]):
                self.assertEqual(pc.resolve_browser_application(), str(browser))
            missing = Path(directory) / "gone"
            with mock.patch.object(pc, "browser_application_commands", return_value=[str(missing)]), mock.patch.object(
                pc.shutil, "which", return_value=None
            ):
                self.assertIsNone(pc.resolve_browser_application())


@unittest.skipIf(pc.IS_WINDOWS, "POSIX mode bits have no Windows equivalent")
class PosixPrivacyTests(unittest.TestCase):
    def test_private_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.env"
            path.write_text("TOKEN=1", encoding="utf-8")
            path.chmod(0o600)
            info = path.stat()
            self.assertTrue(pc.file_is_private(info))
            self.assertFalse(pc.is_group_or_world_writable(info))
            self.assertTrue(pc.owned_by_current_user(info))

    def test_world_readable_file_is_not_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "world.env"
            path.write_text("TOKEN=1", encoding="utf-8")
            path.chmod(0o644)
            info = path.stat()
            self.assertFalse(pc.file_is_private(info))

    def test_group_writable_file_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "group.env"
            path.write_text("TOKEN=1", encoding="utf-8")
            path.chmod(0o660)
            self.assertTrue(pc.is_group_or_world_writable(path.stat()))

    def test_other_owners_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            info = os.stat(directory)
            foreign = type("S", (), {"st_uid": info.st_uid + 1, "st_mode": 0o600})()
            self.assertFalse(pc.owned_by_current_user(foreign))
            self.assertFalse(pc.file_is_private(foreign))

    def test_privacy_falls_back_when_the_stat_has_no_owner(self):
        synthetic = type("S", (), {"st_mode": 0o600})()
        self.assertTrue(pc.file_is_private(synthetic))
        writable = type("S", (), {"st_mode": 0o666})()
        self.assertFalse(pc.file_is_private(writable))

    def test_privacy_falls_back_for_ownership_when_the_stat_has_no_owner(self):
        self.assertTrue(pc.owned_by_current_user(type("S", (), {"st_mode": 0o600})()))


@unittest.skipUnless(pc.IS_WINDOWS, "Windows has no POSIX mode bits to inspect")
class WindowsPrivacyTests(unittest.TestCase):
    def test_windows_reports_profile_owned_files_as_private(self):
        info = Path(sys.executable).stat()
        self.assertTrue(pc.file_is_private(info))
        self.assertFalse(pc.is_group_or_world_writable(info))
        self.assertTrue(pc.owned_by_current_user(info))


class SteamDiscoveryTests(unittest.TestCase):
    def test_steam_install_roots_only_return_directories(self):
        roots = pc.steam_install_roots()
        self.assertIsInstance(roots, list)
        for root in roots:
            self.assertIsInstance(root, Path)
            self.assertTrue(root.is_dir())

    @unittest.skipIf(pc.IS_WINDOWS, "Epic manifests are a Windows-only source")
    def test_epic_manifest_paths_are_windows_only(self):
        self.assertEqual(pc.epic_manifest_paths(), [])

    @unittest.skipUnless(pc.IS_WINDOWS, "Epic manifests are a Windows-only source")
    def test_epic_manifest_paths_read_the_manifest_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            manifests = Path(directory) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
            manifests.mkdir(parents=True)
            (manifests / "b.item").write_text("{}", encoding="utf-8")
            (manifests / "a.item").write_text("{}", encoding="utf-8")
            (manifests / "notes.txt").write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PROGRAMDATA": directory}):
                found = pc.epic_manifest_paths()
        self.assertEqual([path.name for path in found], ["a.item", "b.item"])

    @unittest.skipUnless(pc.IS_WINDOWS, "Epic manifests are a Windows-only source")
    def test_epic_manifest_paths_without_the_folder(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PROGRAMDATA": directory}):
            self.assertEqual(pc.epic_manifest_paths(), [])

    @unittest.skipUnless(pc.IS_WINDOWS, "Epic manifests are a Windows-only source")
    def test_epic_manifest_paths_survive_a_failing_glob(self):
        with tempfile.TemporaryDirectory() as directory:
            manifests = Path(directory) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
            manifests.mkdir(parents=True)
            with mock.patch.dict(os.environ, {"PROGRAMDATA": directory}), mock.patch.object(
                pc.Path, "glob", side_effect=OSError("denied")
            ):
                self.assertEqual(pc.epic_manifest_paths(), [])


if __name__ == "__main__":
    unittest.main()
