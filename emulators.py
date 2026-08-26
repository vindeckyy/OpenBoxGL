"""Install and configure supported Linux emulators."""

import shlex
import shutil
import subprocess

from pkg.parity.parity_emulator_defs import EMULATORS, PLATFORM_EMULATORS
from parity_import import recommend_emulators


def commands_for(app_id, prefix):
    return {
        platform: shlex.join(prefix + shlex.split(arguments))
        for platform, arguments in EMULATORS[app_id]["profiles"].items()
    }


def emulator_status(run=subprocess.run, which=shutil.which):
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
    installed = []
    errors = []
    for app_id in EMULATORS:
        status = next(item for item in emulator_status(run=run, which=which) if item["app_id"] == app_id)
        if status["installed"]:
            continue
        try:
            install_emulator(app_id, run=run, which=which)
            installed.append(status["name"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(f"{status['name']}: {error}")
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
    return {"updated": EMULATORS[app_id]["name"]}


def update_all_emulators(run=subprocess.run, which=shutil.which):
    updated, errors = [], []
    for app_id in EMULATORS:
        status = next(item for item in emulator_status(run=run, which=which) if item["app_id"] == app_id)
        if not status["installed"] or status["mode"] != "flatpak":
            continue
        try:
            update_emulator(app_id, run=run, which=which)
            updated.append(status["name"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(f"{status['name']}: {error}")
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
    return commands_for(app_id, [flatpak, "run", app_id])
