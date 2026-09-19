"""Cross-platform shims for the Linux-first OpenBox runtime (ADR 0013).

The runtime stays dependency-free: every helper here is standard library
only, including the Windows ctypes bindings.  Modules import this shim
instead of using POSIX-only primitives directly so the same code runs on
Linux (its primary target) and Windows (the port target).

Coverage note: branches guarded by ``if IS_WINDOWS`` carry a
``# pragma: no cover`` marker because the Linux coverage gate cannot
execute them.  The Windows CI job runs the same tests against the same
source, so those branches are exercised even though they are excluded
from the Linux coverage report.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"
IS_MACOS = sys.platform == "darwin"

WINDOWS_EXECUTABLE_SUFFIXES = frozenset({".exe", ".bat", ".cmd", ".com", ".lnk", ".ps1"})

DEFAULT_LOCK_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Data and configuration directories
# ---------------------------------------------------------------------------


def default_data_dir() -> Path:
    """Return the per-user OpenBox data directory for this platform."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "openbox-game-launcher"
        home = home_dir()
        if home is not None:
            return home / "AppData" / "Local" / "openbox-game-launcher"
        return Path(os.getcwd()) / "openbox-game-launcher"
    else:  # pragma: no cover if IS_WINDOWS
        home = home_dir()
        if home is not None:
            return home / ".local" / "share" / "openbox-game-launcher"
        return Path(os.getcwd()) / "openbox-game-launcher"


def home_dir() -> Path | None:
    """Return the current user's home directory, or None when undeterminable.

    ``Path.home()`` raises when the environment has no home variable (for
    example a stripped-down service environment); optional discovery must
    degrade instead of failing startup.
    """
    try:
        return Path.home()
    except (RuntimeError, OSError):
        return None


def env_file_roots() -> list[Path]:
    """Return the directories searched for an optional credentials ``.env``."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        roots = []
        home = home_dir()
        if home is not None:
            roots.append(home)
        for variable in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                roots.append(Path(base) / "openbox-game-launcher")
        return roots
    else:  # pragma: no cover if IS_WINDOWS
        home = home_dir()
        roots = []
        if home is not None:
            roots.extend([home, home / ".config" / "openbox-game-launcher"])
        return roots


def start_menu_programs_dir() -> Path | None:
    """Return the per-user Start Menu Programs folder, or None off Windows.

    Desktop integration on Windows means a Start Menu shortcut, so the folder
    has to come from the platform the same way ``.local/share`` does on POSIX.
    """
    if not IS_WINDOWS:
        return None
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def windows_install_dir() -> Path | None:
    """Return the root of a portable Windows install, or None off Windows.

    The launchers and the installer treat this as the bin root; the runtime
    itself lives in ``share/openbox`` beneath it.
    """
    if not IS_WINDOWS:
        return None
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "OpenBox"


# ---------------------------------------------------------------------------
# Advisory file locking
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lock_handle(handle, *, exclusive: bool = True, timeout: float = DEFAULT_LOCK_TIMEOUT, poll: float = 0.05):
    """Hold an advisory whole-file lock on an open file object.

    POSIX uses ``fcntl.flock``; Windows uses ``msvcrt.locking`` on the first
    byte.  Both release automatically when the owning process dies, so a
    crashed server never leaves a stale lock behind.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        import msvcrt

        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the OpenBox data lock.") from None
                time.sleep(poll)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    else:  # pragma: no cover if IS_WINDOWS
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path, *, exclusive: bool = True, timeout: float = DEFAULT_LOCK_TIMEOUT):
    """Open *path* and hold the cross-process lock for the context duration."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        with lock_handle(handle, exclusive=exclusive, timeout=timeout):
            yield


# ---------------------------------------------------------------------------
# Process liveness and termination
# ---------------------------------------------------------------------------


def process_alive(pid) -> bool:
    """Return True when *pid* still names a live process.

    On Windows this deliberately avoids ``os.kill(pid, 0)``, which is not a
    liveness probe there: CPython maps it to ``TerminateProcess``.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _windows_process_alive(pid)
    else:  # pragma: no cover if IS_WINDOWS
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def terminate_process(pid, *, force: bool = False) -> bool:
    """Terminate a single process; ``force`` skips graceful close attempts.

    PID 1 is refused: nothing OpenBox launches is the init process, and
    signalling it would take the host (or the CI runner) down instead.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        if not force and _windows_graceful_close(pid):
            return True
        return _windows_terminate_process(pid)
    else:  # pragma: no cover if IS_WINDOWS
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def terminate_process_tree(pid, *, force: bool = False, pgid=None) -> bool:
    """Terminate a process and its descendants (POSIX process group)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _windows_terminate_tree(pid, force=force)
    else:  # pragma: no cover if IS_WINDOWS
        target = _group_target(pid, pgid)
        if target is not None:
            try:
                os.killpg(target, signal.SIGKILL if force else signal.SIGTERM)
                return True
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def suspend_process_tree(pid, *, pgid=None) -> bool:
    """Suspend a process group/tree (SIGSTOP equivalent)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return all(_windows_suspend_resume(item, suspend=True) for item in [pid, *child_pids(pid)])
    else:  # pragma: no cover if IS_WINDOWS
        target = _group_target(pid, pgid)
        if target is not None:
            try:
                os.killpg(target, signal.SIGSTOP)
                return True
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGSTOP)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def resume_process_tree(pid, *, pgid=None) -> bool:
    """Resume a process group/tree (SIGCONT equivalent)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return all(_windows_suspend_resume(item, suspend=False) for item in [pid, *child_pids(pid)])
    else:  # pragma: no cover if IS_WINDOWS
        target = _group_target(pid, pgid)
        if target is not None:
            try:
                os.killpg(target, signal.SIGCONT)
                return True
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGCONT)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def _coerce_pgid(value) -> int | None:
    """Return *value* as a signalable process-group id, or None.

    Only ints and digit strings are trusted.  Any other object reaches
    ``int()`` through ``__int__`` and silently becomes 0 or 1 (``int`` of a
    bare object or a test double is 1), which are the caller's own group and
    the init group - signalling either takes down the host, not the game.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 1 else None


def _group_target(pid: int, pgid=None) -> int | None:
    """Return the process group to signal for *pid*, or None when unsafe.

    An explicit ``pgid`` wins only when it is a usable group id; otherwise the
    group is derived from the process itself, falling back to the PID.
    """
    explicit = _coerce_pgid(pgid)
    if explicit is not None:
        return explicit
    derived = process_group_id(pid)
    if derived > 1:
        return derived
    return pid if pid > 1 else None


def process_group_id(pid) -> int:
    """Return the POSIX process-group id, or the PID itself on Windows."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return 0
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return pid
    else:  # pragma: no cover if IS_WINDOWS
        try:
            return os.getpgid(pid)
        except (OSError, ProcessLookupError):
            return pid


