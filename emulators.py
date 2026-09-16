"""Install and configure supported Linux emulators."""

import copy
import shlex
import shutil
import subprocess
import threading
import time

from pkg.parity.parity_emulator_defs import EMULATORS, PLATFORM_EMULATORS
from parity_import import recommend_emulators

# ``flatpak info`` costs a subprocess per app; a short TTL keeps the status
# endpoint and the install/update loops from re-spawning it repeatedly
# (P2-10).
STATUS_TTL = 30.0
_STATUS_CACHE: dict = {"at": 0.0, "result": None}
_STATUS_LOCK = threading.Lock()


def invalidate_status_cache() -> None:
    with _STATUS_LOCK:
        _STATUS_CACHE.update({"at": 0.0, "result": None})


def commands_for(app_id, prefix):
    return {
        platform: shlex.join(prefix + shlex.split(arguments))
        for platform, arguments in EMULATORS[app_id]["profiles"].items()
    }


def emulator_status(run=subprocess.run, which=shutil.which, *, refresh=False):
    # Only cache the default probes; injected runners are test/dev doubles.
    cacheable = run is subprocess.run and which is shutil.which
    now = time.monotonic()
    if cacheable and not refresh:
        with _STATUS_LOCK:
            cached = _STATUS_CACHE["result"]
            if cached is not None and now - _STATUS_CACHE["at"] < STATUS_TTL:
                return copy.deepcopy(cached)
    flatpak = which("flatpak")
    result = []
    for app_id, emulator in EMULATORS.items():
        native = which(emulator["native"])
        flatpak_installed = bool(flatpak) and run(
            [flatpak, "info", app_id],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        mode = "native" if native else "flatpak" if flatpak_installed else ""
        prefix = [native] if native else [flatpak, "run", app_id] if flatpak_installed else []
        result.append({
            "app_id": app_id,
            "name": emulator["name"],
            "platforms": list(emulator["profiles"]),
            "installed": bool(mode),
            "mode": mode,
            "profiles": commands_for(app_id, prefix) if prefix else {},
            "can_install": bool(flatpak),
            "recommendations": PLATFORM_EMULATORS,
        })
    if cacheable:
        with _STATUS_LOCK:
            _STATUS_CACHE.update({"at": now, "result": copy.deepcopy(result)})
    return result


def recommendations_for_platform(platform):
    return recommend_emulators(platform)


def launch_emulator(app_id, which=shutil.which):
    if app_id not in EMULATORS:
        raise ValueError("Unknown emulator.")
    emulator = EMULATORS[app_id]
    native = which(emulator["native"])
    flatpak = which("flatpak")
    if native:
        subprocess.Popen([native], start_new_session=True)
        return {"mode": "native", "command": native}
    if flatpak and subprocess.run(
        [flatpak, "info", app_id],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0:
        subprocess.Popen([flatpak, "run", app_id], start_new_session=True)
        return {"mode": "flatpak", "command": f"flatpak run {app_id}"}
    raise FileNotFoundError(f"{emulator['name']} is not installed.")


def install_all_emulators(run=subprocess.run, which=shutil.which):
    # One status listing for the whole loop instead of one per app (P2-10).
    statuses = {item["app_id"]: item for item in emulator_status(run=run, which=which)}
    installed = []
    errors = []
    for app_id in EMULATORS:
        status = statuses.get(app_id)
        if status is None or status["installed"]:
            continue
        try:
            install_emulator(app_id, run=run, which=which)
            installed.append(status["name"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(f"{status['name']}: {error}")
    if installed:
        invalidate_status_cache()
    return {"installed": installed, "errors": errors}


def update_emulator(app_id, run=subprocess.run, which=shutil.which):
    if app_id not in EMULATORS:
        raise ValueError("Unknown emulator.")
    flatpak = which("flatpak")
    if not flatpak:
        raise FileNotFoundError("Flatpak is required for emulator updates.")
    run(
        [flatpak, "update", "--user", "--noninteractive", "-y", app_id],
        check=True, capture_output=True, text=True, timeout=1800,
    )
    invalidate_status_cache()
    return {"updated": EMULATORS[app_id]["name"]}


def update_all_emulators(run=subprocess.run, which=shutil.which):
    statuses = {item["app_id"]: item for item in emulator_status(run=run, which=which)}
    updated, errors = [], []
    for app_id in EMULATORS:
        status = statuses.get(app_id)
        if status is None or not status["installed"] or status["mode"] != "flatpak":
            continue
        try:
            update_emulator(app_id, run=run, which=which)
            updated.append(status["name"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(f"{status['name']}: {error}")
    if updated:
        invalidate_status_cache()
    return {"updated": updated, "errors": errors}


def install_emulator(app_id, run=subprocess.run, which=shutil.which):
    if app_id not in EMULATORS:
        raise ValueError("Unknown emulator.")
    flatpak = which("flatpak")
    if not flatpak:
        raise FileNotFoundError("Flatpak is required for automatic emulator installation.")
    remotes = run(
        [flatpak, "remotes", "--user"],
        capture_output=True, text=True, timeout=120,
    )
    if "flathub" not in (remotes.stdout + remotes.stderr).lower():
        remote_result = run(
            [flatpak, "remote-add", "--user", "--if-not-exists", "flathub", "https://flathub.org/repo/flathub.flatpakrepo"],
            capture_output=True, text=True, timeout=120,
        )
        if remote_result.returncode != 0:
            detail = (remote_result.stderr or remote_result.stdout or "").strip()
            raise RuntimeError(
                "Could not add the Flathub remote. "
                + (f"flatpak says: {detail}" if detail else "Check network access to flathub.org.")
            )
    install_result = run(
        [flatpak, "install", "--user", "--noninteractive", "-y", "flathub", app_id],
        capture_output=True, text=True, timeout=1800,
    )
    if install_result.returncode != 0:
        detail = (install_result.stderr or install_result.stdout or "").strip()
        raise RuntimeError(
            f"Flatpak could not install {EMULATORS[app_id]['name']}. "
            + (f"flatpak says: {detail}" if detail else "Check network access to flathub.org.")
        )
    invalidate_status_cache()
    return commands_for(app_id, [flatpak, "run", app_id])
