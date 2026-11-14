"""Per-game process tracking modes for Linux and Windows launch sessions."""

import time
from collections import namedtuple
from pathlib import Path

from pkg.platform_compat import (
    process_alive as _platform_alive,
    process_command_line as _platform_cmdline,
    process_cwd as _platform_cwd,
    process_name as _platform_name,
    find_pids_by_name as _platform_find_by_name,
    find_pids_in_folder as _platform_find_in_folder,
)

WaitResult = namedtuple('WaitResult', ['exit_code', 'timed_out'])

TRACKING_MODES = ("default", "process", "original_process", "folder", "process_name")

STORE_CLIENTS = {
    "Steam": ("steam", "com.valvesoftware.Steam"),
    "Epic": ("heroic", "com.heroicgameslauncher.hgl"),
    "GOG": ("heroic", "com.heroicgameslauncher.hgl"),
    "Amazon": ("heroic", "com.heroicgameslauncher.hgl"),
    "Lutris": ("lutris", "net.lutris.Lutris"),
}


def resolve_mode(game, settings):
    mode = str(game.get("tracking_mode") or settings.get("tracking_mode", "default")).strip().casefold()
    if mode not in TRACKING_MODES:
        mode = "default"
    try:
        delay = int(game.get("tracking_delay", settings.get("tracking_delay", 0)) or 0)
    except (TypeError, ValueError):
        delay = 0
    delay = min(600, max(0, delay))
    try:
        frequency = float(game.get("tracking_frequency", settings.get("tracking_frequency", 2)) or 2)
    except (TypeError, ValueError):
        frequency = 2
    frequency = min(60, max(0.5, frequency))
    process_name = str(game.get("tracking_process_name", "")).strip()
    return {"mode": mode, "delay": delay, "frequency": frequency, "process_name": process_name}


def _proc_name(pid):
    return _platform_name(pid)


def _proc_cmdline(pid):
    return _platform_cmdline(pid)


def _proc_cwd(pid):
    return _platform_cwd(pid)


def _alive(pid):
    return _platform_alive(pid)


def find_pids_by_name(pattern):
    return _platform_find_by_name(pattern)


def find_pids_in_folder(folder):
    return _platform_find_in_folder(folder)


_OPENER_NAMES = frozenset({"open", "xdg-open", "cmd", "cmd.exe", "explorer", "explorer.exe"})


def _launched_name(process) -> str:
    args = getattr(process, "args", None) or []
    if not args:
        return ""
    return Path(str(args[0])).name.casefold()


def _empty_grace(config) -> float:
    """How long an empty scan may last after the launcher has already exited."""
    return max(float(config["delay"] or 0), 5.0)


def wait_for_exit(process, game, settings, max_tracking_duration=3600):
    config = resolve_mode(game, settings)
    if config["delay"]:
        time.sleep(config["delay"])
    mode = config["mode"]
    # `open` and `xdg-open` return before the game exits. Waiting on that
    # process ends the session while the game is still running.
    opener_launch = mode in {"default", "process"} and _launched_name(process) in _OPENER_NAMES
    if mode in {"default", "process"} and not opener_launch:
        return WaitResult(exit_code=process.wait(), timed_out=False)
    if opener_launch:
        mode = "folder"
    original = process.pid
    if mode == "original_process":
        while True:
            # poll() reaps the child. os.kill(pid, 0) still succeeds on a
            # zombie, so a loop that only checks liveness never finishes.
            finished = process.poll()
            if finished is not None or not _alive(original):
                return WaitResult(exit_code=finished if finished is not None else 0, timed_out=False)
            time.sleep(config["frequency"])
    if mode == "folder":
        if opener_launch:
            # The opener's own directory is /usr/bin. Watching it matches
            # every system tool. Only the game's install directory is useful.
            install_dir = str(game.get("install_dir", "")).strip()
            folders = [install_dir] if install_dir else []
        else:
            folders = [str(game.get("install_dir", "")).strip(), str(game.get("path", "")).strip()]
            folders = [str(Path(folder).parent) if Path(folder).is_file() else folder for folder in folders if folder]
        empty_limit = 60.0 if opener_launch and not folders else _empty_grace(config)
        tracked = set()
        elapsed = 0
        empty_for = 0.0
        while True:
            tracked.clear()
            for folder in folders:
                tracked.update(find_pids_in_folder(folder))
            if not tracked:
                if process.poll() is not None:
                    empty_for += config["frequency"]
                    if empty_for >= empty_limit:
                        return WaitResult(exit_code=process.poll(), timed_out=False)
                else:
                    empty_for = 0.0
                time.sleep(config["frequency"])
                elapsed += config["frequency"]
                continue
            empty_for = 0.0
            if not any(_alive(pid) for pid in tracked):
                return WaitResult(exit_code=0, timed_out=False)
            if process.poll() is not None and elapsed >= max_tracking_duration:
                return WaitResult(exit_code=-1, timed_out=True)
            time.sleep(config["frequency"])
            elapsed += config["frequency"]
    if mode == "process_name":
        pattern = config["process_name"] or Path(str(game.get("path", ""))).stem
        # One- and two-letter names match unrelated executables and hold the
        # session until the tracking cap.
        if len(pattern) < 3:
            return WaitResult(exit_code=process.wait(), timed_out=False)
        elapsed = 0
        empty_for = 0.0
        while True:
            matches = find_pids_by_name(pattern)
            if not matches:
                if process.poll() is not None:
                    empty_for += config["frequency"]
                    if empty_for >= _empty_grace(config):
                        return WaitResult(exit_code=process.poll(), timed_out=False)
                else:
                    empty_for = 0.0
                time.sleep(config["frequency"])
                elapsed += config["frequency"]
                continue
            empty_for = 0.0
            if not any(_alive(pid) for pid in matches):
                return WaitResult(exit_code=0, timed_out=False)
            if process.poll() is not None and elapsed >= max_tracking_duration:
                return WaitResult(exit_code=-1, timed_out=True)
            time.sleep(config["frequency"])
            elapsed += config["frequency"]
    return WaitResult(exit_code=process.wait(), timed_out=False)


def close_store_client(game, settings):
    if not settings.get("auto_close_store_clients"):
        return
    source = str(game.get("source", "")).strip()
    entry = STORE_CLIENTS.get(source)
    if not entry:
        return
    binary, flatpak_id = entry
    import shutil
    import subprocess
    if shutil.which(binary):
        subprocess.Popen([binary, "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("flatpak"):
        subprocess.Popen(
            ["flatpak", "kill", flatpak_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
