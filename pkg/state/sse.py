"""Server-Sent Events (SSE) broadcasting and webhook delivery infrastructure."""

from datetime import datetime
import json
import logging
import os
from pathlib import Path
import queue
import sys
import threading

from automation import MAX_WEBHOOKS, build_event, utc_now
from notifications import add_notification
from openbox import DATA, load_state
from pkg.state.cache import GZIP_THRESHOLD  # noqa: F401  # re-exported via webapp_state shim
from pkg.state.registry import EVENT_SEQUENCE  # noqa: F401  # re-exported via webapp_state shim

LOGGER = logging.getLogger("openbox")
METADATA_DATABASE = DATA.parent / "metadata/launchbox.db"
WEBHOOK_DISPATCHER = None
WEBHOOK_DISPATCHER_LOCK = threading.Lock()
EVENT_SUBSCRIBERS = set()
EVENT_SUBSCRIBERS_LOCK = threading.Lock()
SSE_MAX_SUBSCRIBERS = 16
SSE_QUEUE_SIZE = 128
SSE_MAX_EVENT_BYTES = 64 * 1024
SSE_WRITE_TIMEOUT = 5
# Session recap (S2): the newest finished-session recap payload plus the
# per-session RetroAchievements baselines captured at session start. Both are
# in-memory; the recap route falls back to deriving one from history so a
# dropped SSE event or a restart still leaves the card retrievable.
LAST_SESSION_RECAP = {}
SESSION_RA_BASELINES = {}
SESSION_RA_BASELINE_LIMIT = 64
SESSION_RECAP_LOCK = threading.Lock()
RESUME_STATE_META_NAMES = ("meta.json", "state.json", "manifest.json")
RESUME_STATE_SCAN_LIMIT = 200


def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    from pkg.state._deps import get
    return get(name, default)


def webhook_configs(state=None):
    """Return the persisted webhook configurations list (redacted when public)."""
    load_fn = _ns("load_state", load_state)
    state = state or load_fn()
    configs = state.get("settings", {}).get("webhooks", [])
    if not isinstance(configs, list):
        return []
    return [config for config in configs[:MAX_WEBHOOKS] if isinstance(config, dict)]


def emit_notification(*, kind="system", level="info", title="OpenBox", body="", source="", correlation_id="", dedupe_key=""):
    from pkg.state.cache import transact_state
    transact = _ns("transact_state", transact_state)

    def mutate(state):
        return add_notification(state, kind=kind, level=level, title=title, body=body, source=source, correlation_id=correlation_id, dedupe_key=dedupe_key)
    try:
        transact(mutate)
    except Exception:
        LOGGER.exception("Could not persist notification")


def public_webhook_configs(state=None):
    """Return webhook configs with secrets replaced by a secret_set flag."""
    configs = []
    for config in webhook_configs(state):
        public = {
            key: value
            for key, value in config.items()
            if key != "secret"
        }
        public["secret_set"] = bool(config.get("secret"))
        configs.append(public)
    return configs


def _webhook_payload(envelope, configs):
    """Persist and enqueue one event envelope for matching webhook configs.

    Never raises: webhook delivery is best-effort and must not change the
    outcome of the originating operation. Returns the event id string.
    """
    event_id = str(envelope.get("id") or "")
    try:
        matched = [config for config in configs if config.get("enabled") and event_matches(config, envelope)]
        dispatcher = get_webhook_dispatcher()
        if dispatcher is None:
            return event_id
        if not dispatcher.enqueue(matched, envelope):
            LOGGER.warning("Webhook queue is full; event %s was dropped", event_id)
            _emit_webhook_failure(event_id, "Webhook delivery queue is full; the event was dropped.")
    except Exception:
        LOGGER.exception("Webhook delivery failed for event %s", event_id)
    return event_id


def event_matches(config, envelope):
    events = config.get("events") or []
    return isinstance(events, list) and str(envelope.get("type", "")) in events


