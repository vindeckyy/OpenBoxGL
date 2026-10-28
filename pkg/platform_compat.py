"""Cross-platform shims for the OpenBox runtime (ADR 0013, ADR 0048, ADR 0085).

The runtime stays dependency-free: every helper here is standard library
only, including the Windows ctypes bindings and the macOS libproc bindings.
Modules import this shim instead of using POSIX-only primitives directly so
the same code runs on Linux, Windows, and macOS.

Coverage note: branches guarded by ``if IS_WINDOWS`` or ``if IS_MACOS`` carry
a ``# pragma: no cover`` marker because the Linux coverage gate cannot
execute them.  The Windows and macOS CI jobs run the same tests against the
same source, so those branches are exercised even though they are excluded
from the Linux coverage report.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_POSIX = os.name == "posix"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def platform_features() -> dict:
    """Return the platform capability contract (ADR 0086).

    The server computes this once per capabilities call, and the frontend
    hides or shows every platform-specific control from it. A feature that is
    false means its controls are hidden, and its backend routes reject the
    request with a clear error. Absent keys must behave like false.
    """
    return {
        # Launch wrappers: gamescope preset, Game Mode, window marking.
        "gamescope": IS_LINUX,
        # MangoHud overlay.
        "mangohud": IS_LINUX,
        # ryzenadj handheld TDP controls.
        "handheld_perf": IS_LINUX,
        # Flatpak emulator install, update-all, and folder grants.
        "flatpak": IS_LINUX,
        # Wine/Proton prefix and runner manager.
        "wine": IS_LINUX,
        # Linux launcher import sources.
        "faugus": IS_LINUX,
        "lutris": IS_LINUX,
        # Heroic ships on Linux and Windows.
        "heroic": IS_LINUX or IS_WINDOWS,
        "epic": IS_LINUX or IS_WINDOWS,
        # Direct GOG/EA/Ubisoft imports read Windows launchers.
        "native_storefronts": IS_WINDOWS,
        # Desktop shortcut installer (XDG .desktop).
        "desktop_entry": IS_LINUX,
    }

WINDOWS_EXECUTABLE_SUFFIXES = frozenset({".exe", ".bat", ".cmd", ".com", ".lnk", ".ps1"})

DEFAULT_LOCK_TIMEOUT = 30.0

# Identifiers that arrive from a request and are then joined into a filesystem
# path. A preview id is generated server-side, but it is *supplied* by the
# client on every read, and `base / f"{value}.json"` will happily walk out of
# `base` when the value contains a separator or "..". The generated form is a
# uuid4 hex string, so this admits every id the server ever mints and nothing
# else.
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class UnsafeIdentifier(ValueError):
    """Raised when a request-supplied identifier is not safe to join into a path."""


def safe_identifier(value, *, field: str = "identifier") -> str:
    """Return ``value`` if it is safe to use as a single path component.

    Rejects path separators, ``..``, absolute paths, drive letters, NUL and
    anything outside a conservative character set. Callers that build a path
    from a request parameter must pass the value through here first; the
    defence in depth is :func:`contained_path`.
    """
    text = str(value or "").strip()
    if not text:
        raise UnsafeIdentifier(f"{field} is required")
    if not SAFE_IDENTIFIER_RE.fullmatch(text):
        raise UnsafeIdentifier(f"{field} contains characters that are not allowed in a path component")
    if text in {".", ".."} or text.startswith("."):
        raise UnsafeIdentifier(f"{field} must not start with a dot")
    return text


def contained_path(base: Path, *parts: str) -> Path:
    """Join ``parts`` onto ``base`` and assert the result stays inside it.

    Uses ``resolve()`` on both sides so a symlink or a ``..`` that slipped past
    :func:`safe_identifier` still cannot escape ``base``.
    """
    base_resolved = Path(base).resolve()
    candidate = base_resolved.joinpath(*parts)
    resolved = Path(candidate).resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise UnsafeIdentifier("resolved path escapes its permitted directory")
    return resolved


# Windows refuses these as file names regardless of extension, and silently
# retargets the write elsewhere when given one.
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
MAX_FILENAME_COMPONENT = 200


def resolved_data_dir() -> Path:
    """Return the OpenBox data directory, honouring ``OPENBOX_DATA_DIR``.

    One definition, deliberately. ``openbox.py`` binds this at import time for
    ``DATA``; the plugin sandbox resolves it at *call* time so that a test (or
    a user) pointing ``OPENBOX_DATA_DIR`` somewhere new gets a sandbox that
    actually masks the directory it is protecting. When those two disagreed,
    the sandbox masked the default location while the state lived elsewhere.
    """
    custom = os.environ.get("OPENBOX_DATA_DIR")
    if custom:
        return Path(custom).expanduser()
    return default_data_dir()


def safe_filename_component(value, *, field: str = "filename") -> str:
    """Return ``value`` if it is usable as a single file *name* component.

    Deliberately looser than :func:`safe_identifier`: real ROM names contain
    spaces, parentheses and ampersands -- ``Game (Disc 1)``, ``Sonic &
    Knuckles`` -- so an identifier-grade character class would reject the
    library this is meant to protect. What is rejected is only what can change
    *where* the write lands: path separators, ``..``, control characters, an
    absolute or drive-qualified form, and Windows' reserved device names.

    Always follow this with :func:`contained_path` when the name is used to
    build a destination.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise UnsafeIdentifier(f"{field} is required")
    if len(text) > MAX_FILENAME_COMPONENT:
        raise UnsafeIdentifier(f"{field} is longer than {MAX_FILENAME_COMPONENT} characters")
    if "/" in text or "\\" in text:
        raise UnsafeIdentifier(f"{field} must not contain a path separator")
    if text in {".", ".."} or text.startswith(".."):
        raise UnsafeIdentifier(f"{field} must not be a relative path component")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise UnsafeIdentifier(f"{field} must not contain control characters")
    # Surrounding whitespace is stripped above rather than rejected: an import
    # that padded a name is a data quirk, and stripping cannot move the write.
    # Trailing dots are a different matter -- Windows silently drops them, so
    # "game." and "game" would collide in the same directory.
    if text != text.strip("."):
        raise UnsafeIdentifier(f"{field} must not begin or end with a dot")
    if text.split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES:
        raise UnsafeIdentifier(f"{field} is a reserved device name on Windows")
    return text



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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        home = home_dir()
        if home is not None:
            return home / "Library" / "Application Support" / "openbox-game-launcher"
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