def launch_kwargs(*, detached: bool = False, new_group: bool = True, no_window: bool = False) -> dict:
    """Return ``Popen`` keyword arguments that detach a child on this platform."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        flags = 0
        if new_group:
            flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if detached:
            flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
        if no_window:
            flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}  # pragma: no cover if IS_WINDOWS


# ---------------------------------------------------------------------------
# Process identity (session reattach)
# ---------------------------------------------------------------------------


def process_start_token(pid) -> str | None:
    """Return an opaque per-process start marker, or None when unavailable.

    POSIX uses field 22 of ``/proc/<pid>/stat``; Windows uses the process
    creation FILETIME.  Both are stable for a process's lifetime and differ
    after PID reuse, which is what session reattach compares.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _windows_start_token(pid)
    else:  # pragma: no cover if IS_WINDOWS
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[-1].split()
                return fields[19]
        except (OSError, IndexError):
            return None


def process_command_line(pid) -> str:
    """Return a short identity fingerprint for a process.

    POSIX reads ``/proc/<pid>/cmdline``; Windows has no portable command-line
    API in the standard library, so the executable image path is used as the
    fingerprint (paired with the creation token it is equally effective).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _windows_image_path(pid)[:100]
    else:  # pragma: no cover if IS_WINDOWS
        try:
            with open(f"/proc/{pid}/cmdline", encoding="utf-8") as handle:
                return handle.read().replace("\0", " ")[:100]
        except OSError:
            return ""


def process_name(pid) -> str:
    """Return the executable name for *pid* (``comm`` on POSIX)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        path = _windows_image_path(pid)
        return Path(path).name if path else ""
    else:  # pragma: no cover if IS_WINDOWS
        try:
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def process_cwd(pid) -> str:
    """Return the working directory of *pid*, or "" when it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid <= 0:
        return ""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return ""
    else:  # pragma: no cover if IS_WINDOWS
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return ""


def child_pids(pid) -> list[int]:
    """Return the direct children of *pid*."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    if pid <= 0:
        return []
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return [entry["pid"] for entry in _windows_process_table() if entry["parent"] == pid]
    else:  # pragma: no cover if IS_WINDOWS
        children = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                    fields = handle.read().rsplit(")", 1)[-1].split()
                if int(fields[1]) == pid:
                    children.append(int(name))
            except (OSError, IndexError, ValueError):
                continue
        return children


