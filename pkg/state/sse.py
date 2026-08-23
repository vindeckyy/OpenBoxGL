"""Server-Sent Events (SSE) broadcasting and webhook delivery infrastructure."""

from datetime import datetime
import json
import logging
import os
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


def _ns(name, default):
    mod = sys.modules.get("webapp_state")
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


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
    bcast(kind, event)
