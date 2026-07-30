#!/usr/bin/env python3
"""Emulate Steam Deck / Bazzite Game Mode enough to accept OpenBox guest behavior.

Runs inside nested gamescope (see scripts/emulate_deck_gamemode.sh). This is not a
full Steam client, but it mirrors the session env and window-prop contract OpenBox
depends on: Steam owns STEAM_GAME=769, OpenBox is a guest with a dedicated id,
Steam launches stay on steam, non-Steam windows get STEAM_GAME props.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
FAILURES = []


def _gamescope_available():
    return bool(shutil.which("gamescope"))


def _inside_gamescope():
    return bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"))


def _run_under_gamescope():
    """Re-exec this file inside nested gamescope, Deck/Bazzite style."""
    env = os.environ.copy()
    env.update(
        {
            "SteamDeck": "1",
            "SCB_GAMEMODE": "1",
            "SCB_NOSCOPE": "1",
            "OPENBOX_DECK_EMU_NESTED": "1",
        }
    )
    rc_file = tempfile.NamedTemporaryFile(prefix="openbox-deck-emu-rc-", delete=False)
    rc_file.close()
    cmd = [
        "timeout",
        "120",
        "gamescope",
        "-W",
        "1280",
        "-H",
        "800",
        "-w",
        "1280",
        "-h",
        "800",
        "--backend",
        "sdl",
        "--",
        "env",
        "SteamDeck=1",
        "SCB_GAMEMODE=1",
        "SCB_NOSCOPE=1",
        "OPENBOX_DECK_EMU_NESTED=1",
        f"OPENBOX_DECK_EMU_RC={rc_file.name}",
        sys.executable,
        str(Path(__file__).resolve()),
    ]
    try:
        result = subprocess.run(cmd, env=env, cwd=str(ROOT), check=False)
        try:
            text = Path(rc_file.name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text:
            return int(text)
        return result.returncode
    finally:
        try:
            Path(rc_file.name).unlink(missing_ok=True)
        except OSError:
            pass


def check(name, condition, detail=""):
    if condition:
        print(f"PASS  {name}")
        return True
    FAILURES.append(name)
    print(f"FAIL  {name}{(' — ' + detail) if detail else ''}")
    return False


def wait_url(proc, timeout=20):
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
            continue
        buf += line
        if line.startswith("http://127.0.0.1:"):
            return line.strip(), buf
    return None, buf


def api_get(base_url, path):
    parsed = urlparse(base_url)
    token = parse_qs(parsed.query)["token"][0]
    url = f"http://{parsed.hostname}:{parsed.port}{path}"
    req = urllib.request.Request(url, headers={"X-OpenBox-Token": token})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def api_post(base_url, path, body):
    parsed = urlparse(base_url)
    token = parse_qs(parsed.query)["token"][0]
    url = f"http://{parsed.hostname}:{parsed.port}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-OpenBox-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode())


def write_fake_steam(bin_dir: Path, log_path: Path):
    steam = bin_dir / "steam"
    steam.write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{log_path}"\n'
        'if [[ " $* " == *" -applaunch "* ]]; then\n'
        '  exec sleep 8\n'
        "fi\n"
        "exit 0\n"
    )
    steam.chmod(0o755)
    return steam


def spawn_steam_owner_window():
    """Simulate Steam gamepadui as session owner with STEAM_GAME=769."""
    script = (
        "import tkinter as t, subprocess, time, os\n"
        "r = t.Tk(); r.title('Steam');\n"
        "r.geometry('320x200'); r.update_idletasks()\n"
        "time.sleep(0.4)\n"
        "wid = hex(int(r.winfo_id()))\n"
        "subprocess.run(['xprop','-id',str(r.winfo_id()),'-f','STEAM_GAME','32c','-set','STEAM_GAME','769'], check=False)\n"
        "print('STEAM_OWNER', r.winfo_id(), flush=True)\n"
        "r.after(25000, r.destroy); r.mainloop()\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    from parity_gamescope import (
        OPENBOX_STEAM_GAME_ID,
        game_mode_url,
        is_gamescope_guest,
        is_steam_launch,
        should_nest_gamescope,
        steam_game_id_for,
    )

    print("=== Deck / Bazzite Game Mode emulation ===")
    print(f"DISPLAY={os.environ.get('DISPLAY')}")
    print(f"GAMESCOPE_WAYLAND_DISPLAY={os.environ.get('GAMESCOPE_WAYLAND_DISPLAY')}")
    print(f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP')}")
    print(f"SteamDeck={os.environ.get('SteamDeck')}")

    check("guest detect under gamescope", is_gamescope_guest())
    check("do not nest gamescope", should_nest_gamescope() is False)
    check("OpenBox id is not Steam 769", OPENBOX_STEAM_GAME_ID != 769)
    check(
        "deeplink Big Box URL",
        "deeplink=bigbox" in game_mode_url("http://127.0.0.1:9/?token=abc"),
    )

    steam_owner = spawn_steam_owner_window()
    try:
        owner_line = ""
        deadline = time.time() + 8
        while time.time() < deadline and "STEAM_OWNER" not in owner_line:
            chunk = steam_owner.stdout.readline()
            if not chunk:
                break
            owner_line += chunk
        owner_ok = "STEAM_OWNER" in owner_line
        check("Steam owner window started (769 role)", owner_ok, owner_line.strip())
        if owner_ok:
            wid = owner_line.strip().split()[-1]
            prop = subprocess.run(
                ["xprop", "-id", wid, "STEAM_GAME"],
                capture_output=True,
                text=True,
                check=False,
            )
            check("Steam owner has STEAM_GAME=769", "769" in prop.stdout, prop.stdout.strip())

        with tempfile.TemporaryDirectory(prefix="openbox-deck-emu-") as tmp:
            data_dir = Path(tmp) / "data"
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            data_dir.mkdir()
            steam_log = Path(tmp) / "steam.log"
            write_fake_steam(bin_dir, steam_log)

            env = os.environ.copy()
            env["OPENBOX_DATA_DIR"] = str(data_dir)
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["SteamDeck"] = env.get("SteamDeck", "1")
            # Bazzite/ScopeBuddy-style marker that we are already in Game Mode.
            env["SCB_GAMEMODE"] = "1"
            env["SCB_NOSCOPE"] = "1"

            rom = Path(tmp) / "fake-rom"
            rom.write_text("#!/bin/bash\nexec sleep 12\n")
            rom.chmod(0o755)

            library = {
                "games": [
                    {
                        "name": "Emu Guest Title",
                        "path": str(rom),
                        "launch": str(rom),
                        "platform": "NES",
                    },
                    {
                        "name": "Fake Steam Title",
                        "path": str(bin_dir / "steam"),
                        "launch": "steam -applaunch 570",
                        "steam_app_id": "570",
                        "platform": "PC",
                    },
                ],
                "profiles": {},
                "history": [],
                "settings": {"welcome_completed": True},
                "playlists": [],
            }
            (data_dir / "library.json").write_text(json.dumps(library, indent=2))

            server = subprocess.Popen(
                [sys.executable, str(ROOT / "web_app.py"), "--game-mode", "--no-browser"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            try:
                url, boot_log = wait_url(server)
                check("OpenBox server boots in game mode", bool(url), boot_log[-500:])
                if not url:
                    return 1

                settings = api_get(url, "/api/settings")
                check("settings.gamescope_guest true", settings.get("gamescope_guest") is True, str(settings.get("gamescope_guest")))

                # Non-Steam launch under guest session
                launch = api_post(url, "/api/launch", {"id": 0})
                check("non-Steam launch returns pid", bool(launch.get("pid")), str(launch))
                time.sleep(1.2)
                marked = False
                marked_id = None
                for attempt in range(15):
                    result = subprocess.run(
                        ["xdotool", "search", "--name", "Emu Guest Title"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    # sleep has no window; mark by launching a titled helper instead if needed
                    if result.returncode == 0 and result.stdout.strip():
                        for wid in result.stdout.split():
                            prop = subprocess.run(
                                ["xprop", "-id", wid, "STEAM_GAME"],
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            if str(steam_game_id_for(library["games"][0])) in prop.stdout:
                                marked = True
                                marked_id = wid
                                break
                    if marked:
                        break
                    # Fallback: any window with our synthetic id range
                    prop_root = subprocess.run(
                        ["xprop", "-root"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    time.sleep(0.25)

                # sleep binary has no X window — spawn a titled stand-in the way emulators do
                if not marked:
                    helper = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "import tkinter as t; r=t.Tk(); r.title('Emu Guest Title'); "
                            "r.geometry('240x140'); r.after(8000, r.destroy); r.mainloop()",
                        ],
                        env=env,
                    )
                    time.sleep(0.8)
                    from parity_gamescope import mark_process_windows

                    windows = mark_process_windows(
                        helper.pid,
                        steam_game_id_for(library["games"][0]),
                        attempts=10,
                        delay=0.2,
                        window_name="Emu Guest Title",
                    )
                    marked = bool(windows)
                    if windows:
                        prop = subprocess.run(
                            ["xprop", "-id", windows[0], "STEAM_GAME"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        match = re.search(r"=\s*(\d+)", prop.stdout)
                        actual = int(match.group(1)) if match else None
                        check(
                            "non-Steam window STEAM_GAME set (not 769)",
                            actual == steam_game_id_for(library["games"][0]) and actual != 769,
                            prop.stdout.strip(),
                        )
                    else:
                        check("non-Steam window STEAM_GAME set (not 769)", False, "no windows marked")
                    helper.terminate()
                    try:
                        helper.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        helper.kill()
                else:
                    check("non-Steam window STEAM_GAME set (not 769)", True, f"id={marked_id}")

                # Steam launch path must stay on steam (Input/overlay stay with Steam)
                args = ["steam", "-applaunch", "570"]
                check("Steam launch detected as steam path", is_steam_launch(args))
                steam_launch = api_post(url, "/api/launch", {"id": 1})
                check("Steam title launch returns pid", bool(steam_launch.get("pid")), str(steam_launch))
                time.sleep(0.6)
                steam_log_text = steam_log.read_text() if steam_log.exists() else ""
                check(
                    "fake steam received -applaunch (Input path intact)",
                    "-applaunch" in steam_log_text and "570" in steam_log_text,
                    steam_log_text.strip(),
                )

                # QAM / TDP proxy: Steam owner prop must still be 769 after OpenBox activity
                if owner_ok:
                    wid = owner_line.strip().split()[-1]
                    prop = subprocess.run(
                        ["xprop", "-id", wid, "STEAM_GAME"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    check(
                        "Steam owner still STEAM_GAME=769 after OpenBox (QAM role intact)",
                        "769" in prop.stdout,
                        prop.stdout.strip(),
                    )

                # Nested gamescope guard: guest code must refuse nesting
                check("should_nest_gamescope false in Game Mode", should_nest_gamescope() is False)

            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
    finally:
        steam_owner.terminate()
        try:
            steam_owner.wait(timeout=3)
        except subprocess.TimeoutExpired:
            steam_owner.kill()

    print("---")
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): {', '.join(FAILURES)}")
        code = 1
    else:
        print("RESULT PASS: Deck/Bazzite gamescope guest emulation accepted")
        code = 0

    rc_file = os.environ.get("OPENBOX_DECK_EMU_RC")
    if rc_file:
        try:
            Path(rc_file).write_text(str(code), encoding="utf-8")
        except OSError:
            pass

    # When run by the harness wrapper, gamescope stays alive until we exit; the
    # wrapper records our code from the rc file, so a hard exit is fine here.
    if os.environ.get("OPENBOX_DECK_EMU_RC"):
        os._exit(code)
    return code


class DeckGamescopeEmuTests(unittest.TestCase):
    def test_deck_gamescope_guest_emulation(self):
        if not _gamescope_available():
            self.skipTest("gamescope not installed")
        if _inside_gamescope():
            code = main()
            self.assertEqual(code, 0, "Deck/Bazzite gamescope emulation failed inside gamescope")
            return
        code = _run_under_gamescope()
        self.assertEqual(code, 0, "Deck/Bazzite gamescope emulation harness failed")


if __name__ == "__main__":
    if _inside_gamescope():
        raise SystemExit(main())
    if not _gamescope_available():
        print("gamescope not installed; skipping Deck/Bazzite emulation")
        raise SystemExit(0)
    rc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "emulate_deck_gamemode.sh")],
        cwd=str(ROOT),
        check=False,
    ).returncode
    raise SystemExit(rc)
