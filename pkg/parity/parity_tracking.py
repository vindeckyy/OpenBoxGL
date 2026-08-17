"""Per-game process tracking modes for Linux launch sessions."""

import os
import time
from pathlib import Path

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
    try:
        return (Path(f"/proc/{pid}/comm").read_text().strip())
    except OSError:
        return ""


def _proc_cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _proc_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_pids_by_name(pattern):
    pattern = pattern.casefold()
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        name = _proc_name(pid).casefold()
        cmdline = _proc_cmdline(pid).casefold()
        if pattern in name or pattern in cmdline:
            matches.append(pid)
    return matches


def find_pids_in_folder(folder):
    folder = str(Path(folder).resolve())
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cwd = _proc_cwd(pid)
        if cwd == folder or cwd.startswith(folder + os.sep):
            matches.append(pid)
    return matches


def wait_for_exit(process, game, settings):
    config = resolve_mode(game, settings)
    if config["delay"]:
        time.sleep(config["delay"])
    mode = config["mode"]
    if mode in {"default", "process"}:
        return process.wait()
    original = process.pid
    if mode == "original_process":
        while _alive(original):
            time.sleep(config["frequency"])
        return process.poll() if process.poll() is not None else 0
    if mode == "folder":
        folders = [str(game.get("install_dir", "")).strip(), str(game.get("path", "")).strip()]
        folders = [str(Path(folder).parent) if Path(folder).is_file() else folder for folder in folders if folder]
        tracked = set()
        while True:
            tracked.clear()
            for folder in folders:
                tracked.update(find_pids_in_folder(folder))
            if not tracked:
                if process.poll() is not None:
                    return process.poll()
                time.sleep(config["frequency"])
                continue
            if not any(_alive(pid) for pid in tracked):
                return 0
            time.sleep(config["frequency"])
    if mode == "process_name":
        pattern = config["process_name"] or Path(str(game.get("path", ""))).stem
        if not pattern:
            return process.wait()
        while True:
            matches = find_pids_by_name(pattern)
            if not matches:
                if process.poll() is not None:
                    return process.poll()
                time.sleep(config["frequency"])
                continue
            if not any(_alive(pid) for pid in matches):
                return 0
            time.sleep(config["frequency"])
    return process.wait()


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
