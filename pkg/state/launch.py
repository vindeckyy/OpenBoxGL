"""Game launch lifecycle, process supervision, and session tracking."""

import copy
from datetime import datetime
import logging
import os
from pathlib import Path
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time

from automation import build_event
from pkg.state.registry import EVENT_SEQUENCE, PROCESS_LOCK, PROCESSES, RUNNING, SESSION_EVENTS  # noqa: F401
from backend_io import contained_path
from catalog import apply_progress_automation
from openbox import DATA, build_launch, load_state, update_state
from parity_gamescope import is_gamescope_guest, is_steam_launch, mark_process_windows, steam_game_id_for, apply_mangohud_env, merge_gamescope_preset, should_nest_gamescope
from parity_integrations import auto_attach_obs_recording
from parity_perf import apply_perf_profile, effective_profile_name, restore_perf_profile
from parity_saves import enforce_backup_limit
from parity_tracking import close_store_client, wait_for_exit
from plugins import run_plugins
from saves import backup_saves

LOGGER = logging.getLogger("openbox")


def _apply_mangohud_from_state(state):
    """Return an env dict with MangoHud enabled if the setting is on (1.7.2)."""
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    return apply_mangohud_env(enabled=bool(settings.get("mangohud_enabled", False)))


def _apply_gamescope_preset_from_state(state, args):
    """Wrap *args* with gamescope when a preset is set and nesting is safe (1.7.2).

    Returns the (possibly wrapped) args list.  When already inside a gamescope
    guest session the preset is skipped to avoid nested gamescope.
    """
    if not args:
        return args
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    preset = str(settings.get("gamescope_preset", "")).strip()
    if not preset:
        return args
    if not should_nest_gamescope(force="--game-mode" in sys.argv):
        return args
    gs_args = merge_gamescope_preset(preset)
    if not gs_args:
        return args
    return ["gamescope"] + gs_args + ["--"] + list(args)


def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def _read_proc_start_time(pid):
    """Read process start time from /proc/<pid>/stat."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[-1].split()
            return fields[19]  # starttime (field 22, 0-indexed as 19 after rparen split)
    except (OSError, IndexError):
        return None


def _read_proc_cmdline(pid):
    """Read command fingerprint from /proc/<pid>/cmdline."""
    try:
        with open(f'/proc/{pid}/cmdline') as f:
            return f.read().replace('\0', ' ')[:100]
    except OSError:
        return ''


def _verify_process_identity(session):
    """Check if a persisted session's process is still the same one."""
    read_start = _ns("_read_proc_start_time", _read_proc_start_time)
    read_cmd = _ns("_read_proc_cmdline", _read_proc_cmdline)
    pid = session.get('pid')
    if not pid:
        return False
    # Check PID exists
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    # Verify start time matches
    current_start = read_start(pid)
    if current_start != session.get('proc_start_time'):
        return False
    # Verify command fingerprint matches
    current_cmd = read_cmd(pid)
    if session.get('command_fingerprint') and current_cmd:
        # Fuzzy match — first 50 chars should match
        if current_cmd[:50] != session['command_fingerprint'][:50]:
            return False
    return True


def reconcile_sessions_on_startup(state):
    """Called once during server startup to handle sessions from previous run."""
    verify_fn = _ns("_verify_process_identity", _verify_process_identity)
    sessions = state.get('active_sessions', [])
    reattached = []
    abandoned = []
    for session in sessions:
        if verify_fn(session):
            # Reattach: start a watcher thread for this process
            reattached.append(session)
        else:
            # Mark abandoned — DON'T kill the PID
            session['status'] = 'abandoned'
            abandoned.append(session)
    # Update state: keep reattached + abandoned (abandoned shown in UI)
    state['active_sessions'] = reattached + abandoned
    return reattached, abandoned