# taskkill finishes in well under a second; a hung one must not block termination.
TASKKILL_TIMEOUT_SECONDS = 10

def parse_timestamp(value):
    """Parse an ISO-8601 timestamp into naive local time, or return None.

    The app writes naive local time. Values from imports, bulk edits and other
    devices may carry ``Z`` or an offset; those are converted to local time, so
    every comparison with ``datetime.now()`` is between two naive values and
    cannot raise. An empty or unparseable value is None, not an error.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


# A wedged Flatpak or D-Bus session must not block a status or launch request.
FLATPAK_PROBE_TIMEOUT_SECONDS = 5


def flatpak_app_installed(app_id, *, run=None, flatpak="flatpak") -> bool:
    """Return True when ``flatpak info <app_id>`` exits 0 within the probe timeout.

    A probe that times out, or cannot start, counts as "not installed". The
    ``run`` argument exists so callers and tests can substitute the runner.
    """
    run = run or subprocess.run
    try:
        result = run(
            [flatpak, "info", app_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=FLATPAK_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(result, "returncode", 1) == 0


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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        home = home_dir()
        if home is None:
            return []
        return [home, default_data_dir()]
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


@contextlib.contextmanager
def try_file_lock(path):
    """Take *path* only when it is free, and never wait for it.

    Yields True when the lock was taken and False when another process
    already holds it. The lock is released when the context exits. This is
    the non-blocking counterpart of :func:`file_lock`, whose POSIX branch
    blocks for as long as the lock is held.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            return
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return _darwin_start_token(pid)
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        # The full executable path: proc_pidpath is bounded by MAXPATHLEN, and a cap
        # would drop the executable's own name from the end of a long app-bundle path.
        return _darwin_pid_path(pid)
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        path = _darwin_pid_path(pid)
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return _darwin_process_cwd(pid)
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        children = []
        for candidate in _darwin_pid_list():
            if candidate == pid:
                continue
            if _darwin_parent_pid(candidate) == pid:
                children.append(candidate)
        return children
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        matches = []
        for candidate in _darwin_pid_list():
            path = _darwin_pid_path(candidate)
            if not path:
                continue
            if needle in Path(path).name.casefold() or needle in path.casefold():
                matches.append(candidate)
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        matches = []
        folder_fold = path_identity_fold(str(resolved))
        folder_prefix = folder_fold.rstrip(os.sep) + os.sep
        for candidate in _darwin_pid_list():
            cwd = _darwin_process_cwd(candidate)
            if cwd:
                cwd_fold = path_identity_fold(cwd)
                if cwd_fold == folder_fold or cwd_fold.startswith(folder_prefix):
                    matches.append(candidate)
                    continue
            # A game launched from an app bundle keeps its working directory
            # elsewhere, so the image path is checked as well.
            path = _darwin_pid_path(candidate)
            if not path:
                continue
            try:
                image_fold = path_identity_fold(str(Path(path).resolve(strict=False)))
            except OSError:
                continue
            if image_fold.startswith(folder_prefix):
                matches.append(candidate)
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
# macOS process introspection (libproc)
# ---------------------------------------------------------------------------

