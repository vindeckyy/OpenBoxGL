"""Signed, bounded, asynchronous webhook delivery for OpenBox automation.

Independent open-source software not affiliated with LaunchBox or Unbroken
Software, LLC.

The dispatcher is deliberately pure: it never imports ``web_app`` or touches
the state store. ``on_result(webhook_id, event_id, status, error, sent_at,
terminal)`` is the only bridge back to the application layer, so delivery
failures can update persisted config status and surface through the
Notification Center without a module import cycle.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from backend_io import read_limited

LOGGER = logging.getLogger("openbox.webhooks")

MAX_URL_LENGTH = 2048
MAX_ENVELOPE_BYTES = 64 * 1024
DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_PENDING = 128
DEFAULT_ATTEMPTS = 3
MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT = 5
MAX_TIMEOUT = 15
MAX_WEBHOOKS = 32
_RESPONSE_DRAIN_BYTES = 4 * 1024
_ATTEMPT_DELAYS = (1.0, 2.0, 4.0, 8.0)
_MAX_RETRY_AFTER = 30.0

# Event types a webhook configuration may subscribe to. The list is the
# catalog exposed by the API; data payloads are allowlisted per event.
EVENT_TYPES = (
    "session.started",
    "session.stopped",
    "queue.advanced",
    "library.imported",
    "library.changed",
    "metadata.synced",
    "backup.created",
    "update.installed",
    "plugin.changed",
)

# Data allowlists: only these keys are copied from the caller-provided data.
EVENT_DATA_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "session.started": ("launch_id", "game_id", "name", "platform", "started_at"),
    "session.stopped": (
        "launch_id", "game_id", "name", "seconds", "exit_code",
        "started_at", "stopped_at",
    ),
    "queue.advanced": ("from_game_id", "from_entry_id", "to_game_id", "to_entry_id"),
    "library.imported": ("source", "found", "added"),
    "library.changed": ("action", "game_ids", "count"),
    "metadata.synced": ("status",),
    "backup.created": ("archive_name", "items"),
    "update.installed": ("version",),
    "plugin.changed": ("action", "plugin"),
}

SOURCE_NAME = "openbox"
ENVELOPE_VERSION = 1
SECRET_MIN_LENGTH = 8
SIGNATURE_HEADER = "X-OpenBox-Signature"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex}"


def build_event(event: str, data: dict, *, event_id=None, timestamp=None) -> dict:
    """Build one sanitized webhook envelope with an allowlisted data payload."""
    if event not in EVENT_TYPES:
        raise ValueError(f"Unknown webhook event type: {event}")
    if not isinstance(data, dict):
        raise ValueError("Webhook event data must be an object.")
    allowed = EVENT_DATA_ALLOWLISTS[event]
    clean = {}
    for key in allowed:
        if key in data:
            clean[key] = data[key]
    envelope = {
        "id": event_id or new_event_id(),
        "type": event,
        "version": ENVELOPE_VERSION,
        "created_at": timestamp or utc_now(),
        "source": SOURCE_NAME,
        "data": clean,
    }
    if len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise ValueError("Webhook envelope exceeds the 64 KiB size limit.")
    return envelope


def serialize_envelope(envelope: dict) -> bytes:
    """Serialize the envelope exactly once as compact UTF-8 JSON."""
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_event(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the HMAC-SHA256 signature over ``timestamp + "." + body``.

    The digest covers the exact transmitted bytes, so the receiver can verify
    by concatenating the ``X-OpenBox-Timestamp`` header value, a literal dot,
    and the raw request body.
    """
    mac = hmac.new(str(secret).encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(str(timestamp).encode("utf-8"))
    mac.update(b".")
    mac.update(bytes(body))
    return mac.hexdigest()


def _retryable_status(status: int) -> bool:
    return status in (408, 425, 429) or status >= 500


def _attempt_delay(attempt_index: int, retry_after_value: str | None = None) -> float:
    """Exponential delays 1, 2, 4, 8 seconds with Retry-After clamped to 30s."""
    delay = _ATTEMPT_DELAYS[attempt_index] if attempt_index < len(_ATTEMPT_DELAYS) else 8.0
    if retry_after_value:
        try:
            retry_after = float(retry_after_value)
        except (TypeError, ValueError):
            retry_after = 0.0
        if retry_after > 0:
            delay = min(delay, max(0.0, min(retry_after, _MAX_RETRY_AFTER)))
    return max(0.0, delay)


def _drain_response(response) -> None:
    """Consume and discard a bounded response body, never unboundedly."""
    try:
        read_limited(response, _RESPONSE_DRAIN_BYTES)
    except Exception:  # the response body is never surfaced anywhere
        try:
            response.close()
        except Exception:
            pass


def _port_in_use(host: str, port: int) -> bool:
    """True when a TCP listener is accepting on host:port (loopback only)."""
    if not port or not 1 <= port <= 65535:
        return False
    try:
        probe = socket.create_connection((host, port), timeout=0.4)
    except OSError:
        return False
    probe.close()
    return True


class WebhookDispatcher:
    """Deliver signed envelopes to configured webhooks on background workers.

    The dispatcher never blocks the originating operation: a bounded queue
    feeds four daemon workers, and delivery retries happen off-thread.
    """

    def __init__(
        self,
        *,
        on_result: Callable[[str, str, int | None, str, str, bool], None],
        opener=None,
        resolver=None,
        clock=None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_pending: int = DEFAULT_MAX_PENDING,
    ):
        self._on_result = on_result
        self._opener = opener
        self._resolver = resolver
        self._clock = clock or time.monotonic
        self._max_pending = max(1, int(max_pending))
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(max_pending)))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stopping = False
        self._threads: list[threading.Thread] = []
        for index in range(max(1, min(int(max_workers), 16))):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"openbox-webhook-{index}",
                daemon=True,
            )
            self._threads.append(thread)

    def start(self):
        with self._lock:
            for thread in self._threads:
                if not thread.is_alive():
                    thread.start()

    def enqueue(self, configs, envelope) -> bool:
        """Enqueue one envelope for every matching enabled config.

        Returns True when every matched config was queued; overflow or a
        stopped dispatcher returns False so the caller can log/surface it.
        """
        event = str(envelope.get("type", ""))
        body = serialize_envelope(envelope)
        event_id = str(envelope.get("id", ""))
        timestamp = str(envelope.get("created_at", ""))
        matched = []
        for config in configs or []:
            if not isinstance(config, dict):
                continue
            if not config.get("enabled"):
                continue
            events = config.get("events") or []
            if isinstance(events, list) and event not in events:
                continue
            webhook_id = str(config.get("id") or "")
            if not webhook_id or not str(config.get("url") or "").strip():
                continue
            matched.append(config)
        if not matched:
            return True
        with self._condition:
            if self._stopping:
                return False
            for config in matched:
                item = {
                    "webhook_id": str(config.get("id") or ""),
                    "url": str(config.get("url") or ""),
                    "secret": str(config.get("secret") or ""),
                    "events": list(config.get("events") or []),
                    "timeout": int(config.get("timeout") or DEFAULT_TIMEOUT),
                    "attempts": int(config.get("attempts") or DEFAULT_ATTEMPTS),
                    "event_id": event_id,
                    "event": event,
                    "body": body,
                    "timestamp": timestamp,
                }
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    self._condition.notify_all()
                    return False
            self._condition.notify_all()
            return True

    def _deliver(self, item: dict) -> None:
        webhook_id = str(item["webhook_id"])
        event_id = str(item["event_id"])
        url = str(item["url"])
        timeout = max(1, min(int(item.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        attempts = max(1, min(int(item.get("attempts") or DEFAULT_ATTEMPTS), MAX_ATTEMPTS))
        sent_at = ""
        final_error = ""
        final_status: int | None = None
        terminal = True

        for attempt in range(attempts):
            if self._stopping:
                terminal = False
                final_error = "stopped"
                break
            try:
                status, retry_after, redirected = self._send_once(item, url, timeout)
            except ValueError as error:
                # Invalid URLs fail validation before the first delivery; a
                # redirected request is a terminal failure rather than an
                # unvalidated second request.
                final_status = None
                final_error = str(error)
                break
            except (OSError, TimeoutError) as error:
                final_error = str(error)
                continue
            sent_at = utc_now()
            if redirected or not _retryable_status(status):
                final_status = status
                final_error = "" if status == 200 else f"HTTP {status}"
                break
            final_status = status
            final_error = f"HTTP {status}"
            if attempt + 1 < attempts:
                delay = _attempt_delay(attempt, retry_after)
                if self._stopping:
                    terminal = False
                    final_error = "stopped"
                    break
                self._clock()
                time.sleep(delay)
                self._clock()
        try:
            self._on_result(webhook_id, event_id, final_status, final_error, sent_at, terminal)
        except Exception:  # the callback must never break a delivery worker
            LOGGER.exception("Webhook on_result callback failed for %s", webhook_id)

    def _send_once(self, item: dict, url: str, timeout: int):
        request = Request(
            url,
            data=item["body"],
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OpenBox/1",
                "X-OpenBox-Event-Id": item["event_id"],
                "X-OpenBox-Event": item["event"],
                "X-OpenBox-Timestamp": item["timestamp"],
            },
        )
        if item.get("secret"):
            request.add_header(
                SIGNATURE_HEADER,
                f"sha256={sign_event(item['secret'], item['timestamp'], item['body'])}",
            )
        with self._opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            headers = getattr(response, "headers", None)
            retry_after = ""
            if headers is not None:
                retry_after = str(headers.get("Retry-After", "")).strip()
            _drain_response(response)
            return status, retry_after, 300 <= status < 400

    def _worker_loop(self):
        while True:
            with self._condition:
                while True:
                    if self._stopping and self._queue.empty():
                        return
                    try:
                        item = self._queue.get_nowait()
                        break
                    except queue.Empty:
                        if self._stopping:
                            return
                        self._condition.wait(timeout=0.25)
            try:
                self._deliver(item)
            except Exception:  # worker isolation: never kill the dispatcher
                LOGGER.exception("Webhook delivery worker crashed for %s", item.get("webhook_id", "?"))
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def shutdown(self, wait_seconds: float = 2.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        deadline = self._clock() + max(0.0, float(wait_seconds))
        for thread in list(self._threads):
            remaining = max(0.0, deadline - self._clock())
            thread.join(timeout=remaining)


def _clean_url(url: str) -> str:
    return str(url or "").strip()


def _hostname_for(parsed) -> str:
    host = str(parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise ValueError("Webhook URL must include a hostname.")
    return host


def _resolve_addresses(host: str, resolver=None) -> list[str]:
    if resolver is not None:
        return [str(address) for address in resolver(host)]
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def _reject_unsafe_address(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as error:
        raise ValueError("Webhook URL could not be resolved to a valid address.") from error
    if ip.is_unspecified:
        raise ValueError("Webhook URLs may not target an unspecified address.")
    if ip.is_multicast:
        raise ValueError("Webhook URLs may not target a multicast address.")
    if ip.is_link_local:
        raise ValueError("Webhook URLs may not target a link-local address.")
    if ip.is_reserved:
        raise ValueError("Webhook URLs may not target a reserved address.")


def _loopback_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback


def _is_loopback_host(host: str) -> bool:
    lowered = host.casefold()
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def validate_webhook(config, *, openbox_port=None, resolver=None) -> None:
    """Validate one webhook configuration for saving and before delivery.

    Raises ValueError with a user-facing message for every unsafe or unusable
    URL. HTTP is only allowed when ``OPENBOX_ALLOW_HTTP_WEBHOOKS=1``; loopback
    URLs whose port equals the running OpenBox server port are rejected to
    prevent the application from webhooking itself.
    """
    if not isinstance(config, dict):
        raise ValueError("Webhook configuration must be an object.")
    events = config.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError("Select at least one webhook event.")
    unknown = [event for event in events if event not in EVENT_TYPES]
    if unknown:
        raise ValueError(f"Unknown webhook event: {unknown[0]}")
    url = _clean_url(config.get("url"))
    if not url:
        raise ValueError("Webhook URL is required.")
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("Webhook URL is too long.")
    try:
        parsed = urlparse(url)
    except ValueError as error:
        raise ValueError("Webhook URL is not a valid URL.") from error
    scheme = str(parsed.scheme).casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("Webhook URL must use http or https.")
    if scheme == "http" and os.environ.get("OPENBOX_ALLOW_HTTP_WEBHOOKS") != "1":
        raise ValueError("HTTP webhooks are disabled; enable them with OPENBOX_ALLOW_HTTP_WEBHOOKS=1.")
    host = _hostname_for(parsed)
    if parsed.username or parsed.password:
        raise ValueError("Webhook URLs may not embed credentials.")
    if parsed.fragment:
        raise ValueError("Webhook URLs may not contain a fragment.")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Webhook URL port must be between 1 and 65535.")
    if _is_loopback_host(host):
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError:
            port = 0
        if openbox_port and int(openbox_port) == port:
            raise ValueError("Webhook URL may not point at the running OpenBox server.")
        if _port_in_use(host, port):
            raise ValueError("Webhook URL may not point at the running OpenBox server.")
    else:
        addresses = _resolve_addresses(host, resolver=resolver)
        if not addresses:
            raise ValueError("Webhook URL could not be resolved.")
        for address in addresses:
            _reject_unsafe_address(address)
        if _loopback_address(addresses[0]):
            try:
                port = parsed.port or (443 if scheme == "https" else 80)
            except ValueError:
                port = 0
            if openbox_port and int(openbox_port) == port:
                raise ValueError("Webhook URL may not point at the running OpenBox server.")
    try:
        int(config.get("attempts") or DEFAULT_ATTEMPTS)
        int(config.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError) as error:
        raise ValueError("Webhook attempts and timeout must be numbers.") from error


def test_ping(config, *, openbox_port=None, resolver=None, opener=None, timeout=None) -> dict:
    """Perform one bounded synchronous test.ping delivery.

    Returns {"ok": bool, "status": int|None, "error": str} without exposing
    the response body, resolved addresses, or credentials.
    """
    validate_webhook(config, openbox_port=openbox_port, resolver=resolver)
    url = _clean_url(config.get("url"))
    envelope = {
        "id": new_event_id(),
        "type": "test.ping",
        "version": ENVELOPE_VERSION,
        "created_at": utc_now(),
        "source": SOURCE_NAME,
        "data": {"ok": True},
    }
    body = serialize_envelope(envelope)
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "OpenBox/1",
            "X-OpenBox-Event-Id": envelope["id"],
            "X-OpenBox-Event": envelope["type"],
            "X-OpenBox-Timestamp": envelope["created_at"],
        },
    )
    secret = str(config.get("secret") or "")
    if secret:
        request.add_header(
            SIGNATURE_HEADER,
            f"sha256={sign_event(secret, envelope['created_at'], body)}",
        )
    effective_timeout = timeout if timeout is not None else max(
        1, min(int(config.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
    )
    try:
        with (opener or urlopen)(request, timeout=effective_timeout) as response:
            status = int(getattr(response, "status", 200))
            _drain_response(response)
            return {
                "ok": status < 300,
                "status": status,
                "error": "" if status < 300 else f"HTTP {status}",
            }
    except (OSError, ValueError) as error:
        return {"ok": False, "status": None, "error": str(error)}