def _emit_webhook_failure(event_id, error):
    """Surface a delivery failure through the Notification Center when present.

    Uses getattr so this module works even before the notification module
    lands in the same release; failures are logged when no emitter exists.
    """
    emitter = _ns("emit_notification", emit_notification)
    if emitter is None:
        LOGGER.warning("Webhook event %s failed delivery: %s", event_id, error)
        return
    try:
        emitter(
            level="error",
            source="webhook",
            title="Webhook delivery failed",
            body=error,
            correlation_id=event_id,
            dedupe_key=f"webhook:{event_id}",
        )
    except Exception:
        LOGGER.exception("Failed to record webhook delivery failure notification")


def _commit_webhook_result(webhook_id, event_id, status, error, sent_at, terminal):
    """Persist the last delivery status for one webhook config.

    Runs outside every dispatcher, process, and state lock; the callback
    contract requires the worker to release all locks before invoking it.
    """
    from pkg.state.cache import transact_state
    transact = _ns("transact_state", transact_state)

    try:
        def mutate(state):
            settings = state.setdefault("settings", {})
            for config in settings.get("webhooks", []):
                if not isinstance(config, dict):
                    continue
                if str(config.get("id") or "") != webhook_id:
                    continue
                config["last_status"] = status
                config["last_error"] = error
                if sent_at:
                    config["last_sent_at"] = sent_at
                if terminal:
                    config["last_delivery_at"] = sent_at or utc_now()
                return True
            return False

        _, updated = transact(mutate)
        if not updated:
            return
        if terminal and (status is None or status >= 300 or status == 0):
            _emit_webhook_failure(
                event_id,
                error or f"Webhook delivery failed with HTTP {status}." if status else (error or "Webhook delivery failed."),
            )
    except Exception:
        LOGGER.exception("Failed to commit webhook delivery status for %s", webhook_id)


def get_webhook_dispatcher():
    """Return the lazily-created dispatcher singleton, or None in safe mode.

    The dispatcher factory is replaceable under ``WEBHOOK_DISPATCHER_FACTORY``
    so handler/session tests can inject a fake without running ``main()``.
    """
    global WEBHOOK_DISPATCHER
    if os.environ.get("OPENBOX_SAFE_MODE"):
        return None
    factory = _ns("WEBHOOK_DISPATCHER_FACTORY", globals().get("WEBHOOK_DISPATCHER_FACTORY", _default_webhook_dispatcher_factory))
    with WEBHOOK_DISPATCHER_LOCK:
        if WEBHOOK_DISPATCHER is None:
            WEBHOOK_DISPATCHER = factory()
            WEBHOOK_DISPATCHER.start()
        return WEBHOOK_DISPATCHER


def _default_webhook_dispatcher_factory():
    from automation import WebhookDispatcher
    return WebhookDispatcher(on_result=_commit_webhook_result)


def publish_event(event, data):
    """Build and enqueue one webhook event for matching configs. Never raises.

    Returns the event id string, or "" when the event could not be built.
    """
    load_fn = _ns("load_state", load_state)
    try:
        envelope = build_event(event, data)
    except (ValueError, TypeError) as error:
        LOGGER.warning("Skipped webhook event %s: %s", event, error)
        return ""
    try:
        configs = webhook_configs(load_fn())
        _webhook_payload(envelope, configs)
    except Exception:
        LOGGER.exception("Webhook publish failed for event %s", event)
    return str(envelope.get("id") or "")


def shutdown_webhooks(wait_seconds=2.0):
    """Stop and join the lazy webhook dispatcher singleton if it exists."""
    global WEBHOOK_DISPATCHER
    with WEBHOOK_DISPATCHER_LOCK:
        dispatcher = WEBHOOK_DISPATCHER
        WEBHOOK_DISPATCHER = None
    if dispatcher is not None:
        try:
            dispatcher.shutdown(wait_seconds=wait_seconds)
        except Exception:
            LOGGER.exception("Webhook dispatcher shutdown failed")


def _publish_session_event(envelope):
    pub_event = _ns("publish_event", publish_event)
    try:
        pub_event(envelope["type"], envelope["data"])
    except Exception:
        LOGGER.exception("Failed to publish session webhook event")