def find_pids_by_name(pattern: str) -> list[int]:
    """Return PIDs whose process name or command line contains *pattern*."""
    needle = str(pattern or "").casefold()
    if not needle:
        return []
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        matches = []
        for entry in _windows_process_table():
            name = entry["name"].casefold()
            image = entry.get("image", "").casefold()
            if needle in name or needle in image:
                matches.append(entry["pid"])
        return matches
    else:  # pragma: no cover if IS_WINDOWS
        matches = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            if needle in process_name(pid).casefold() or needle in process_command_line(pid).casefold():
                matches.append(pid)
        return matches


def find_pids_in_folder(folder) -> list[int]:
    """Return PIDs whose working directory or image lives under *folder*."""
    folder = Path(folder).expanduser()
    try:
        resolved = folder.resolve(strict=False)
    except OSError:
        return []
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        matches = []
        for entry in _windows_process_table():
            image = entry.get("image", "")
            if not image:
                continue
            try:
                Path(image).resolve(strict=False).relative_to(resolved)
                matches.append(entry["pid"])
            except (OSError, ValueError):
                continue
        return matches
    else:  # pragma: no cover if IS_WINDOWS
        matches = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        folder_str = str(resolved)
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            cwd = process_cwd(pid)
            if cwd == folder_str or cwd.startswith(folder_str + os.sep):
                matches.append(pid)
        return matches


# ---------------------------------------------------------------------------
# Command parsing and executables
# ---------------------------------------------------------------------------


def split_command(command) -> list[str]:
    """Split a stored launch command using this platform's quoting rules."""
    text = str(command or "")
    if not text.strip():
        return []
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _split_windows_command(text)
    return shlex.split(text)  # pragma: no cover if IS_WINDOWS


def join_command(parts) -> str:
    """Join an argv list into a stored launch command string."""
    values = [str(part) for part in parts]
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return subprocess.list2cmdline(values)
    return shlex.join(values)  # pragma: no cover if IS_WINDOWS


def is_executable(path) -> bool:
    """Return True when *path* can be launched as a program on this platform."""
    path = Path(path)
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return path.is_file() and path.suffix.casefold() in WINDOWS_EXECUTABLE_SUFFIXES
    return os.access(path, os.X_OK)  # pragma: no cover if IS_WINDOWS


def executable_paths(commands) -> list[str]:
    """Return the subset of *commands* that ``shutil.which`` can resolve."""
    found = []
    for command in commands:
        resolved = shutil.which(command)
        if resolved:
            found.append(resolved)
    return found


def windows_process_table() -> list[dict]:
    """Return the Windows process table, or [] on other platforms."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _windows_process_table()
    return []  # pragma: no cover if IS_WINDOWS


def _split_windows_command(command: str) -> list[str]:
    """Split a command line with the MSVCRT quoting rules.

    Kept platform-independent so the Windows parser is testable from the
    Linux gate as well as from Windows CI.
    """
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    started = False
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if char in " \t" and not in_quotes:
            if started:
                args.append("".join(current))
                current = []
                started = False
            index += 1
            continue
        if char == '"':
            if in_quotes and index + 1 < length and command[index + 1] == '"':
                current.append('"')
                index += 2
                continue
            in_quotes = not in_quotes
            started = True
            index += 1
            continue
        if char == "\\":
            backslashes = 0
            while index < length and command[index] == "\\":
                backslashes += 1
                index += 1
            if index < length and command[index] == '"':
                current.extend("\\" * (backslashes // 2))
                if backslashes % 2:
                    current.append('"')
                    index += 1
                else:
                    in_quotes = not in_quotes
                    index += 1
            else:
                current.extend("\\" * backslashes)
            started = True
            continue
        current.append(char)
        started = True
        index += 1
    if in_quotes or started:
        args.append("".join(current))
    return args


# ---------------------------------------------------------------------------
# Desktop openers
# ---------------------------------------------------------------------------


def open_path(target) -> None:
    """Open a file, folder, or URI with the platform's default handler."""
    text = str(target)
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        os.startfile(text)
        return
    if IS_MACOS:  # pragma: no cover - macOS is not a covered target
        subprocess.Popen(["open", text], **launch_kwargs())
        return
    else:  # pragma: no cover if IS_WINDOWS
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("xdg-open is required to open files and folders.")
        subprocess.Popen([opener, text], **launch_kwargs())