class _ReattachedProcess:
    """Process-like view for a verified process from a previous server run."""

    def __init__(self, session):
        self.pid = int(session["pid"])
        self.pgid = int(session.get("pgid") or self.pid)
        self._identity = {
            "pid": self.pid,
            "proc_start_time": session.get("proc_start_time"),
            "command_fingerprint": session.get("command_fingerprint", ""),
        }

    def poll(self):
        verify_fn = _ns("_verify_process_identity", _verify_process_identity)
        return None if verify_fn(self._identity) else 0

    def wait(self):
        while self.poll() is None:
            time.sleep(1.0)
        return 0


class _ReattachedLease:
    """Restore a performance profile once when a reattached session ends."""

    def __init__(self, profile_name):
        self.profile_name = str(profile_name or "").strip()
        self._restored = False
        self._lock = threading.Lock()

    def restore(self):
        load_fn = _ns("load_state", load_state)
        rest_perf = _ns("restore_perf_profile", restore_perf_profile)
        with self._lock:
            if self._restored:
                return
            self._restored = True
        if self.profile_name:
            rest_perf(self.profile_name, load_fn())


def _stable_id_match(game, stable_id):
    """True when the game's current or legacy stable id equals stable_id."""
    aliases = game.get("legacy_game_ids", [])
    return (
        str(game.get("game_id") or "") == stable_id
        or isinstance(aliases, list) and stable_id in {str(value) for value in aliases}
    )


def _match_storefront_ids(games, identity):
    """Return the first game whose storefront id matches, else None."""
    for key in ("gameyfin_id", "steam_app_id", "heroic_app_id", "lutris_id"):
        value = str(identity.get(key) or "").strip()
        if not value:
            continue
        for game in games:
            if str(game.get(key) or "").strip() == value:
                return game
    return None


def _match_path_and_name(games, path, name):
    """Match by path, preferring an exact name when both are given."""
    if path:
        matches = [game for game in games if str(game.get("path", "")) == path]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1 and name:
            by_name = [game for game in matches if str(game.get("name", "")) == name]
            if len(by_name) == 1:
                return by_name[0]
    return None


def _match_fallback_index(games, fallback_index, name, path):
    """Resolve by array index with name/path sanity checks, else None."""
    if fallback_index is not None:
        try:
            candidate = games[int(fallback_index)]
        except (IndexError, TypeError, ValueError):
            return None
        candidate_name = str(candidate.get("name", ""))
        candidate_path = str(candidate.get("path", ""))
        if (not name and not path) or candidate_name == name or candidate_path == path:
            return candidate
    return None


def resolve_library_game(state, identity, fallback_index=None):
    """Find a library game by stable ids/path, not a stale array index."""
    games = state.get("games") or []
    if not isinstance(identity, dict):
        identity = {}
    stable_game_id = str(identity.get("stable_game_id") or identity.get("game_id") or "").strip()
    if stable_game_id:
        for game in games:
            if _stable_id_match(game, stable_game_id):
                return game
    storefront_match = _match_storefront_ids(games, identity)
    if storefront_match is not None:
        return storefront_match
    path = str(identity.get("game_path") or identity.get("path") or "").strip()
    name = str(identity.get("game_name") or identity.get("name") or "").strip()
    path_match = _match_path_and_name(games, path, name)
    if path_match is not None:
        return path_match
    return _match_fallback_index(games, fallback_index, name, path)


def game_from_payload(state, payload):
    """Resolve additive stable IDs first, then retain the numeric frontend ID."""
    if not isinstance(payload, dict):
        raise ValueError("Request payload must be an object.")
    games = state.get("games", [])
    game = resolve_library_game(state, payload)
    if game is not None:
        return game
    raw_id = payload.get("id")
    if raw_id is None:
        raise IndexError("Game not found")
    try:
        index = int(raw_id)
    except (TypeError, ValueError) as error:
        raise IndexError("Game not found") from error
    if index < 0 or index >= len(games):
        raise IndexError("Game not found")
    return games[index]


def game_from_query(state, query):
    payload = {"id": query.get("id", [None])[0]}
    if query.get("game_id", [""])[0]:
        payload["game_id"] = query["game_id"][0]
    return game_from_payload(state, payload)


