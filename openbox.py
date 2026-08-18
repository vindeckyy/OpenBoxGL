#!/usr/bin/env python3
"""Local-first Linux game library and launcher. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC.

Shared core for the web server and native host: data paths, state store, launch commands, profile discovery.
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

from archives import extract_game
from parity_import import EXTENSIONS_EXTRA, PLATFORM_BY_EXTENSION_EXTRA
from state_store import JsonStateStore

CUSTOM_DATA_DIR = os.environ.get("OPENBOX_DATA_DIR")
APP_DIR = Path(CUSTOM_DATA_DIR or Path.home() / ".local/share/openbox-game-launcher").expanduser()
DATA = APP_DIR / "library.json"
LEGACY_DATA = Path.home() / ".local" / "share" / "launchbox-linux" / "library.json"
if not CUSTOM_DATA_DIR and not DATA.exists() and LEGACY_DATA.is_file():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LEGACY_DATA, DATA)

STATE_STORE = JsonStateStore(DATA)

EXTENSIONS = {".sh", ".appimage", ".exe", ".iso", ".rom", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc", ".zip", ".7z", ".rar"} | EXTENSIONS_EXTRA
PLATFORM_BY_EXTENSION = {
    ".nes": "NES", ".sfc": "SNES", ".smc": "SNES", ".gba": "Game Boy Advance",
    ".gb": "Game Boy", ".gbc": "Game Boy Color", ".iso": "Disc image",
    **PLATFORM_BY_EXTENSION_EXTRA,
}

# Development-only screenshot fixtures must never ship in user libraries.
DEMO_PATH_MARKERS = ("/tmp/openbox-screenshots/",)


def is_demo_game(game):
    if not isinstance(game, dict):
        return False
    if game.get("demo"):
        return True
    path = str(game.get("path", ""))
    return any(marker in path for marker in DEMO_PATH_MARKERS)


def purge_demo_games(state):
    games = state.get("games", [])
    if not isinstance(games, list):
        return 0
    kept = [game for game in games if not is_demo_game(game)]
    removed = len(games) - len(kept)
    if removed:
        state["games"] = kept
    return removed


def load_state():
    return STATE_STORE.load()


def load_state_readonly():
    """Return the cached state without copying. Callers must not mutate the result."""
    return STATE_STORE.load_readonly()


def save_state(state):
    return STATE_STORE.save(state)


def update_state(mutator):
    """Apply one state mutation under the cross-process transaction lock."""
    return STATE_STORE.update(mutator)


def update_state_with_result(mutator):
    """Apply a mutation and return both the committed state and callback result."""
    return STATE_STORE.update_with_result(mutator)


def recover_state():
    return STATE_STORE.recover()


def format_duration(seconds):
    minutes = int(seconds or 0) // 60
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def discover_profiles(which=shutil.which):
    candidates = {
        "DOSBox": ("dosbox", "dosbox {path}"),
        "Windows": ("wine", "wine {path}"),
        "Arcade": ("mame", "mame {path}"),
        "GameCube": ("dolphin-emu", "dolphin-emu -b -e {path}"),
        "Wii": ("dolphin-emu", "dolphin-emu -b -e {path}"),
        "PlayStation 2": ("pcsx2-qt", "pcsx2-qt {path}"),
        "PSP": ("ppsspp", "ppsspp {path}"),
        "PlayStation 3": ("rpcs3", "rpcs3 {path}"),
        "PlayStation": ("duckstation-qt", "duckstation-qt -batch {path}"),
    }
    return {platform: command for platform, (binary, command) in candidates.items() if which(binary)}


def build_launch(game, profiles):
    path = game.get("path", "")
    if not path:
        raise ValueError(f"{game.get('name', 'This game')} has no launch path.")
    if not Path(path).exists():
        raise FileNotFoundError(f"The configured path no longer exists:\n{path}")
    launch_path = str(extract_game(path, DATA.parent / "cache/archives", game.get("archive_member", ""))) if game.get("extract_archive") else path
    game_command = game.get("launch", "")
    command = game_command or profiles.get(game.get("platform", ""), "")
    if command:
        rom_p = Path(launch_path)
        replacements = {
            "{path}": launch_path,
            "{ImagePath}": launch_path,
            "{name}": str(game.get("name", "")),
            "{Name}": str(game.get("name", "")),
            "{dir}": str(rom_p.parent),
            "{Dir}": str(rom_p.parent),
            "{file}": rom_p.name,
            "{File}": rom_p.name,
            "{stem}": rom_p.stem,
            "{FileNameWithoutExtension}": rom_p.stem,
            "{platform}": str(game.get("platform", "")),
            "{Platform}": str(game.get("platform", "")),
            "{app_id}": str(game.get("steam_app_id", "")),
            "{heroic_app_id}": str(game.get("heroic_app_id", "")),
            "{lutris_id}": str(game.get("lutris_id", "")),
            "{rom_name}": str(game.get("rom_name", "")),
            "{DataDir}": str(DATA.parent),
        }
        args = shlex.split(command)
        for marker, value in replacements.items():
            args = [part.replace(marker, value) for part in args]
        if not game_command and "{path}" not in command and "{ImagePath}" not in command:
            args.append(launch_path)
    elif Path(launch_path).suffix.lower() == ".sh":
        args = ["bash", launch_path]
    else:
        args = [launch_path]
    return args, str(Path(launch_path).parent)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        assert {"games", "profiles", "history"} <= load_state().keys()
        assert "{path}" in "retroarch -L core.so {path}"
        assert shlex.split("retroarch -L core.so {path}")[-1] == "{path}"
        assert format_duration(3720) == "1h 2m"
        assert discover_profiles(lambda binary: f"/usr/bin/{binary}" if binary == "wine" else None) == {"Windows": "wine {path}"}
        try:
            build_launch({"name": "Missing", "path": ""}, {})
        except ValueError:
            pass
        else:
            raise AssertionError("empty paths must not launch")
        print("openbox self-test: ok")
    else:
        # Only the self-test above runs this module directly.
        sys.stderr.write("openbox.py is a library module. Run web_app.py or the native host.\n")
        sys.exit(1)