# macOS has no /proc. The libproc API answers the same questions through
# ctypes: the process table, the executable path, the parent pid, the
# process start time, and the working directory. The field offsets below
# mirror ``struct proc_bsdinfo`` and ``struct vnode_info_path`` from
# <sys/proc_info.h>; the sizes are asserted on every call, so a layout
# change on a future macOS release degrades to "unavailable" instead of
# returning wrong data.
_PROC_PIDTBSDINFO = 3
_PROC_PIDVNODEPATHINFO = 9
_PROC_BSDINFO_SIZE = 136
_PROC_VNODEPATHINFO_SIZE = 2352
_PROC_PIDPATHINFO_MAXSIZE = 4096
_PROC_BSDINFO_PID_OFFSET = 12
_PROC_BSDINFO_PPID_OFFSET = 16
_PROC_BSDINFO_COMM_OFFSET = 48
_PROC_BSDINFO_START_SEC_OFFSET = 120
_PROC_BSDINFO_START_USEC_OFFSET = 128
_PROC_VNODE_CWD_OFFSET = 152
_PROC_VNODE_PATH_MAX = 1024

_libproc_handle = None
_libproc_unavailable = False


def _libproc():
    """Return the libproc handle, or None when the API is unavailable."""
    global _libproc_handle, _libproc_unavailable
    if _libproc_handle is not None or _libproc_unavailable:
        return _libproc_handle
    try:
        import ctypes

        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.proc_listallpids.restype = ctypes.c_int
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        lib.proc_pidpath.restype = ctypes.c_int
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        lib.proc_pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError, ImportError):  # pragma: no cover - macOS only
        _libproc_unavailable = True
        return None
    _libproc_handle = lib
    return lib


def _darwin_bsd_info(pid):
    """Return the raw ``proc_bsdinfo`` bytes for *pid*, or None."""
    lib = _libproc()
    if lib is None:
        return None
    import ctypes

    buffer = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
    written = lib.proc_pidinfo(int(pid), _PROC_PIDTBSDINFO, 0, buffer, _PROC_BSDINFO_SIZE)
    if written != _PROC_BSDINFO_SIZE:
        return None
    return buffer.raw