def register_event_subscriber(subscriber):
    with EVENT_SUBSCRIBERS_LOCK:
        if len(EVENT_SUBSCRIBERS) >= SSE_MAX_SUBSCRIBERS:
            return False
        EVENT_SUBSCRIBERS.add(subscriber)
        return True


def unregister_event_subscriber(subscriber):
    with EVENT_SUBSCRIBERS_LOCK:
        EVENT_SUBSCRIBERS.discard(subscriber)


def _close_sse_subscriber(subscriber):
    try:
        while True:
            subscriber.get_nowait()
    except queue.Empty:
        pass
    except (OSError, ValueError) as e:
        LOGGER.warning("sse subscriber drain: %s", e)
        return
    try:
        subscriber.put_nowait(None)
    except (OSError, ValueError) as e:
        LOGGER.warning("sse subscriber sentinel: %s", e)


def emit_operation_event(event_name, payload):
    """Emit durable-operation SSE events (job.queued, job.progress, job.finished, ...)."""
    broadcast_event(event_name, payload)


def broadcast_event(kind, payload):
    """Push one bounded event to every connected SSE subscriber. Never blocks."""
    try:
        data = json.dumps(payload, ensure_ascii=False)
        encoded = data.encode("utf-8")
    except (TypeError, ValueError):
        LOGGER.warning("Skipped non-serializable SSE event %s", kind)
        return
    if len(encoded) > SSE_MAX_EVENT_BYTES:
        data = json.dumps({"truncated": True, "bytes": len(encoded)}, separators=(",", ":"))
    event_kind = str(kind).replace("\r", "").replace("\n", "")[:64]
    with EVENT_SUBSCRIBERS_LOCK:
        subscribers = list(EVENT_SUBSCRIBERS)
    for subscriber in subscribers:
        try:
            subscriber.put_nowait((event_kind, data))
        except queue.Full:
            unregister_event_subscriber(subscriber)
            _close_sse_subscriber(subscriber)
        except Exception:
            LOGGER.exception("SSE subscriber queue failed")