def reveal_path(target) -> None:
    """Reveal *target* in the platform file manager."""
    path = Path(target).expanduser()
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        if path.is_file():
            subprocess.Popen(
                ["explorer", "/select,", os.path.normpath(str(path))],
                **launch_kwargs(),
            )
        else:
            os.startfile(os.path.normpath(str(path)))
        return
    else:  # pragma: no cover if IS_WINDOWS
        folder = path.parent if path.is_file() else path
        open_path(folder)


def open_url(url) -> None:
    """Open *url* in the user's browser, raising when no handler exists."""
    text = str(url)
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        os.startfile(text)
        return
    if IS_MACOS:  # pragma: no cover - macOS is not a covered target
        subprocess.Popen(["open", text], **launch_kwargs())
        return
    else:  # pragma: no cover if IS_WINDOWS
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, text], **launch_kwargs())
            return
        import webbrowser

        if not webbrowser.open(text):
            raise OSError("No application is available to open links.")


def browser_application_commands() -> list[str]:
    """Return candidate Chromium-family browser executables for app windows."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        candidates = []
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        for base_var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(base_var)
            if not base:
                continue
            for relative in (
                ("Microsoft", "Edge", "Application", "msedge.exe"),
                ("Google", "Chrome", "Application", "chrome.exe"),
                ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            ):
                candidates.append(str(Path(base).joinpath(*relative)))
        candidates.extend(
            ["msedge.exe", "chrome.exe", "brave.exe", "chromium.exe", "firefox.exe"]
        )
        if local_appdata:
            candidates.append(str(Path(local_appdata) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
        return candidates
    if IS_MACOS:  # pragma: no cover - macOS is not a covered target
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "firefox",
        ]
    else:  # pragma: no cover if IS_WINDOWS
        return [
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "brave-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
            "firefox",
        ]


def resolve_browser_application() -> str | None:
    """Return the first installed Chromium-family browser, if any."""
    for candidate in browser_application_commands():
        path = Path(candidate)
        if path.is_absolute() or os.sep in candidate:
            if path.is_file():
                return str(path)
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


# ---------------------------------------------------------------------------
# Display names / diagnostics
# ---------------------------------------------------------------------------


def platform_label() -> str:
    """Return the marketing label for the current platform."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return "Windows"
    if IS_MACOS:  # pragma: no cover - macOS is not a covered target
        return "macOS"
    return "Linux"  # pragma: no cover if IS_WINDOWS


def python_command() -> str:
    """Return the interpreter name for user-facing instructions.

    Windows only ships ``python3`` as a Microsoft Store alias that cannot run
    the runtime, so every printed hint must say ``python`` there.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return "python"
    return "python3"  # pragma: no cover if IS_WINDOWS


# ---------------------------------------------------------------------------
# File privacy helpers
# ---------------------------------------------------------------------------


def file_is_private(info) -> bool:
    """Return True when a ``stat`` result describes an owner-only file.

    Windows has no POSIX mode bits; files under the per-user profile
    inherit the user's ACL, so the regular-file and symlink checks are the
    meaningful part there.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return True
    else:  # pragma: no cover if IS_WINDOWS
        try:
            return info.st_uid == os.geteuid() and not info.st_mode & 0o077
        except AttributeError:
            return not info.st_mode & 0o077


def is_group_or_world_writable(info) -> bool:
    """Return True when a ``stat`` result grants write access beyond the owner.

    POSIX mode bits have no Windows equivalent, so the check reports False
    there rather than rejecting every directory.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return False
    return bool(info.st_mode & 0o022)  # pragma: no cover if IS_WINDOWS


def owned_by_current_user(info) -> bool:
    """Return True when a ``stat`` result belongs to the current user."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return True
    else:  # pragma: no cover if IS_WINDOWS
        try:
            return info.st_uid == os.geteuid()
        except AttributeError:  # pragma: no cover - defensive; POSIX always has geteuid
            return True