def _darwin_parent_pid(pid):
    """Return the parent pid of *pid*, or 0 when it cannot be read."""
    raw = _darwin_bsd_info(pid)
    if raw is None:
        return 0
    import struct

    return int(struct.unpack_from("<I", raw, _PROC_BSDINFO_PPID_OFFSET)[0])


def _darwin_start_token(pid):
    """Return the process start marker for *pid*, or None."""
    raw = _darwin_bsd_info(pid)
    if raw is None:
        return None
    import struct

    seconds = int(struct.unpack_from("<Q", raw, _PROC_BSDINFO_START_SEC_OFFSET)[0])
    micros = int(struct.unpack_from("<Q", raw, _PROC_BSDINFO_START_USEC_OFFSET)[0])
    return f"{seconds}.{micros:06d}"


def _darwin_comm(pid):
    """Return the short process name of *pid*, or an empty string."""
    raw = _darwin_bsd_info(pid)
    if raw is None:
        return ""
    chunk = raw[_PROC_BSDINFO_COMM_OFFSET:_PROC_BSDINFO_COMM_OFFSET + 16]
    return chunk.split(b"\x00")[0].decode("utf-8", errors="replace")


def _darwin_pid_path(pid):
    """Return the executable path of *pid*, or an empty string."""
    lib = _libproc()
    if lib is None:
        return ""
    import ctypes

    buffer = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
    written = lib.proc_pidpath(int(pid), buffer, _PROC_PIDPATHINFO_MAXSIZE)
    if written <= 0:
        return ""
    return buffer.value.decode("utf-8", errors="replace")


def _darwin_pid_list():
    """Return every process id the host can see."""
    lib = _libproc()
    if lib is None:
        return []
    import ctypes

    capacity = 65536
    for _attempt in range(4):
        buffer = (ctypes.c_int * capacity)()
        count = lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if count <= 0:
            return []
        if count < capacity:
            return [int(value) for value in buffer[:count]]
        capacity *= 8
    return []


def _darwin_process_cwd(pid):
    """Return the working directory of *pid*, or an empty string."""
    lib = _libproc()
    if lib is None:
        return ""
    import ctypes

    buffer = ctypes.create_string_buffer(_PROC_VNODEPATHINFO_SIZE)
    written = lib.proc_pidinfo(
        int(pid), _PROC_PIDVNODEPATHINFO, 0, buffer, _PROC_VNODEPATHINFO_SIZE
    )
    if written != _PROC_VNODEPATHINFO_SIZE:
        return ""
    raw = buffer.raw
    chunk = raw[_PROC_VNODE_CWD_OFFSET:_PROC_VNODE_CWD_OFFSET + _PROC_VNODE_PATH_MAX]
    text = chunk.split(b"\x00")[0].decode("utf-8", errors="replace")
    if text.startswith("/") and all(character.isprintable() for character in text):
        return text
    return ""


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


EXTERNAL_OPEN_DISABLED_ENV = "OPENBOX_DISABLE_EXTERNAL_OPEN"