def reattach_session(session, state=None):
    """Register a verified persisted session and resume normal lifecycle cleanup."""
    if not isinstance(session, dict):
        return False
    launch_id = str(session.get("launch_id") or "").strip()
    try:
        pid = int(session.get("pid"))
    except (TypeError, ValueError):
        return False
    if not launch_id or pid <= 0:
        return False

    load_fn = _ns("load_state", load_state)
    res_game = _ns("resolve_library_game", resolve_library_game)
    fin_sess = _ns("finish_session", finish_session)

    state = state or load_fn()
    stable_game_id = str(session.get("game_id") or "").strip()
    game = res_game(state, {"stable_game_id": stable_game_id})
    games = state.get("games", [])
    game_index = games.index(game) if game in games else -1
    game = game or {}
    started_value = str(session.get("start_time") or "").strip()
    try:
        started = datetime.fromisoformat(started_value)
    except ValueError:
        started = datetime.now()
    entry = {
        "launch_id": launch_id,
        "game_id": game_index,
        "stable_game_id": stable_game_id,
        "effective_profile": str(session.get("perf_profile") or ""),
        "game": game.get("name", "Unknown game"),
        "game_path": str(game.get("path", "")),
        "steam_app_id": str(game.get("steam_app_id") or ""),
        "heroic_app_id": str(game.get("heroic_app_id") or ""),
        "lutris_id": str(game.get("lutris_id") or ""),
        "gameyfin_id": str(game.get("gameyfin_id") or ""),
        "started": started.isoformat(timespec="seconds"),
        "pid": pid,
        "paused": False,
    }
    process = _ReattachedProcess(session)
    lease = _ReattachedLease(session.get("perf_profile", ""))
    with PROCESS_LOCK:
        RUNNING[launch_id] = entry
        PROCESSES[launch_id] = process
    threading.Thread(
        target=fin_sess,
        args=(launch_id, game_index, started, process, lease),
        daemon=True,
    ).start()
    return True


def _contained_launch_cwd(cwd, game):
    """True when a plugin-requested working directory stays inside the game or data directories."""
    data_parent = _ns("DATA", DATA).parent
    roots = [data_parent, data_parent / "cache" / "archives"]
    game_path = str(game.get("path") or "").strip()
    if game_path:
        roots.append(str(Path(game_path).expanduser().parent))
    try:
        contained_path(cwd, roots)
    except (OSError, ValueError):
        return False
    return True


def _resolve_start_game(state, index, stable_game_id):
    """Resolve the game to launch by stable id or index; returns (game, index)."""
    res_game = _ns("resolve_library_game", resolve_library_game)
    if stable_game_id:
        selected = res_game(state, {"stable_game_id": stable_game_id}, fallback_index=index)
        if selected is None:
            raise IndexError("Game not found")
        index = state["games"].index(selected)
    elif index is None or index < 0 or index >= len(state["games"]):
        raise IndexError("Game not found")
    return copy.deepcopy(state["games"][index]), index


def _terminate_owned_process(process):
    """Send SIGTERM to a launched or reattached process group."""
    if process is None:
        return
    if isinstance(process, _ReattachedProcess):
        process_group = process.pgid
    else:
        try:
            process_group = os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            process_group = getattr(process, "pid", None)
    if not process_group:
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except (OSError, ProcessLookupError, AttributeError):
            pass


def _rollback_failed_launch(launch_id, process=None):
    """Undo phases 5-7 after a post-spawn failure."""
    upd_st = _ns("update_state", update_state)
    _terminate_owned_process(process)
    with PROCESS_LOCK:
        RUNNING.pop(launch_id, None)
        PROCESSES.pop(launch_id, None)

    def mutate(state):
        state["active_sessions"] = [
            item for item in state.get("active_sessions", [])
            if not isinstance(item, dict) or item.get("launch_id") != launch_id
        ]

    try:
        upd_st(mutate)
    except Exception:
        LOGGER.exception("Failed to clear persisted session %s during launch rollback", launch_id)