# ---------------------------------------------------------------------------
# Steam discovery (shared by importers, saves, and storefront helpers)
# ---------------------------------------------------------------------------


def steam_install_roots() -> list[Path]:
    """Return detected Steam installation roots for this platform."""
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        roots: list[Path] = []
        for hive, key in (
            (0x80000001, r"Software\Valve\Steam"),  # HKCU
            (0x80000002, r"Software\Valve\Steam"),  # HKLM
            (0x80000002, r"Software\WOW6432Node\Valve\Steam"),
        ):
            try:
                import winreg

                with winreg.OpenKey(hive, key) as handle:
                    value, _kind = winreg.QueryValueEx(handle, "SteamPath")
                if value:
                    roots.append(Path(value))
            except OSError:
                continue
        for base_var in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(base_var)
            if base:
                roots.append(Path(base) / "Steam")
        home = Path.home()
        roots.extend([home / "Steam", home / ".steam" / "steam"])
        seen: set[str] = set()
        unique: list[Path] = []
        for root in roots:
            key = os.path.normcase(str(root))
            if key in seen or not root.is_dir():
                continue
            seen.add(key)
            unique.append(root)
        return unique
    else:  # pragma: no cover if IS_WINDOWS
        home = Path.home()
        candidates = [
            home / ".local" / "share" / "Steam",
            home / ".steam" / "steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
        ]
        return [candidate for candidate in candidates if candidate.is_dir()]


def epic_manifest_paths() -> list[Path]:
    """Return Epic Games Launcher manifest files on Windows."""
    if not IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return []
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    manifests = Path(program_data) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    if not manifests.is_dir():
        return []
    try:
        return sorted(manifests.glob("*.item"))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Windows implementation details
# ---------------------------------------------------------------------------

if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SUSPEND_RESUME = 0x0800
    _SYNCHRONIZE = 0x00100000
    _WAIT_TIMEOUT = 0x00000102
    _MAX_PATH = 260

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * _MAX_PATH),
        ]

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtSuspendProcess.restype = ctypes.c_long
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    def _windows_open(pid: int, access: int):
        return _kernel32.OpenProcess(access, False, int(pid))

    def _windows_process_alive(pid: int) -> bool:
        handle = _windows_open(pid, _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return False
        try:
            return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_terminate_process(pid: int) -> bool:
        handle = _windows_open(pid, _PROCESS_TERMINATE)
        if not handle:
            return False
        try:
            return bool(_kernel32.TerminateProcess(handle, 1))
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_graceful_close(pid: int) -> bool:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T"],
                capture_output=True,
                check=False,
                **launch_kwargs(no_window=True),
            )
        except Exception:  # noqa: BLE001 - termination must never raise from probing
            return False
        if result.returncode != 0:
            return False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if not _windows_process_alive(pid):
                return True
            time.sleep(0.1)
        return False

    def _windows_terminate_tree(pid: int, *, force: bool) -> bool:
        if not force and _windows_graceful_close(pid):
            return True
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                capture_output=True,
                check=False,
                **launch_kwargs(no_window=True),
            )
        except Exception:  # noqa: BLE001 - termination must never raise from probing
            result = None
        if result is not None and result.returncode == 0:
            return True
        return _windows_terminate_process(pid)

    def _windows_start_token(pid: int) -> str | None:
        handle = _windows_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return None
        try:
            creation = _FILETIME()
            exit_time = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_image_path(pid: int) -> str:
        handle = _windows_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_process_table() -> list[dict]:
        snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot == _INVALID_HANDLE_VALUE:
            return []
        entries: list[dict] = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            if not _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return entries
            while True:
                pid = int(entry.th32ProcessID)
                name = entry.szExeFile
                entries.append(
                    {
                        "pid": pid,
                        "parent": int(entry.th32ParentProcessID),
                        "name": name,
                        "image": _windows_image_path(pid) if pid else "",
                    }
                )
                if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            _kernel32.CloseHandle(snapshot)
        return entries

    def _windows_suspend_resume(pid: int, *, suspend: bool) -> bool:
        handle = _windows_open(pid, _PROCESS_SUSPEND_RESUME)
        if not handle:
            return False
        try:
            status = (
                _ntdll.NtSuspendProcess(handle) if suspend else _ntdll.NtResumeProcess(handle)
            )
            return status == 0
        finally:
            _kernel32.CloseHandle(handle)
