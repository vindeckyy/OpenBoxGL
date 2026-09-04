#!/usr/bin/env python3
"""Local-first Linux game library and launcher. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC.

Shared core for the web server and native host: data paths, state store, launch commands, profile discovery.
"""

import os
import shlex
import shutil
import sys
from pathlib import Path

import pkg.parity  # noqa: F401  # register flat-import finder before parity_* imports
from archives import extract_game
from parity_import import EXTENSIONS_EXTRA, PLATFORM_BY_EXTENSION_EXTRA
from parity_emulator_defs import build_platform_by_extension, resolve_launch
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
    **build_platform_by_extension(),
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
    from parity_emulator_defs import _registry, build_adapter_argv, detect_adapter_prefix

    profiles = {}
    for platform, adapters in _registry()["by_platform"].items():
        for adapter in adapters:
            prefix = detect_adapter_prefix(adapter, which=which)
            if not prefix:
                continue
            try:
                command = build_adapter_argv(
                    adapter,
                    {"name": adapter["label"]},
                    "{path}",
                    prefix=prefix,
                    which=which,
                )
            except FileNotFoundError:
                continue
            profiles.setdefault(platform, shlex.join(command))
            break
    candidates = {
        "DOSBox": ("dosbox", "dosbox {path}"),
        "Windows": ("wine", "wine {path}"),
    }
    for platform, (binary, command) in candidates.items():
        if which(binary):
            profiles.setdefault(platform, command)
    return profiles


def build_launch(game, profiles):
    path = game.get("path", "")
    if not path:
        raise ValueError(f"{game.get('name', 'This game')} has no launch path.")
    if not Path(path).exists():
        raise FileNotFoundError(f"The configured path no longer exists:\n{path}")
    launch_path = (
        str(extract_game(path, DATA.parent / "cache/archives", game.get("archive_member", "")))
        if game.get("extract_archive")
        else path
    )
    launch_game = dict(game)
    launch_game["path"] = launch_path
    resolved = resolve_launch(launch_game, profiles, which=shutil.which, data_dir=str(DATA.parent))
    return resolved["args"], resolved["cwd"]


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
    elif "--help" in sys.argv or "-h" in sys.argv:
        print("openbox.py is a core library module providing shared state, launch commands, and path utilities.")
        print("To run the OpenBox web server: python3 web_app.py")
        print("To run tests: ./run_all_tests.sh or make check")
        sys.exit(0)
    else:
        # Only the self-test above runs this module directly.
        sys.stderr.write("openbox.py is a library module. Run web_app.py or the native host.\n")
        sys.exit(1)