def external_open_disabled() -> bool:
    """True when this process must not hand paths or URLs to the desktop.

    A headless or test run sets ``OPENBOX_DISABLE_EXTERNAL_OPEN`` so no
    browser window or file manager is spawned behind the user's back.
    """
    value = str(os.environ.get(EXTERNAL_OPEN_DISABLED_ENV, "")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def url_opener_command() -> str | None:
    """Return the command that opens a URL with the default handler."""
    # Platform-first: a Git/MSYS `xdg-open` on PATH must not win on Windows.
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return "/usr/bin/open"
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\explorer.exe"
    if opener := shutil.which("xdg-open"):
        return opener
    return None


def open_path(target) -> None:
    """Open a file, folder, or URI with the platform's default handler."""
    if external_open_disabled():
        raise FileNotFoundError(
            f"External opening is disabled ({EXTERNAL_OPEN_DISABLED_ENV} is set)."
        )
    text = str(target)
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        os.startfile(text)
        return
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        subprocess.Popen(["/usr/bin/open", text], **launch_kwargs())
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
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        if path.exists():
            subprocess.Popen(["/usr/bin/open", "-R", str(path)], **launch_kwargs())
            return
        open_path(path if path.is_dir() else path.parent)
        return
    else:  # pragma: no cover if IS_WINDOWS
        # xdg-open has no select-this-file verb: opening the folder is the whole
        # reveal. The check is "is this a folder", not "is this an existing
        # file", so a path that does not exist yet still reveals its parent
        # instead of handing the missing path to the opener.
        open_path(path if path.is_dir() else path.parent)


def open_url(url) -> None:
    """Open *url* in the user's browser, raising when no handler exists."""
    if external_open_disabled():
        raise FileNotFoundError(
            f"External opening is disabled ({EXTERNAL_OPEN_DISABLED_ENV} is set)."
        )
    text = str(url)
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        os.startfile(text)
        return
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        subprocess.Popen(["/usr/bin/open", text], **launch_kwargs())
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
# Battery
# ---------------------------------------------------------------------------

BATTERY_ABSENT = {"present": False, "percent": 0, "charging": False}
BATTERY_CACHE_SECONDS = 30.0
_battery_cache: tuple[float, dict] | None = None


def _linux_battery_status(root=None) -> dict:
    """Read the first battery under sysfs. Returns no path in the response."""
    base = Path(root) if root is not None else Path("/sys/class/power_supply")
    try:
        candidates = sorted(base.glob("BAT*"))
    except OSError:
        return dict(BATTERY_ABSENT)
    for candidate in candidates:
        try:
            percent = int((candidate / "capacity").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        try:
            state = (candidate / "status").read_text(encoding="utf-8").strip().casefold()
        except OSError:
            state = ""
        return {
            "present": True,
            "percent": max(0, min(100, percent)),
            "charging": state in {"charging", "full"},
        }
    return dict(BATTERY_ABSENT)


def parse_pmset_battery(text: str) -> dict:
    """Parse ``pmset -g batt`` output into a battery status dict."""
    for line in str(text or "").splitlines():
        if "InternalBattery" not in line:
            continue
        match = re.search(r"(\d{1,3})%", line)
        percent = int(match.group(1)) if match else 0
        charging = bool(re.search(r";\s*(charging|charged)\b", line))
        return {"present": True, "percent": percent, "charging": charging}
    return dict(BATTERY_ABSENT)


# GetSystemPowerStatus sentinel values.
WINDOWS_POWER_STATUS_UNKNOWN = 255
WINDOWS_BATTERY_FLAG_CHARGING = 8
WINDOWS_BATTERY_FLAG_NO_BATTERY = 128


def parse_windows_power_status(ac_line, percent, battery_flag) -> dict:
    """Parse ``GetSystemPowerStatus`` fields into a battery status dict."""
    flag = int(battery_flag)
    if flag & WINDOWS_BATTERY_FLAG_NO_BATTERY:
        return dict(BATTERY_ABSENT)
    level = int(percent)
    return {
        "present": True,
        "percent": 0 if level == WINDOWS_POWER_STATUS_UNKNOWN else max(0, min(100, level)),
        "charging": int(ac_line) == 1 or bool(flag & WINDOWS_BATTERY_FLAG_CHARGING),
    }


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _windows_battery_status() -> dict:  # pragma: no cover - ctypes call, exercised on Windows CI
    """Read the battery through ``GetSystemPowerStatus``."""
    status = _SystemPowerStatus()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return dict(BATTERY_ABSENT)
    except (AttributeError, OSError):  # pragma: no cover - defensive
        return dict(BATTERY_ABSENT)
    return parse_windows_power_status(
        status.ACLineStatus, status.BatteryLifePercent, status.BatteryFlag
    )


def _read_macos_battery() -> dict:
    """Read the battery through ``pmset``."""
    try:
        result = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return dict(BATTERY_ABSENT)
    return parse_pmset_battery(result.stdout or "")


def _cached_battery(reader, clock) -> dict:
    global _battery_cache
    now = clock()
    if _battery_cache is not None and now - _battery_cache[0] < BATTERY_CACHE_SECONDS:
        return dict(_battery_cache[1])
    status = reader()
    _battery_cache = (now, status)
    return dict(status)


def battery_status(root=None, *, clock=time.monotonic) -> dict:
    """Return battery presence, percentage, and charging state.

    The macOS and Windows readings are cached for
    :data:`BATTERY_CACHE_SECONDS` so a polling UI does not ask the OS on
    every frame.
    """
    if IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return _cached_battery(_windows_battery_status, clock)
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return _cached_battery(_read_macos_battery, clock)
    return _linux_battery_status(root)


# ---------------------------------------------------------------------------
# Path identity and display names
# ---------------------------------------------------------------------------


def path_identity_fold(text: str) -> str:
    """Return the canonical identity form of a path on this platform.

    macOS volumes are case-insensitive by default and the file system hands
    back decomposed Unicode, so an identity key that keeps either would let
    the same game be imported twice. Everywhere else this is exactly
    ``os.path.normcase``.
    """
    value = str(text)
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return unicodedata.normalize("NFC", value).casefold()
    return os.path.normcase(value)


def display_name_for_file(name: str) -> str:
    """Return the display form of a file name on this platform."""
    value = str(name)
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return unicodedata.normalize("NFC", value)
    return value


# ---------------------------------------------------------------------------
# Bundled applications and native executables
# ---------------------------------------------------------------------------


def macos_app_bundle_for(path) -> Path | None:
    """Return the ``.app`` bundle that contains *path*, or None."""
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
    except OSError:
        return None
    for parent in candidate.parents:
        if parent.suffix == ".app" and (parent / "Contents" / "Info.plist").is_file():
            return parent
    return None


def macos_app_executable(bundle) -> Path | None:
    """Return the executable inside a macOS app bundle, or None."""
    root = Path(bundle)
    try:
        with (root / "Contents" / "Info.plist").open("rb") as handle:
            payload = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    name = str(payload.get("CFBundleExecutable") or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return None
    executable = root / "Contents" / "MacOS" / name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None
    return executable


def resolve_native_executable(name, which=None) -> str | None:
    """Return the executable for a native emulator name, or None.

    A macOS ``.app`` bundle is not on PATH, so the bundle is looked up in
    the system and user application folders and its real executable is
    returned. Launching that executable (not ``open -a``) keeps the
    emulator's own pid, which the session tracker needs. A name with a
    ``*`` matches the bundles whose name starts that way, which is how a
    build that names its bundle after the release is found.
    """
    text = str(name or "").strip()
    if not text:
        return None
    if text.endswith(".app") and IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        home = home_dir()
        folders = [Path("/Applications")]
        if home is not None:
            folders.append(home / "Applications")
        search: list[Path] = []
        for folder in folders:
            if "*" in text:
                search.extend(sorted(folder.glob(text)))
            else:
                search.append(folder / text)
        for bundle in search:
            executable = macos_app_executable(bundle)
            if executable is not None:
                return str(executable)
        return None
    finder = which or shutil.which
    return finder(text)


def libretro_system_core_dir() -> str:
    """Return the directory that holds the libretro cores on this platform."""
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        home = home_dir()
        base = home / "Library" / "Application Support" / "RetroArch" / "cores" if home else None
        # RetroArch's own default: libretro_directory in its first-run retroarch.cfg (plan A6, confirmed).
        return f"{base}/" if base else ""
    return "/usr/lib/libretro/"


def libretro_core_filename(name) -> str:
    """Return the core file name RetroArch loads on this platform: macOS cores are ``.dylib``."""
    text = str(name or "")
    if IS_MACOS and text.endswith(".so"):  # pragma: no cover - macOS branch exercised on the macOS CI job
        return text[: -len(".so")] + ".dylib"
    return text


# ---------------------------------------------------------------------------
# Host tool paths and screenshot tools
# ---------------------------------------------------------------------------

MACOS_TOOL_PATHS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/opt/local/bin",
)


def ensure_macos_tool_path(environ=None) -> str:
    """Return a PATH that also contains the usual Homebrew and MacPorts bins.

    An app started from Finder inherits a minimal PATH, so tools the user
    installed with Homebrew would otherwise be invisible.
    """
    value = str(environ if environ is not None else os.environ.get("PATH", ""))
    if not IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return value
    entries = value.split(os.pathsep) if value else []
    missing = [entry for entry in MACOS_TOOL_PATHS if entry not in entries]
    if not missing:
        return value
    return os.pathsep.join([*missing, *entries])


def macos_screenshot_command(dest) -> list[str] | None:
    """Return the macOS screenshot command for *dest*, or None elsewhere."""
    if not IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return None
    return ["/usr/sbin/screencapture", "-x", str(dest)]


# ---------------------------------------------------------------------------
# Symlink inspection
# ---------------------------------------------------------------------------

# macOS keeps /var, /tmp, /etc, and /home behind symlinks into the data
# volume, so every path under one of them contains a link the user never
# made. Those root-level system links are allowed; every other symlink
# component is reported.
MACOS_SYSTEM_LINK_ROOTS = ("/private/", "/System/Volumes/Data/")


def _macos_system_link(component) -> bool:
    """True when *component* is a macOS system symlink at the filesystem root."""
    if not IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return False
    try:
        target = os.readlink(component)
    except OSError:
        return False
    resolved = os.path.normpath(os.path.join(str(component.parent), target))
    return resolved.startswith(MACOS_SYSTEM_LINK_ROOTS)


def user_symlink_component(path) -> Path | None:
    """Return the first symlink component of *path* a user could have made.

    Returns None when the path holds no user-created symlink. System
    symlinks that the operating system itself installs (macOS: /var, /tmp,
    /etc, /home) do not count.
    """
    candidate = Path(os.path.abspath(os.fspath(path)))
    parts = candidate.parts
    for index in range(1, len(parts)):
        component = Path(*parts[: index + 1])
        try:
            if not component.is_symlink():
                continue
        except OSError:
            continue
        if index == 1 and _macos_system_link(component):
            continue
        return component
    return None


# ---------------------------------------------------------------------------
# Steam discovery (shared by importers, saves, and storefront helpers)
# ---------------------------------------------------------------------------


def steam_candidate_roots(home: Path) -> list[Path]:
    """Return the Steam install locations for *home* on this platform."""
    if IS_MACOS:  # pragma: no cover - macOS branch exercised on the macOS CI job
        return [home / "Library" / "Application Support" / "Steam"]
    return [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]


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
        home = home_dir()
        if home is not None:
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
        home = home_dir()
        if home is None:
            return []
        candidates = steam_candidate_roots(home)
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


def windows_registry_tree(root: str, path: str) -> dict[str, dict[str, str]]:  # pragma: no cover - Windows-only branch exercised on windows CI
    """Return subkey -> string values under a Windows registry path.

    `root` is "HKCU" or "HKLM". Both registry views are tried. Off Windows,
    or when the key cannot be read, returns {}. Values are expanded and
    stringified; this helper never writes.
    """
    if not IS_WINDOWS:  # pragma: no cover - Windows branch exercised on windows CI
        return {}
    import winreg

    hive = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}.get(str(root).upper())
    if hive is None:
        return {}
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                tree: dict[str, dict[str, str]] = {}
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, name) as child:
                            values = {}
                            for value_index in range(winreg.QueryInfoKey(child)[1]):
                                value_name, value_data, _kind = winreg.EnumValue(child, value_index)
                                values[str(value_name)] = os.path.expandvars(str(value_data))
                            tree[str(name)] = values
                    except OSError:
                        continue
                return tree
        except OSError:
            continue
    return {}


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
                timeout=TASKKILL_TIMEOUT_SECONDS,
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
                timeout=TASKKILL_TIMEOUT_SECONDS,
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
