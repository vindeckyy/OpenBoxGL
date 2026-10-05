#!/usr/bin/env python3
"""The plugin sandbox must mask the data directory, wherever it lives.

The bubblewrap command in ``plugins._sandboxed_command`` ro-binds the whole
filesystem and then masks five top-level directories: /home, /tmp, /run, /mnt
and /media. The OpenBox data directory was therefore protected only *by
accident* -- it normally lives under ``$HOME``, and /home is masked.

Two supported configurations break that accident:

* ``OPENBOX_DATA_DIR`` is a documented override (the test suite and the manual
  verification instructions both use it), and it can point anywhere;
* ``default_data_dir()`` falls back to ``Path.cwd() / "openbox-game-launcher"``
  when the home directory cannot be determined.

Either way, ``library.json``, ``settings.json`` -- which holds provider
credentials -- and the plugin state stay readable through the ``--ro-bind / /``
while the code and the docs both claim a plugin only ever sees its stdin
payload.

Masking by resolved path is free: ``plugin_runner.py`` does
``json.load(sys.stdin)``, and ``plugins.py`` documents per-plugin settings as
travelling in that payload. No legitimate plugin reads the data directory from
disk, so this makes the documented guarantee true.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import plugins
from pkg.platform_compat import resolved_data_dir


def _masked_paths(command) -> list[str]:
    """Return every path the command masks with --tmpfs."""
    masked = []
    for index, token in enumerate(command[:-1]):
        if token == "--tmpfs":
            masked.append(command[index + 1])
    return masked


def _build(env_data_dir: str, *, share_net: bool = False):
    with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": env_data_dir}, clear=False):
        with mock.patch.object(plugins.shutil, "which", return_value="/usr/bin/bwrap"):
            return plugins._sandboxed_command(
                Path("."), Path("pkg/example_plugin.py"), "before_library", share_net=share_net
            )


class SandboxPrefixRuleTests(unittest.TestCase):
    """The prefix rule, as a pure function, so it is testable on any host.

    Feeding Path.resolve() a "/srv/data" string on Windows yields
    "C:\\\\srv\\\\data", so a test that asserted on the literal string would be
    asserting about a path that cannot exist on the machine running it -- and
    would pass for the wrong reason. The rule itself takes a POSIX string and
    is tested directly.
    """

    def test_a_dir_outside_the_five_needs_a_mask(self):
        for path in (
            "/srv/openbox-data",
            "/opt/app-root/custom-state",
            "/var/lib/openbox",
            "/data/openbox-game-launcher",
            "/",  # degenerate: the root is not one of the five
        ):
            with self.subTest(path=path):
                self.assertTrue(plugins._needs_data_dir_mask(path))

    def test_a_dir_already_under_a_masked_prefix_does_not(self):
        for path in (
            "/home/tester/.local/share/openbox-game-launcher",
            "/tmp/openbox",
            "/run/openbox",
            "/mnt/games",
            "/media/usb/openbox",
            # Exactly equal to a prefix is covered too.
            "/home",
            "/tmp",
        ):
            with self.subTest(path=path):
                self.assertFalse(plugins._needs_data_dir_mask(path))

    def test_a_sibling_prefix_is_not_covered(self):
        # "/homebrew" starts with "/home" but is not under it.
        for path in ("/homebrew/openbox", "/tmpfiles/openbox", "/runner/openbox"):
            with self.subTest(path=path):
                self.assertTrue(plugins._needs_data_dir_mask(path))


class SandboxMasksDataDirTests(unittest.TestCase):
    def _expected_mask(self, env_data_dir: str) -> str:
        with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": env_data_dir}, clear=False):
            return resolved_data_dir().resolve().as_posix()

    def test_masks_a_data_dir_outside_the_default_five(self):
        # The configuration that used to leak: a data dir not under $HOME.
        outside = "/srv/openbox-data"
        command = _build(outside)
        expected = self._expected_mask(outside)
        if not plugins._needs_data_dir_mask(expected):
            self.skipTest("this host's resolution placed the data dir under a masked prefix")
        self.assertIn(expected, _masked_paths(command), command)

    def test_does_not_double_mask_a_dir_already_under_a_masked_prefix(self):
        home = Path.home()
        data_dir = str(home / ".local" / "share" / "openbox-game-launcher")
        command = _build(data_dir)
        expected = self._expected_mask(data_dir)
        if plugins._needs_data_dir_mask(expected):
            self.skipTest("this host's home is not under a masked prefix, so nothing to dedupe")
        masked = _masked_paths(command)
        self.assertNotIn(expected, masked, "already covered by an existing --tmpfs")
        self.assertTrue(
            any(expected.startswith(prefix.rstrip("/") + "/") for prefix in masked),
            f"{expected} is not under any masked prefix: {masked}",
        )

    def test_still_keeps_the_original_five_masks(self):
        command = _build("/srv/openbox-data")
        masked = _masked_paths(command)
        for path in plugins.SANDBOX_MASKED_PREFIXES:
            self.assertIn(path, masked, command)

    def test_mask_does_not_change_the_plugin_payload_contract(self):
        """The mask is safe because the payload still arrives on stdin."""
        command = _build("/srv/openbox-data")
        joined = " ".join(command)
        self.assertIn("plugin_runner.py", joined)
        runner = Path(__file__).resolve().parent.parent / "plugin_runner.py"
        source = runner.read_text(encoding="utf-8")
        self.assertIn("json.load(sys.stdin)", source)

    def test_share_net_ordering_is_preserved(self):
        """--share-net must still follow --unshare-all (bubblewrap is ordered)."""
        command = _build("/srv/openbox-data", share_net=True)
        joined = " ".join(command)
        self.assertLess(joined.index("--unshare-all"), joined.index("--share-net"))

    def test_returns_none_without_bwrap(self):
        with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": "/srv/openbox-data"}, clear=False):
            with mock.patch.object(plugins.shutil, "which", return_value=None):
                self.assertIsNone(
                    plugins._sandboxed_command(Path("."), Path("pkg/example_plugin.py"), "before_library")
                )


class ResolvedDataDirTests(unittest.TestCase):
    def test_openbox_data_dir_override_wins(self):
        with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": "/srv/custom"}, clear=False):
            self.assertEqual(resolved_data_dir(), Path("/srv/custom"))

    def test_expands_a_user_relative_override(self):
        with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": "~/mystate"}, clear=False):
            resolved = resolved_data_dir()
            self.assertFalse(str(resolved).startswith("~"), resolved)

    def test_matches_the_path_openbox_actually_binds(self):
        """The sandbox and the app must agree on the directory, by construction."""
        import openbox

        with mock.patch.dict(os.environ, {"OPENBOX_DATA_DIR": "/srv/agreement-check"}, clear=False):
            # openbox binds at import time, so compare against the rule rather
            # than the already-bound constant.
            self.assertEqual(resolved_data_dir(), Path("/srv/agreement-check"))
        self.assertEqual(openbox.DATA.parent, Path(os.environ.get("OPENBOX_DATA_DIR", openbox.DATA.parent)).expanduser())


if __name__ == "__main__":
    unittest.main()
