#!/usr/bin/env python3
"""Tests for Steam gamescope guest helpers."""

import unittest
from unittest import mock

from parity_gamescope import (
    OPENBOX_STEAM_GAME_ID,
    game_mode_url,
    is_gamescope_guest,
    is_steam_launch,
    kiosk_command,
    mark_process_windows,
    open_ui,
    resolve_kiosk_browser,
    set_steam_game_prop,
    should_nest_gamescope,
    steam_game_id_for,
)


class DetectionTests(unittest.TestCase):
    def test_empty_env_is_not_guest(self):
        self.assertFalse(is_gamescope_guest({}))

    def test_force_makes_guest(self):
        self.assertTrue(is_gamescope_guest({}, force=True))

    def test_gamescope_wayland_display(self):
        self.assertTrue(is_gamescope_guest({"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}))

    def test_xdg_current_desktop_gamescope(self):
        self.assertTrue(is_gamescope_guest({"XDG_CURRENT_DESKTOP": "gamescope"}))

    def test_gnome_desktop_not_guest(self):
        self.assertFalse(
            is_gamescope_guest(
                {
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                    "DISPLAY": ":0",
                }
            )
        )

    def test_should_not_nest_when_guest(self):
        self.assertFalse(should_nest_gamescope({"GAMESCOPE_WAYLAND_DISPLAY": "1"}))
        self.assertTrue(should_nest_gamescope({}))


class UrlAndBrowserTests(unittest.TestCase):
    def test_game_mode_url_adds_deeplink(self):
        url = game_mode_url("http://127.0.0.1:9/?token=abc")
        self.assertIn("token=abc", url)
        self.assertIn("deeplink=bigbox", url)

    def test_resolve_kiosk_browser_order(self):
        def which(name):
            return "/usr/bin/google-chrome" if name == "google-chrome" else None

        self.assertEqual(resolve_kiosk_browser(which=which), ["/usr/bin/google-chrome"])

    def test_resolve_kiosk_browser_none(self):
        self.assertIsNone(resolve_kiosk_browser(which=lambda name: None))

    def test_kiosk_command_app_flag(self):
        cmd = kiosk_command(["/usr/bin/google-chrome"], "http://127.0.0.1/?token=1")
        self.assertEqual(cmd[0], "/usr/bin/google-chrome")
        self.assertIn("--app=http://127.0.0.1/?token=1", cmd)

    def test_open_ui_guest_uses_kiosk(self):
        calls = []

        def fake_which(name):
            return "/bin/chromium" if name == "chromium" else None

        def fake_popen(args, start_new_session=False, env=None):
            calls.append(args)
            return mock.Mock()

        result = open_ui(
            "http://127.0.0.1:1/?token=t",
            guest=True,
            popen=fake_popen,
            browser_open=lambda url: calls.append(["browse", url]),
            which=fake_which,
        )
        self.assertEqual(result["mode"], "kiosk")
        self.assertIn("deeplink=bigbox", result["url"])
        self.assertTrue(any("--app=" in str(part) for part in calls[0]))
        self.assertIn("pid", result)

    def test_open_ui_guest_falls_back_on_oserror(self):
        browsed = []

        def fake_popen(args, start_new_session=False, env=None):
            raise OSError("spawn failed")

        result = open_ui(
            "http://127.0.0.1:1/?token=t",
            guest=True,
            popen=fake_popen,
            browser_open=lambda url: browsed.append(url),
            which=lambda name: "/bin/chromium" if "chrom" in name else None,
        )
        self.assertEqual(result["mode"], "webbrowser")
        self.assertIn("deeplink=bigbox", browsed[0])

    def test_open_ui_desktop_uses_xdg_open_with_clean_env(self):
        calls = []

        def fake_popen(args, start_new_session=False, env=None):
            calls.append((args, env))
            return mock.Mock(pid=55)

        result = open_ui(
            "http://127.0.0.1:1/?token=t",
            guest=False,
            popen=fake_popen,
            browser_open=lambda url: self.fail("webbrowser should not run"),
            which=lambda name: "/usr/bin/xdg-open" if name == "xdg-open" else None,
            environ={
                "LD_LIBRARY_PATH": "/tmp/appimage/usr/lib",
                "PYTHONHOME": "/tmp/appimage/usr",
                "DISPLAY": ":0",
                "PATH": "/usr/bin",
            },
        )
        self.assertEqual(result["mode"], "xdg-open")
        self.assertEqual(calls[0][0], ["/usr/bin/xdg-open", "http://127.0.0.1:1/?token=t"])
        self.assertNotIn("LD_LIBRARY_PATH", calls[0][1])
        self.assertNotIn("PYTHONHOME", calls[0][1])

    def test_open_ui_desktop_falls_back_to_webbrowser(self):
        browsed = []
        result = open_ui(
            "http://127.0.0.1:1/?token=t",
            guest=False,
            popen=lambda *a, **k: self.fail("popen should not run"),
            browser_open=lambda url: browsed.append(url),
            which=lambda name: None,
        )
        self.assertEqual(result["mode"], "webbrowser")
        self.assertEqual(browsed, ["http://127.0.0.1:1/?token=t"])


class PropTests(unittest.TestCase):
    def test_steam_game_id_from_app_id(self):
        self.assertEqual(steam_game_id_for({"steam_app_id": "570"}), 570)

    def test_steam_game_id_synthetic_stable(self):
        game = {"name": "Local ROM", "path": "/games/a.nes"}
        self.assertEqual(steam_game_id_for(game), steam_game_id_for(game))
        self.assertNotEqual(steam_game_id_for(game), OPENBOX_STEAM_GAME_ID)

    def test_set_steam_game_prop_builds_argv(self):
        seen = {}

        def runner(cmd, **kwargs):
            seen["cmd"] = cmd
            return mock.Mock(returncode=0)

        ok = set_steam_game_prop(
            "0x123",
            OPENBOX_STEAM_GAME_ID,
            xprop="/usr/bin/xprop",
            display=":0",
            runner=runner,
        )
        self.assertTrue(ok)
        self.assertEqual(seen["cmd"][0], "/usr/bin/xprop")
        self.assertIn("STEAM_GAME", seen["cmd"])
        self.assertEqual(seen["cmd"][-1], str(OPENBOX_STEAM_GAME_ID))

    def test_set_steam_game_prop_noop_without_xprop(self):
        self.assertFalse(set_steam_game_prop("1", 2, xprop="", display=":0"))

    def test_set_steam_game_prop_noop_without_display(self):
        self.assertFalse(set_steam_game_prop("1", 2, xprop="/usr/bin/xprop", display=""))

    def test_mark_process_windows(self):
        runs = []

        def runner(cmd, **kwargs):
            runs.append(cmd)
            if cmd[0] == "/bin/xdotool":
                return mock.Mock(returncode=0, stdout="42\n")
            return mock.Mock(returncode=0)

        marked = mark_process_windows(
            99,
            570,
            attempts=2,
            delay=0,
            sleep=lambda _s: None,
            xdotool="/bin/xdotool",
            xprop="/bin/xprop",
            display=":0",
            runner=runner,
        )
        self.assertEqual(marked, ["42"])
        self.assertTrue(any(cmd[0] == "/bin/xprop" for cmd in runs))

    def test_mark_process_windows_by_name_fallback(self):
        runs = []

        def runner(cmd, **kwargs):
            runs.append(cmd)
            if len(cmd) >= 3 and cmd[1] == "search" and cmd[2] == "--pid":
                return mock.Mock(returncode=1, stdout="")
            if len(cmd) >= 3 and cmd[1] == "search" and cmd[2] == "--name":
                return mock.Mock(returncode=0, stdout="99\n")
            return mock.Mock(returncode=0)

        marked = mark_process_windows(
            123,
            OPENBOX_STEAM_GAME_ID,
            attempts=1,
            delay=0,
            sleep=lambda _s: None,
            window_name="OpenBox",
            xdotool="/bin/xdotool",
            xprop="/bin/xprop",
            display=":0",
            runner=runner,
        )
        self.assertEqual(marked, ["99"])
        self.assertTrue(any(len(cmd) >= 3 and cmd[2] == "--name" for cmd in runs))

    def test_is_steam_launch(self):
        self.assertTrue(is_steam_launch(["steam", "-applaunch", "570"]))
        self.assertTrue(is_steam_launch(["xdg-open", "steam://rungameid/570"]))
        self.assertTrue(is_steam_launch(["flatpak", "run", "com.valvesoftware.Steam", "-applaunch", "1"]))
        self.assertFalse(is_steam_launch(["retroarch", "-L", "core.so", "rom.nes"]))


if __name__ == "__main__":
    unittest.main()
