"""Portable Steam gamescope guest helpers for OpenBox.

Detection is env-based; xprop and kiosk browsers are best-effort.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# Dedicated id for OpenBox UI windows. Not 769 (Steam / main client).
OPENBOX_STEAM_GAME_ID = 413091001

KIOSK_BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
)

FLATPAK_BROWSERS = (
    "org.chromium.Chromium",
    "com.google.Chrome",
    "com.brave.Browser",
    "com.microsoft.Edge",
)


def is_gamescope_guest(environ=None, force=False):
    """Return True when running under gamescope or --game-mode was requested."""
    if force:
        return True
    env = environ if environ is not None else os.environ
    if str(env.get("GAMESCOPE_WAYLAND_DISPLAY", "")).strip():
        return True
    # Steam sets this in Game Mode even when the gamescope env is not exported (some Deck firmware).
    if str(env.get("STEAM_GAMESCOPE_RESTRICTED", "")).strip():
        return True
    desktop = " ".join(
        [
            str(env.get("XDG_CURRENT_DESKTOP", "")),
            str(env.get("XDG_SESSION_DESKTOP", "")),
            str(env.get("DESKTOP_SESSION", "")),
        ]
    ).casefold()
    if "gamescope" in desktop:
        return True
    return False


def should_nest_gamescope(environ=None, force=False):
    """Never nest gamescope when already a guest."""
    return not is_gamescope_guest(environ=environ, force=force)


def game_mode_url(base_url):
    """Append deeplink=bigbox to a local OpenBox UI URL."""
    parts = urlsplit(str(base_url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["deeplink"] = "bigbox"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def resolve_kiosk_browser(which=None, run=None):
    """Return argv prefix for a kiosk/--app browser, or None."""
    finder = which or shutil.which
    runner = run or subprocess.run
    for name in KIOSK_BROWSERS:
        path = finder(name)
        if path:
            return [path]
    flatpak = finder("flatpak")
    if flatpak:
        for app_id in FLATPAK_BROWSERS:
            try:
                result = runner(
                    [flatpak, "info", app_id],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if getattr(result, "returncode", 1) == 0:
                return [flatpak, "run", app_id]
    return None


def resolve_app_window_browser(which=None, run=None):
    """Return argv for a chrome-less app window, or None; Chromium uses --app=URL, Firefox falls back to --new-window."""
    finder = which or shutil.which
    browser = resolve_kiosk_browser(which=finder, run=run)
    if browser:
        return browser
    firefox = finder("firefox")
    if firefox:
        return [firefox]
    return None


def kiosk_command(browser_argv, url):
    """Build a single-window browser command for the OpenBox UI URL."""
    if not browser_argv:
        raise ValueError("No browser argv provided.")
    args = list(browser_argv)
    binary = Path(args[0]).name.casefold()
    if binary == "flatpak" or "firefox" in binary:
        args.extend(["--new-window", url])
        return args
    args.extend([f"--app={url}", "--new-window"])
    return args


def host_helper_env(environ=None):
    """Env for xdg-open/host browsers without AppImage library overrides."""
    env = dict(environ if environ is not None else os.environ)
    for key in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
        env.pop(key, None)
    return env


def open_ui(url, *, guest=False, force_game_mode=False, native_window=False, popen=None, browser_open=None, which=None, environ=None):
    """Open the UI; use a kiosk/app window when guest/game-mode or requested."""
    target = game_mode_url(url) if (guest or force_game_mode) else url
    opener = popen or subprocess.Popen
    browse = browser_open or webbrowser.open
    helper_env = host_helper_env(environ)
    if guest or force_game_mode or native_window:
        if guest or force_game_mode:
            browser = resolve_kiosk_browser(which=which)
            mode = "kiosk"
        else:
            browser = resolve_app_window_browser(which=which)
            mode = "app"
        if browser:
            try:
                process = opener(
                    kiosk_command(browser, target),
                    start_new_session=True,
                    env=helper_env,
                )
            except OSError:
                browser = None
            else:
                pid = getattr(process, "pid", None)
                return {"url": target, "mode": mode, "browser": browser[0], "pid": pid}
    finder = which or shutil.which
    xdg = finder("xdg-open")
    if xdg:
        try:
            process = opener([xdg, target], start_new_session=True, env=helper_env)
        except OSError:
            process = None
        else:
            return {
                "url": target,
                "mode": "xdg-open",
                "browser": xdg,
                "pid": getattr(process, "pid", None),
            }
    if not browse(target):
        # webbrowser could not open anything either; fail loud so a
        # double-click launch is not a black hole.
        print(f"OpenBox server running at {target} — no window opener available; open this URL manually.",
              file=sys.stderr)
    return {"url": target, "mode": "webbrowser", "browser": "", "pid": None}


def steam_game_id_for(game):
    """Steam App ID when present; otherwise a stable synthetic id for props."""
    if not isinstance(game, dict):
        return OPENBOX_STEAM_GAME_ID
    raw = str(game.get("steam_app_id") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    seed = "|".join(
        [
            str(game.get("source") or ""),
            str(game.get("heroic_app_id") or ""),
            str(game.get("lutris_id") or ""),
            str(game.get("gameyfin_id") or ""),
            str(game.get("path") or ""),
            str(game.get("name") or ""),
        ]
    )
    digest = hashlib.sha1(seed.encode("utf-8", errors="replace")).hexdigest()
    # Keep out of Steam's reserved low range; stay below signed 32-bit.
    return 700_000_000 + (int(digest[:8], 16) % 200_000_000)


def set_steam_game_prop(window_id, app_id, *, xprop=None, display=None, runner=None):
    """Best-effort set STEAM_GAME on an X11 window. Returns True on success."""
    if window_id is None or str(window_id).strip() == "":
        return False
    try:
        app = int(app_id)
    except (TypeError, ValueError):
        return False
    binary = xprop or shutil.which("xprop")
    if not binary:
        return False
    env_display = display if display is not None else os.environ.get("DISPLAY", "")
    if not str(env_display).strip():
        return False
    run = runner or subprocess.run
    cmd = [
        binary,
        "-id",
        str(window_id),
        "-f",
        "STEAM_GAME",
        "32c",
        "-set",
        "STEAM_GAME",
        str(app),
    ]
    try:
        result = run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "DISPLAY": str(env_display)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _child_pids(pid):
    """Return pid plus descendants from /proc (best-effort)."""
    try:
        root = int(pid)
    except (TypeError, ValueError):
        return []
    found = {root}
    changed = True
    while changed:
        changed = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            child = int(entry.name)
            if child in found:
                continue
            try:
                status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parent = None
            for line in status.splitlines():
                if line.startswith("PPid:"):
                    try:
                        parent = int(line.split()[1])
                    except (IndexError, ValueError):
                        parent = None
                    break
            if parent in found:
                found.add(child)
                changed = True
    return sorted(found)


def _window_ids_for_pid(pid, *, xdotool=None, wmctrl=None, runner=None, display=None):
    env_display = display if display is not None else os.environ.get("DISPLAY", "")
    if not str(env_display).strip() or pid is None:
        return []
    run = runner or subprocess.run
    ids = []
    pids = {str(value) for value in _child_pids(pid)}
    xdotool_bin = xdotool or shutil.which("xdotool")
    if xdotool_bin:
        for candidate in sorted(pids):
            try:
                result = run(
                    [xdotool_bin, "search", "--pid", candidate],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    env={**os.environ, "DISPLAY": str(env_display)},
                )
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
                continue
            if getattr(result, "returncode", 1) != 0:
                continue
            for line in str(result.stdout or "").splitlines():
                text = line.strip()
                if text and text not in ids:
                    ids.append(text)
    if ids:
        return ids
    wmctrl_bin = wmctrl or shutil.which("wmctrl")
    if not wmctrl_bin:
        return []
    try:
        result = run(
            [wmctrl_bin, "-lp"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "DISPLAY": str(env_display)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    for line in str(result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        window_id, owner = parts[0], parts[2]
        if owner in pids and window_id not in ids:
            ids.append(window_id)
    return ids


def _window_ids_by_name(name, *, xdotool=None, runner=None, display=None):
    if not name:
        return []
    binary = xdotool or shutil.which("xdotool")
    if not binary:
        return []
    env_display = display if display is not None else os.environ.get("DISPLAY", "")
    if not str(env_display).strip():
        return []
    run = runner or subprocess.run
    try:
        result = run(
            [binary, "search", "--name", str(name)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "DISPLAY": str(env_display)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    return [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]


def _window_ids_by_class(class_name, *, xdotool=None, runner=None, display=None):
    if not class_name:
        return []
    binary = xdotool or shutil.which("xdotool")
    if not binary:
        return []
    env_display = display if display is not None else os.environ.get("DISPLAY", "")
    if not str(env_display).strip():
        return []
    run = runner or subprocess.run
    try:
        result = run(
            [binary, "search", "--class", str(class_name)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "DISPLAY": str(env_display)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if getattr(result, "returncode", 1) != 0:
        return []
    return [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]


def mark_process_windows(
    pid,
    app_id,
    *,
    attempts=8,
    delay=0.25,
    sleep=None,
    window_name=None,
    window_class=None,
    **kwargs,
):
    """Poll for windows and set STEAM_GAME. Best-effort across X11/Xwayland."""
    waiter = sleep or time.sleep
    marked = []
    lookup_kwargs = {k: kwargs[k] for k in ("xdotool", "wmctrl", "runner", "display") if k in kwargs}
    for _ in range(max(1, int(attempts))):
        candidates = []
        if pid is not None:
            candidates.extend(_window_ids_for_pid(pid, **lookup_kwargs))
        if window_name:
            candidates.extend(
                _window_ids_by_name(
                    window_name,
                    xdotool=kwargs.get("xdotool"),
                    runner=kwargs.get("runner"),
                    display=kwargs.get("display"),
                )
            )
        if window_class:
            candidates.extend(
                _window_ids_by_class(
                    window_class,
                    xdotool=kwargs.get("xdotool"),
                    runner=kwargs.get("runner"),
                    display=kwargs.get("display"),
                )
            )
        for window_id in candidates:
            if window_id in marked:
                continue
            if set_steam_game_prop(
                window_id,
                app_id,
                xprop=kwargs.get("xprop"),
                display=kwargs.get("display"),
                runner=kwargs.get("runner"),
            ):
                marked.append(window_id)
        if marked:
            break
        waiter(delay)
    return marked


def is_steam_launch(args):
    """True when argv launches through Steam (Input/overlay stay with Steam)."""
    if not args:
        return False
    joined = " ".join(str(part) for part in args).casefold()
    if "steam://" in joined or "-applaunch" in joined or "rungameid" in joined:
        return True
    first = Path(str(args[0])).name.casefold()
    if first in {"steam", "steam.sh"}:
        return True
    if first == "flatpak" and any("steam" in str(part).casefold() for part in args):
        return True
    if first == "xdg-open" and len(args) > 1 and "steam://" in str(args[1]).casefold():
        return True
    return False