def _start_launch_command(game, profiles):
    """Build the launch argv and cwd, rejecting games that cannot run."""
    bld_launch = _ns("build_launch", build_launch)
    args, cwd = bld_launch(game, profiles)
    game_command = shlex.split(str(game.get("launch", "")) or "")
    profile_command = shlex.split(str(profiles.get(game.get("platform", ""), "")) or "")
    has_adapter = bool(str(game.get("emulator_adapter_id", "") or game.get("emulator_id", "")).strip())
    if (
        len(args) == 1
        and not game_command
        and not profile_command
        and not has_adapter
        and not os.access(str(args[0]), os.X_OK)
    ):
        raise ValueError(
            f"{game.get('name', 'This game')} has no launch command and its file is not executable. "
            "Set a launch command for the platform in Emulator profiles, or per-game in Edit game."
        )
    return args, cwd


def _apply_start_plugins(game, args, cwd):
    """Run the before_launch hook and enforce its response contract."""
    data_parent = _ns("DATA", DATA).parent
    run_pl = _ns("run_plugins", run_plugins)
    original_args, original_cwd = args, cwd
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        result = run_pl(data_parent / "plugins", "before_launch", {"game": game, "args": args, "cwd": cwd})
        if not isinstance(result, dict):
            raise ValueError("A plugin returned an invalid launch response.")
        if result.get("cancel"):
            raise ValueError(str(result.get("error") or "Launch canceled by a plugin."))
        args, cwd = result.get("args"), result.get("cwd")
        # The hook may adjust arguments, but it must not swap the binary or
        # move the working directory outside the game or data directories.
        if (
            not isinstance(args, list) or not args
            or not all(isinstance(part, str) and part for part in args)
            or args[0] != original_args[0]
            or (cwd is not None and not isinstance(cwd, str))
            or (cwd is not None and not _contained_launch_cwd(cwd, game))
        ):
            LOGGER.warning(
                "Ignoring before_launch result from plugin hook: invalid args/cwd (requested args=%r, cwd=%r); using the original launch command",
                args, cwd,
            )
            args, cwd = original_args, original_cwd
    return args, cwd


def _validate_start_command(args, cwd):
    """Reject plugin-adjusted launch commands that are not usable."""
    if not isinstance(args, list) or not args or not all(isinstance(part, str) and part for part in args):
        raise ValueError("A plugin returned an invalid launch command.")
    if not isinstance(cwd, str) or not Path(cwd).is_dir():
        raise ValueError("A plugin returned an invalid working directory.")


def _make_start_mutator(stable_game_id, index, started, process, entry, missing, launch_id, effective_profile):
    """Build the state transaction that records a launched session."""
    res_game = _ns("resolve_library_game", resolve_library_game)
    read_start = _ns("_read_proc_start_time", _read_proc_start_time)
    read_cmd = _ns("_read_proc_cmdline", _read_proc_cmdline)

    def mutate(state):
        current = res_game(state, {"stable_game_id": stable_game_id}, fallback_index=index)
        if current is None:
            missing["value"] = True
            return
        current["last_played"] = started.isoformat(timespec="seconds")
        current["play_count"] = current.get("play_count", 0) + 1
        if not current.get("progress") and state.get("settings", {}).get("progress_on_first_play", "Playing"):
            current["progress"] = state.get("settings", {}).get("progress_on_first_play", "Playing")
        entry.update({
            "launch_id": launch_id,
            "game_id": index,
            "stable_game_id": stable_game_id,
            "effective_profile": effective_profile,
            "game": current.get("name", "Untitled"),
            "game_path": str(current.get("path", "")),
            "steam_app_id": str(current.get("steam_app_id") or ""),
            "heroic_app_id": str(current.get("heroic_app_id") or ""),
            "lutris_id": str(current.get("lutris_id") or ""),
            "gameyfin_id": str(current.get("gameyfin_id") or ""),
            "started": started.isoformat(timespec="seconds"),
            "pid": process.pid,
            "paused": False,
        })

        try:
            pgid = os.getpgid(process.pid)
        except OSError:
            pgid = process.pid

        session_record = {
            "game_id": stable_game_id,
            "launch_id": launch_id,
            "pid": process.pid,
            "pgid": pgid,
            "proc_start_time": read_start(process.pid),
            "command_fingerprint": read_cmd(process.pid),
            "start_time": started.isoformat(timespec="seconds"),
            "perf_profile": effective_profile,
            "status": "active"
        }
        state.setdefault("active_sessions", []).append(session_record)

    return mutate


