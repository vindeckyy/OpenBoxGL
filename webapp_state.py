"""Canonical state owner is pkg/state; webapp_state is compat shim.

Process-global state and shared service functions for the OpenBox web server.
Owns the mutable module state (TOKEN, locks, running sessions, caches) and
re-exports service helpers so ``web_app.py`` and ``handlers/*.py`` can import
them from here.  Domain logic lives in ``pkg/state/imports.py`` and
``pkg/state/commands.py``; this module is a thin re-export shim.
"""

import logging
from pathlib import Path
import secrets
import subprocess
import threading

from backend_io import download_file
from job_manager import JobManager
from openbox import DATA, STATE_STORE, build_launch, load_state, load_state_readonly, update_state, update_state_with_result
from parity_emulator_defs import scan_folder as scan_emulator_folder
from parity_perf import apply_perf_profile
from parity_premium import category_for_platform
from parity_tracking import wait_for_exit
from plugins import run_plugins

from pkg.state.cache import (
    CACHE_EPOCH,
    FILE_PROBE_CACHE,
    FILE_PROBE_LOCK,
    FILE_PROBE_MAX,
    FILE_PROBE_TTL,
    MEDIA_EPOCH,
    MEDIA_EPOCH_LOCK,
    PLUGIN_EPOCH,
    PLUGIN_LIBRARY_CACHE,
    PLUGIN_LIBRARY_LOCK,
    PLUGIN_LIBRARY_TTL,
    PUBLIC_SETTINGS_CACHE,
    PUBLIC_SETTINGS_LOCK,
    PUBLIC_STATE_CACHE,
    PUBLIC_STATE_LOCK,
    STATE_LOCK,
    STATE_VIEW_CACHE,
    STATE_VIEW_LOCK,
    _GAME_PROJECTION_CACHE,
    _GAME_PROJECTION_LOCK,
    _GAME_PROJECTION_MAX,
    _KNOWN_MEDIA_MAX,
    _KNOWN_MEDIA_SET_CACHE,
    _KNOWN_MEDIA_SET_LOCK,
    _PLATFORM_CATEGORY_CACHE,
    _PLATFORM_CATEGORY_LOCK,
    _PLATFORM_CATEGORY_MAX,
    _PLUGIN_REFRESH_IN_PROGRESS,
    _SANITIZE_MEDIA_PATH_CACHE,
    _SANITIZE_MEDIA_PATH_LOCK,
    _SANITIZE_MEDIA_PATH_MAX,
    _build_known_media_set,
    _build_public_state,
    _fast_realpath,
    _media_dir_mtime,
    _media_set_contains,
    _project_game,
    _public_settings_uncached,
    _public_state_cached,
    _public_state_signature,
    bump_media_epoch,
    clear_file_probe_cache,
    load_state_view,
    public_settings,
    public_state,
    public_state_bytes,
    public_state_etag,
    transact_state,
)
from pkg.state.commands import clean_commands, run_configured_commands
from pkg.state.imports import (
    WATCH_STOP,
    auto_import_worker,
    consolidate_existing_games,
    game_identity,
    import_folder_path,
    merge_imported_games,
    sync_cloud,
)
from pkg.state.registry import (
    EVENT_SEQUENCE,
    PROCESS_LOCK,
    PROCESSES,
    RUNNING,
    SESSION_EVENTS,
)
from pkg.state.launch import (
    _ReattachedLease,
    _ReattachedProcess,
    _annotate_gamescope_start,
    _apply_start_plugins,
    _contained_launch_cwd,
    _make_start_mutator,
    _match_fallback_index,
    _match_path_and_name,
    _match_storefront_ids,
    _publish_start_events,
    _read_proc_cmdline,
    _read_proc_start_time,
    _resolve_start_game,
    _stable_id_match,
    _start_launch_command,
    _validate_start_command,
    _verify_process_identity,
    control_game_session,
    finish_session,
    game_from_payload,
    game_from_query,
    reattach_session,
    reconcile_sessions_on_startup,
    resolve_library_game,
    start_game,
)
from pkg.state.media_probe import (
    FIELDS,
    MEDIA_PATH_FIELDS,
    MEDIA_ROOTS_ENV,
    MEDIA_TYPES_ALL,
    _media_roots,
    _reject_media_symlink_components,
    approved_backup_file,
    approved_media_path,
    download_image,
    game_media_paths,
    media_probe_path,
    probe_path,
    safe_document_file,
    sanitize_document_records,
    sanitize_media_path,
    update_steam_metadata,
)
from pkg.state.sse import (
    EVENT_SUBSCRIBERS,
    EVENT_SUBSCRIBERS_LOCK,
    GZIP_THRESHOLD,
    METADATA_DATABASE,
    SSE_MAX_EVENT_BYTES,
    SSE_MAX_SUBSCRIBERS,
    SSE_QUEUE_SIZE,
    SSE_WRITE_TIMEOUT,
    WEBHOOK_DISPATCHER,
    WEBHOOK_DISPATCHER_LOCK,
    _close_sse_subscriber,
    _commit_webhook_result,
    _default_webhook_dispatcher_factory,
    _emit_webhook_failure,
    _publish_session_event,
    _webhook_payload,
    broadcast_event,
    emit_notification,
    event_matches,
    get_webhook_dispatcher,
    public_webhook_configs,
    publish_event,
    register_event_subscriber,
    session_event,
    shutdown_webhooks,
    unregister_event_subscriber,
    webhook_configs,
)

ROOT = Path(__file__).parent
TOKEN = secrets.token_urlsafe(24)
LOGGER = logging.getLogger("openbox")

INSTALLS = {}
METADATA_JOB = {}
MEDIA_JOB = {}
JOB_MANAGER = JobManager()
try:
    from pkg.state.sse import broadcast_event

    JOB_MANAGER.set_observer(lambda job: broadcast_event("job.finished", job))
except Exception:
    pass
