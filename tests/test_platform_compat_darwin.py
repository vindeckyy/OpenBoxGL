#!/usr/bin/env python3
"""The macOS process layer of the seam, on a real macOS host.

macOS has no /proc, so the process helpers read the libproc API. These
tests start real child processes: they are the proof that the process
identity, the working directory, and the child walk work.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg import platform_compat as pc  # noqa: E402


def _child(cwd=None):
    """Start a child that waits, so the tests can inspect it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd)


def _sleep_child():
    """Start a child that never re-execs, so its image path is its own."""
    return subprocess.Popen(["/bin/sleep", "30"])


def _wait_for(getter, done, timeout=10.0):
    """Wait until *done* accepts the value *getter* returns."""
    deadline = time.monotonic() + timeout
    value = getter()
    while time.monotonic() < deadline:
        if done(value):
            return value
        time.sleep(0.05)
        value = getter()
    return value


@unittest.skipUnless(sys.platform == "darwin", "the libproc layer only exists on macOS")
class DarwinProcessTests(unittest.TestCase):
    def test_the_libproc_handle_is_available(self):
        self.assertIsNotNone(pc._libproc())

    def test_the_start_token_is_stable_for_a_live_process(self):
        first = pc.process_start_token(os.getpid())
        second = pc.process_start_token(os.getpid())
        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_a_dead_process_has_no_start_token(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=30)
        self.assertIsNone(pc.process_start_token(child.pid))

    def test_the_process_name_comes_from_the_executable(self):
        # A framework Python re-execs into Python.app, so this process's
        # kernel name is not sys.executable's name. A plain binary has no
        # such indirection.
        child = _sleep_child()
        try:
            name = _wait_for(lambda: pc.process_name(child.pid), bool)
            self.assertEqual(name, "sleep")
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_the_command_fingerprint_is_the_executable_path(self):
        child = _sleep_child()
        try:
            fingerprint = _wait_for(lambda: pc.process_command_line(child.pid), bool)
            self.assertEqual(Path(fingerprint).resolve(), Path("/bin/sleep").resolve())
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_the_working_directory_of_a_child_is_its_own(self):
        with tempfile.TemporaryDirectory() as directory:
            child = _child(cwd=directory)
            try:
                cwd = _wait_for(lambda: pc.process_cwd(child.pid), lambda value: bool(value))
                self.assertEqual(Path(cwd).resolve(), Path(directory).resolve())
            finally:
                child.terminate()
                child.wait(timeout=10)

    def test_the_children_of_a_process_are_found(self):
        child = _child()
        try:
            children = _wait_for(
                lambda: pc.child_pids(os.getpid()),
                lambda value: child.pid in value,
            )
            self.assertIn(child.pid, children)
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_a_process_is_found_by_name(self):
        child = _sleep_child()
        try:
            matches = _wait_for(
                lambda: pc.find_pids_by_name("sleep"),
                lambda value: child.pid in value,
            )
            self.assertIn(child.pid, matches)
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_a_process_is_found_by_its_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            child = _child(cwd=directory)
            try:
                found = _wait_for(
                    lambda: pc.find_pids_in_folder(directory),
                    lambda value: child.pid in value,
                )
                self.assertIn(child.pid, found)
            finally:
                child.terminate()
                child.wait(timeout=10)

    def test_a_folder_is_found_regardless_of_its_case(self):
        # The default macOS volume is case-insensitive, so the folder picked by
        # the user may carry a different case than the kernel reports.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve()
            flipped = path.parent / path.name.upper()
            if flipped == path or not flipped.is_dir():
                self.skipTest("this volume is case-sensitive")
            child = _child(cwd=path)
            try:
                found = _wait_for(
                    lambda: pc.find_pids_in_folder(flipped),
                    lambda value: child.pid in value,
                )
                self.assertIn(child.pid, found)
            finally:
                child.terminate()
                child.wait(timeout=10)

    def test_a_terminated_process_is_no_longer_alive(self):
        child = _child()
        self.assertTrue(pc.process_alive(child.pid))
        pc.terminate_process(child.pid, force=True)
        # The child has to be reaped: a process that has exited but not been
        # waited for still answers to a liveness probe.
        child.wait(timeout=10)
        self.assertFalse(pc.process_alive(child.pid))

    def test_the_process_group_of_a_child_is_reported(self):
        child = _child()
        try:
            # The child inherits the group of the process that started it.
            self.assertEqual(
                pc.process_group_id(child.pid),
                pc.process_group_id(os.getpid()),
            )
        finally:
            child.terminate()
            child.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