def _annotate_gamescope_start(args, game, process):
    """Tag the spawned process for gamescope guest mode when not a Steam launch."""
    if is_gamescope_guest(force="--game-mode" in sys.argv) and not is_steam_launch(args):
        window_class = Path(str(args[0])).name if args else None
        threading.Thread(
            target=mark_process_windows,
            kwargs={
                "pid": process.pid,
                "app_id": steam_game_id_for(game),
                "window_name": game.get("name") or None,
                "window_class": window_class,
            },
            daemon=True,
        ).start()


def _publish_start_events(game, entry):
    """Emit the started session events for one launch."""
    from pkg.state.sse import _publish_session_event, session_event

    sess_ev = _ns("session_event", session_event)
    pub_sess_ev = _ns("_publish_session_event", _publish_session_event)

    sess_ev("started", entry["launch_id"], entry["game"])
    pub_sess_ev(build_event("session.started", {
        "launch_id": entry.get("launch_id", ""),
        "game_id": entry.get("stable_game_id", ""),
        "name": entry.get("game", "Untitled"),
        "platform": game.get("platform", ""),
        "started_at": entry.get("started", ""),
    }))


def start_game(index=None, stable_game_id=""):
    # Explicit 8-phase launch (Days 0-14, Task 2): each failure after phase 4 restores perf, no stale RUNNING.
    load_fn = _ns("load_state", load_state)
    res_start = _ns("_resolve_start_game", _resolve_start_game)
    start_cmd = _ns("_start_launch_command", _start_launch_command)
    eff_prof_fn = _ns("effective_profile_name", effective_profile_name)
    app_perf = _ns("apply_perf_profile", apply_perf_profile)
    app_plug = _ns("_apply_start_plugins", _apply_start_plugins)
    val_cmd = _ns("_validate_start_command", _validate_start_command)
    upd_st = _ns("update_state", update_state)
    make_mut = _ns("_make_start_mutator", _make_start_mutator)
    ann_gs = _ns("_annotate_gamescope_start", _annotate_gamescope_start)
    pub_ev = _ns("_publish_start_events", _publish_start_events)
    fin_sess = _ns("finish_session", finish_session)

    # Phase 1: Resolve the game by stable ID.
    state = load_fn()
    game, index = res_start(state, index, stable_game_id)
    stable_game_id = str(game.get("game_id") or stable_game_id)
    # Phase 2: Resolve and validate the launch command and working directory.
    profiles = dict(state["profiles"])
    selected_profile = str(game.get("launch_profile", "")).strip()
    if selected_profile and selected_profile in profiles:
        profiles = {game.get("platform", ""): profiles[selected_profile]}
    args, cwd = start_cmd(game, profiles)
    # Phase 3: Create a launch record containing stable game ID, canonical game path, profile name, and expected process identity.
    launch_id = secrets.token_urlsafe(8)
    # Phase 4: Apply the performance profile and retain whether it actually changed system state.
    effective_profile = eff_prof_fn(game, state["profiles"])
    lease = app_perf(effective_profile, state)
    process = None

    try:
        # Phase 5: Start the process (plugins + validation must succeed first).
        args, cwd = app_plug(game, args, cwd)
        # Apply gamescope preset if set and not already a gamescope guest (1.7.2).
        args = _apply_gamescope_preset_from_state(state, args)
        val_cmd(args, cwd)
        # Apply MangoHud env if enabled in settings (1.7.2).
        launch_env = _apply_mangohud_from_state(state)
        process = subprocess.Popen(args, cwd=cwd, start_new_session=True, env=launch_env)
        started = datetime.now()
        entry = {}
        missing = {"value": False}
        # Phase 6: Persist the active-session record.
        upd_st(make_mut(stable_game_id, index, started, process, entry, missing, launch_id, effective_profile))
        if missing["value"]:
            raise IndexError("Game was removed while it was launching")
        # Phase 7: Register the in-memory session and start the watcher.
        with PROCESS_LOCK:
            RUNNING[launch_id] = entry
            PROCESSES[launch_id] = process
        ann_gs(args, game, process)
        pub_ev(game, entry)
        threading.Thread(
            target=fin_sess,
            args=(launch_id, index, started, process, lease),
            daemon=True,
        ).start()
        return dict(entry)
    except Exception:
        # Phase 8: Roll back post-spawn state and restore the performance lease.
        if process is not None:
            roll_back = _ns("_rollback_failed_launch", _rollback_failed_launch)
            roll_back(launch_id, process)
        lease.restore()
        raise