def session_event(kind, launch_id, game_name, exit_code=None, seconds=None):
    from pkg.state.registry import PROCESS_LOCK, SESSION_EVENTS
    proc_lock = _ns("PROCESS_LOCK", PROCESS_LOCK)
    sess_events = _ns("SESSION_EVENTS", SESSION_EVENTS)
    bcast = _ns("broadcast_event", broadcast_event)

    import pkg.state.registry as _reg
    recap_baseline_id = ""
    with proc_lock:
        _reg.EVENT_SEQUENCE += 1
        event = {
            "id": _reg.EVENT_SEQUENCE,
            "kind": kind,
            "launch_id": launch_id,
            "game": game_name,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        if exit_code is not None:
            event["exit_code"] = exit_code
        if seconds is not None:
            event["seconds"] = seconds
        sess_events.append(event)
        sess_events[:] = sess_events[-100:]
        if kind == "started":
            # Recap RA delta needs a session-start snapshot; the running entry
            # already carries the stable game id, so no state read is needed.
            running = _reg.RUNNING.get(launch_id) or {}
            recap_baseline_id = str(running.get("stable_game_id") or "")
    bcast(kind, event)
    if recap_baseline_id:
        capture_session_ra_baseline(launch_id, recap_baseline_id)


def ra_cache_snapshot(data_parent, game_id):
    """Return the per-game RetroAchievements cache record, or {} when absent.

    Cache files live at ``cache/retroachievements/<openbox-game_id>.json`` and
    are keyed by the OpenBox stable game id (the same convention
    ``parity_insights._load_ra_cache`` reads).
    """
    game_id = str(game_id or "").strip()
    if not game_id:
        return {}
    path = Path(data_parent) / "cache" / "retroachievements" / f"{game_id}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def capture_session_ra_baseline(launch_id, game_id):
    """Snapshot the game's RA cache at session start so the recap can delta it.

    Best-effort: a missing cache file records an empty baseline, which turns
    a mid-session cache write into the full earned count as the delta.
    """
    launch_id = str(launch_id or "")
    if not launch_id:
        return
    data_parent = _ns("DATA", DATA).parent
    snapshot = ra_cache_snapshot(data_parent, game_id)
    with SESSION_RECAP_LOCK:
        SESSION_RA_BASELINES[launch_id] = snapshot
        while len(SESSION_RA_BASELINES) > SESSION_RA_BASELINE_LIMIT:
            SESSION_RA_BASELINES.pop(next(iter(SESSION_RA_BASELINES)))


def pop_session_ra_baseline(launch_id):
    """Return and drop the session-start RA snapshot for *launch_id*."""
    with SESSION_RECAP_LOCK:
        return SESSION_RA_BASELINES.pop(str(launch_id or ""), None)


def _ra_int(snapshot, key):
    try:
        return int((snapshot or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _ra_block(game, baseline, current):
    """Assemble the ``ra`` recap block: end-state summary plus session delta."""
    tracked = bool(str((game or {}).get("ra_game_id") or "").strip())
    available = bool(current)
    earned = _ra_int(current, "earned")
    earned_hardcore = _ra_int(current, "earned_hardcore")
    try:
        progress_pct = float(current.get("progress_pct") or 0.0) if available else 0.0
    except (TypeError, ValueError):
        progress_pct = 0.0
    if baseline is None:
        earned_delta = 0
        earned_hardcore_delta = 0
    else:
        earned_delta = max(0, earned - _ra_int(baseline, "earned"))
        earned_hardcore_delta = max(0, earned_hardcore - _ra_int(baseline, "earned_hardcore"))
    return {
        "tracked": tracked,
        "available": available,
        "baseline": baseline is not None,
        "earned": earned,
        "earned_hardcore": earned_hardcore,
        "total": _ra_int(current, "total"),
        "progress_pct": progress_pct,
        "mastered": bool(current.get("mastered")) if available else False,
        "earned_delta": earned_delta,
        "earned_hardcore_delta": earned_hardcore_delta,
    }


def _resume_state_available(data_parent, game_id, *, game=None, settings=None, profiles=None):
    """Return validated Quick Resume readiness for a concrete library game."""
    needle = str(game_id or "").strip()
    if not needle:
        return False
    try:
        if not isinstance(game, dict):
            state = _ns("load_state", load_state)()
            game = next(
                (
                    item for item in (state.get("games") or [])
                    if isinstance(item, dict) and str(item.get("game_id") or "") == needle
                ),
                None,
            )
            if settings is None:
                settings = state.get("settings", {})
            if profiles is None:
                profiles = state.get("profiles", {})
        if not isinstance(game, dict):
            return False
        from pkg.parity.parity_resume import resume_status
        status = resume_status(
            game,
            settings or {},
            profiles=profiles or {},
            data_parent=data_parent,
        )
        return bool(
            status.get("enabled") and status.get("capable")
            and status.get("available") and not status.get("stale")
        )
    except (OSError, ValueError, TypeError, ImportError):
        return False


def _capture_flags():
    """Report which capture surfaces ship in this build (graceful absence).

    ``moment`` flips on when T1-ui lands ``static/moments.js``; ``clip`` stays
    False until a ``static/clips.js`` surface exists — the frontend also probes
    the ``window.OpenBoxClip`` runtime hook, so T3-obs needs no backend edit.
    """
    static_dir = Path(__file__).resolve().parents[2] / "static"
    try:
        return {
            "moment": (static_dir / "moments.js").is_file(),
            "clip": (static_dir / "clips.js").is_file(),
        }
    except OSError:
        return {"moment": False, "clip": False}


def store_session_recap(payload):
    """Remember the newest recap payload for the v2 recap route."""
    if not isinstance(payload, dict):
        return
    with SESSION_RECAP_LOCK:
        LAST_SESSION_RECAP.clear()
        LAST_SESSION_RECAP.update(payload)


def last_session_recap():
    """Return the stored recap payload, or None when no session produced one."""
    with SESSION_RECAP_LOCK:
        return dict(LAST_SESSION_RECAP) if LAST_SESSION_RECAP else None


def build_session_recap(*, launch_id, session, game, game_before, seconds, exit_code, stopped_at, data_parent, baseline, settings=None, profiles=None):
    """Assemble the recap payload. Pure data shaping — never performs I/O
    beyond the two bounded filesystem probes (RA cache, resume_states)."""
    game = game if isinstance(game, dict) else None
    session = session if isinstance(session, dict) else {}
    game_missing = game is None
    game_id = str((game or {}).get("game_id") or session.get("game_id") or "")
    shots_before = {str(p) for p in (game_before or {}).get("screenshots") or [] if p}
    shots_after = {str(p) for p in (game or {}).get("screenshots") or [] if p}
    exit_code = int(exit_code or 0)
    return {
        "launch_id": str(launch_id or ""),
        "game_id": game_id,
        "name": str(session.get("game") or (game or {}).get("name") or "Untitled"),
        "platform": str((game or {}).get("platform") or ""),
        "started_at": str(session.get("started") or ""),
        "stopped_at": str(stopped_at or ""),
        "seconds": max(0, int(seconds or 0)),
        "exit_code": exit_code,
        "partial": bool(game_missing or exit_code != 0),
        "game_missing": game_missing,
        "progress": str((game or {}).get("progress") or ""),
        "screenshots_taken": len(shots_after - shots_before),
        "ra": _ra_block(game, baseline, ra_cache_snapshot(data_parent, game_id)),
        "resume_state_available": _resume_state_available(
            data_parent,
            game_id,
            game=game,
            settings=settings,
            profiles=profiles,
        ),
        "capture": _capture_flags(),
    }


def publish_session_recap(*, launch_id, session, game_name, exit_code, seconds, state, game_before, identity, data_parent, fallback_index=None):
    """Build, store, and broadcast ``session.recap``. Never raises.

    Called from ``finish_session``'s post-commit publish block once the
    session row is committed; any failure is logged and swallowed so session
    bookkeeping is never broken by the card.
    """
    try:
        from pkg.state.launch import resolve_library_game
        resolver = _ns("resolve_library_game", resolve_library_game)
        game = resolver(state, identity, fallback_index=fallback_index) if isinstance(state, dict) else None
        payload = build_session_recap(
            launch_id=launch_id,
            session=session,
            game=game,
            game_before=game_before,
            seconds=seconds,
            exit_code=exit_code,
            stopped_at=datetime.now().isoformat(timespec="seconds"),
            data_parent=data_parent,
            baseline=pop_session_ra_baseline(launch_id),
            settings=(state or {}).get("settings", {}) if isinstance(state, dict) else {},
            profiles=(state or {}).get("profiles", {}) if isinstance(state, dict) else {},
        )
        if game is None and game_name:
            payload["name"] = str(game_name)
        store_session_recap(payload)
        bcast = _ns("broadcast_event", broadcast_event)
        bcast("session.recap", payload)
    except Exception:
        LOGGER.exception("Failed to publish session recap")


def session_recap_from_history(state=None):
    """Derive a partial recap from the newest history entry.

    Covers the SSE-drop and post-restart cases: no stored payload, no RA
    baseline, no screenshot delta — the card still renders from the recorded
    history row. Returns None when history is empty.
    """
    load_fn = _ns("load_state", load_state)
    state = state if isinstance(state, dict) else load_fn()
    history = state.get("history") or []
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return None
    entry = history[-1]
    data_parent = _ns("DATA", DATA).parent
    from pkg.state.launch import resolve_library_game
    resolver = _ns("resolve_library_game", resolve_library_game)
    game = resolver(state, {
        "stable_game_id": entry.get("game_id"),
        "game_name": entry.get("game"),
    })
    payload = build_session_recap(
        launch_id="",
        session=entry,
        game=game,
        game_before=None,
        seconds=entry.get("seconds"),
        exit_code=entry.get("exit_code"),
        stopped_at="",
        data_parent=data_parent,
        baseline=None,
    )
    payload["partial"] = True
    return payload