def control_game_session(launch_id, action):
    from pkg.state.sse import session_event
    sess_ev = _ns("session_event", session_event)

    with PROCESS_LOCK:
        process = PROCESSES.get(launch_id)
        running = RUNNING.get(launch_id)
        if not process or not running or process.poll() is not None:
            raise ValueError("That game is no longer running.")
        process_group = process.pgid if isinstance(process, _ReattachedProcess) else process.pid
        try:
            if action == "pause":
                os.killpg(process_group, signal.SIGSTOP)
                running["paused"] = True
            elif action == "resume":
                os.killpg(process_group, signal.SIGCONT)
                running["paused"] = False
            elif action in {"stop", "restart", "kill"}:
                running["restart"] = action == "restart"
                if running.get("paused") and action != "kill":
                    os.killpg(process_group, signal.SIGCONT)
                os.killpg(process_group, signal.SIGKILL if action == "kill" else signal.SIGTERM)
            else:
                raise ValueError("Unknown session action.")
        except (ProcessLookupError, OSError) as error:
            raise ValueError("Could not signal the game process.") from error
        game = running["game"]
    if action in {"pause", "resume"}:
        sess_ev("paused" if action == "pause" else "resumed", launch_id, game)
    return {"ok": True, "action": action}


def finish_session(launch_id, game_index, started, process, lease):
    from pkg.state.cache import STATE_LOCK
    from pkg.state.sse import _publish_session_event, session_event

    data_parent = _ns("DATA", DATA).parent
    state_lock = _ns("STATE_LOCK", STATE_LOCK)
    load_fn = _ns("load_state", load_state)
    upd_st = _ns("update_state", update_state)
    res_game = _ns("resolve_library_game", resolve_library_game)
    wait_exit = _ns("wait_for_exit", wait_for_exit)
    bk_saves = _ns("backup_saves", backup_saves)
    enf_limit = _ns("enforce_backup_limit", enforce_backup_limit)
    obs_rec = _ns("auto_attach_obs_recording", auto_attach_obs_recording)
    cls_store = _ns("close_store_client", close_store_client)
    run_pl = _ns("run_plugins", run_plugins)
    sess_ev = _ns("session_event", session_event)
    pub_sess_ev = _ns("_publish_session_event", _publish_session_event)
    start_fn = _ns("start_game", start_game)

    running = {}
    running_snapshot = {}
    game_name = "Untitled"
    exit_code = 0
    seconds = 1
    session = {}
    session_committed = False
    try:
        with PROCESS_LOCK:
            running_snapshot = dict(RUNNING.get(launch_id, {}))
        identity = {
            "stable_game_id": running_snapshot.get("stable_game_id", ""),
            "game_path": running_snapshot.get("game_path", ""),
            "game_name": running_snapshot.get("game") or running_snapshot.get("game_name", ""),
            "steam_app_id": running_snapshot.get("steam_app_id", ""),
            "heroic_app_id": running_snapshot.get("heroic_app_id", ""),
            "lutris_id": running_snapshot.get("lutris_id", ""),
            "gameyfin_id": running_snapshot.get("gameyfin_id", ""),
        }
        state = load_fn()
        with state_lock:
            settings = copy.deepcopy(state.get("settings", {}))
            game = res_game(state, identity, fallback_index=game_index) or {}
            game_snapshot = copy.deepcopy(game)
            original_game_name = str(game_snapshot.get("name", "") or identity.get("game_name") or "Untitled")
        exit_code = wait_exit(process, game_snapshot, settings)
        seconds = max(1, int((datetime.now() - started).total_seconds()))
        if game_snapshot:
            if settings.get("backup_on_close") and game_snapshot.get("save_paths"):
                try:
                    bk_saves(game_snapshot, data_parent / "save-backups", label="on-close")
                    enf_limit(game_snapshot, data_parent / "save-backups", settings.get("save_backup_limit", 10))
                except (OSError, FileNotFoundError):
                    pass
            try:
                obs_rec(game_snapshot, started, settings)
            except (OSError, ValueError, FileNotFoundError):
                pass
            try:
                cls_store(game_snapshot, settings)
            except (OSError, ValueError):
                pass

        session_result = {"game_name": original_game_name, "session": {}}

        def mutate(state):
            settings = state.get("settings", {})
            game = res_game(state, identity, fallback_index=game_index)
            if game is not None:
                game["playtime_seconds"] = game.get("playtime_seconds", 0) + seconds
                apply_progress_automation(game, settings)
                for key in ("video_recording", "recording", "last_recording"):
                    if key in game_snapshot:
                        game[key] = game_snapshot[key]
                game_name_local = game.get("name", "Untitled")
            else:
                game_name_local = original_game_name
            session_local = {
                "game": game_name_local,
                "started": started.isoformat(timespec="seconds"),
                "seconds": seconds,
                "exit_code": exit_code,
            }
            if settings.get("track_session_history", True):
                state["history"].append(session_local)
                state["history"][:] = state["history"][-500:]

            # Remove from active_sessions
            state["active_sessions"] = [s for s in state.get("active_sessions", []) if s.get("launch_id") != launch_id]

            session_result.update({"game_name": game_name_local, "session": session_local})
        upd_st(mutate)
        session_committed = True
        game_name = session_result["game_name"]
        session = session_result["session"]
    finally:
        if not session_committed:
            try:
                def remove_failed_session(state):
                    state["active_sessions"] = [
                        item for item in state.get("active_sessions", [])
                        if not isinstance(item, dict) or item.get("launch_id") != launch_id
                    ]
                upd_st(remove_failed_session)
            except Exception:
                LOGGER.exception("Failed to clean up session %s after watcher failure", launch_id)
        with PROCESS_LOCK:
            running = RUNNING.pop(launch_id, {})
            PROCESSES.pop(launch_id, None)
        try:
            lease.restore()
        except Exception:  # never let performance tuning break session bookkeeping
            LOGGER.exception("restore_perf failed")
    sess_ev("stopped", launch_id, game_name, exit_code=exit_code, seconds=seconds)
    pub_sess_ev(build_event("session.stopped", {
        "launch_id": launch_id,
        "game_id": running_snapshot.get("stable_game_id", ""),
        "name": game_name,
        "seconds": seconds,
        "exit_code": exit_code,
        "started_at": session.get("started", ""),
        "stopped_at": datetime.now().isoformat(timespec="seconds"),
    }))
    if not os.environ.get("OPENBOX_SAFE_MODE"):
        run_pl(data_parent / "plugins", "after_session", session)
    try:
        sync_fn = _ns("sync_cloud", None)
        if sync_fn:
            sync_fn()
    except (OSError, ValueError):
        pass
    if running.get("restart"):
        state = load_fn()
        target = res_game(state, identity, fallback_index=game_index)
        if target is not None:
            index = state["games"].index(target)
            try:
                start_fn(index, stable_game_id=target.get("game_id", ""))
            except (OSError, ValueError, IndexError):
                pass
