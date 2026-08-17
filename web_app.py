#!/usr/bin/env python3
"""Local browser UI for OpenBox. Independent open-source software not affiliated with LaunchBox or Unbroken Software, LLC."""

import email.utils
import gzip
import json
import mimetypes
import os
import queue as queue_module
import secrets
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from api_errors import ApiError, BadRequest, RouteNotFound
from env_config import bootstrap_env
from openbox_logging import configure_logging
from openbox import DATA, load_state, purge_demo_games, update_state
from parity_backup import create_backup, restore_backup
from parity_deeplinks import handle_cli
from parity_emulator_defs import merge_profiles_from_definitions
from parity_gameyfin import GameyfinError
from parity_gamescope import OPENBOX_STEAM_GAME_ID, is_gamescope_guest, mark_process_windows, open_ui
from routes import dispatch_get, dispatch_post
from state_store import StateCorruptError, secure_text_write
from stock_themes import ensure_stock_themes
from webapp_state import (
    GZIP_THRESHOLD,
    JOB_MANAGER,
    LOGGER,
    PROCESS_LOCK,
    ROOT,
    RUNNING,
    TOKEN,
    WATCH_STOP,
    approved_backup_file,
    auto_import_worker,
    control_game_session,
    run_configured_commands,
    shutdown_webhooks,
    SSE_MAX_EVENT_BYTES,
    SSE_QUEUE_SIZE,
    SSE_WRITE_TIMEOUT,
    register_event_subscriber,
    unregister_event_subscriber,
)
from handlers.data import DataHandlers
from handlers.emulators import EmulatorsHandlers
from handlers.extensions import ExtensionsHandlers
from handlers.health import HealthHandlers
from handlers.imports import ImportsHandlers
from handlers.library import LibraryHandlers
from handlers.media import MediaHandlers
from handlers.metadata import MetadataHandlers
from handlers.sessions import SessionHandlers
from handlers.settings import SettingsHandlers

import re as _re

def _sanitize_error_message(error):
    """Return a client-safe error string without absolute filesystem paths.

    Full details are logged server-side; the client receives the prefix
    before the path to avoid leaking home-directory layout.
    """
    raw = str(error)
    if not raw:
        return raw
    # If the message contains an absolute path, strip the path portion.
    # Heuristic: absolute paths contain "/" and typical error prefixes contain ":"
    if "/" in raw or raw.strip().startswith("~"):
        if ":" in raw:
            # Keep text before the first path-like segment
            # e.g. "Folder does not exist: /tmp/foo" -> "Folder does not exist."
            prefix = raw.split(":", 1)[0].strip()
            # Only strip if the suffix looks like a path (contains /)
            suffix = raw.split(":", 1)[1]
            if "/" in suffix or "~" in suffix:
                # Preserve a trailing period for consistency
                if not prefix.endswith("."):
                    prefix += "."
                return prefix
        # Fallback: replace any absolute path token with [path]
        sanitized = _re.sub(r"(?:~)?/[^\s\"']*", "[path]", raw)
        sanitized = _re.sub(r"\[path\](?:\s*\[path\])+", "[path]", sanitized)
        # Collapse " : [path]" to "."
        sanitized = _re.sub(r"\s*:\s*\[path\].*", ".", sanitized)
        sanitized = sanitized.strip()
        if sanitized:
            return sanitized
    return raw


# Host header values the API accepts. The server binds loopback only; rejecting
# any other Host closes DNS-rebinding, where a remote page resolves to
# 127.0.0.1 and carries the attacker's hostname in the Host header.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Rate limiting for authentication failures (per client IP, loopback only).
# 10 failures in 60s triggers 429 with Retry-After; success resets the window.
_AUTH_FAILURES: dict[str, list[float]] = {}
_AUTH_FAILURES_LOCK = threading.Lock()
_AUTH_MAX_FAILURES = 10
_AUTH_WINDOW_SECONDS = 60.0

# Security headers shared by every response (including the SSE stream) so the
# policy can't drift between code paths.
CSP_DEFAULT = "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.bunny.net; font-src 'self' https://fonts.bunny.net; script-src 'self'; object-src 'none'; base-uri 'none'"


class Handler(LibraryHandlers, ImportsHandlers, MediaHandlers, MetadataHandlers, SessionHandlers, SettingsHandlers, ExtensionsHandlers, HealthHandlers, EmulatorsHandlers, DataHandlers, BaseHTTPRequestHandler):
    server_version = "OpenBox/1"
    protocol_version = "HTTP/1.1"
    MAX_BODY = 65536
    REQUEST_TIMEOUT = 30

    def setup(self):
        super().setup()
        self.connection.settimeout(self.REQUEST_TIMEOUT)

    def log_message(self, *_):
        pass

    def send_response(self, code, message=None):
        LOGGER.debug("HTTP %s %s -> %s", getattr(self, "command", "?"), urlparse(getattr(self, "path", "")).path, code)
        super().send_response(code, message)

    def headers_common(self, content_type, cache_control="no-store"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP_DEFAULT)

    def send_bytes(self, status, data, content_type, cache_control="no-store", etag=None, last_modified=None, extra_headers=None):
        if etag and self.headers.get("If-None-Match", "").strip() == etag:
            self.send_response(304)
            self.headers_common(content_type, cache_control=cache_control)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(status)
        self.headers_common(content_type, cache_control=cache_control)
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _cache_headers(self, path, stat_result):
        etag = f'"{stat_result.st_mtime_ns:x}-{stat_result.st_size:x}"'
        last_modified = email.utils.formatdate(stat_result.st_mtime, usegmt=True)
        return etag, last_modified

    def send_file(self, status, path, content_type=None):
        path = Path(path)
        stat_result = path.stat()
        size = stat_result.st_size
        etag, last_modified = self._cache_headers(path, stat_result)
        request_cache_control = "private, max-age=31536000, immutable"
        conditional = self.headers.get("If-None-Match", "")
        if etag in {item.strip() for item in conditional.split(",")}:
            self.send_response(304)
            self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return
        if not conditional:
            if_modified_since = self.headers.get("If-Modified-Since", "")
            if if_modified_since:
                try:
                    since = email.utils.parsedate_to_datetime(if_modified_since)
                    if stat_result.st_mtime < since.timestamp() + 1:
                        self.send_response(304)
                        self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
                        self.send_header("ETag", etag)
                        self.send_header("Last-Modified", last_modified)
                        self.end_headers()
                        return
                except (TypeError, ValueError):
                    pass
        start, end = 0, size - 1
        response_status = status
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            spec = range_header[6:].split(",", 1)[0].strip()
            if "-" not in spec:
                raise ValueError("Invalid byte range.")
            left, right = spec.split("-", 1)
            if left:
                start = int(left)
                end = int(right) if right else end
            elif right:
                length = int(right)
                start = max(0, size - length)
            if start < 0 or start >= size or end < start:
                self.send_response(416)
                self.headers_common(content_type or "application/octet-stream", cache_control=request_cache_control)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            response_status = 206
        length = max(0, end - start + 1)
        self.send_response(response_status)
        self.headers_common(content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream", cache_control=request_cache_control)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Content-Length", str(length))
        if response_status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self.wfile.flush()
        # Use os.sendfile for zero-copy transfer on full-file requests (Linux)
        if response_status == 200 and hasattr(os, "sendfile") and hasattr(self, "connection"):
            sendfile_sent = 0
            sendfile_failed = False
            try:
                file_fd = os.open(str(path), os.O_RDONLY)
                try:
                    offset = start
                    while sendfile_sent < length:
                        transferred = os.sendfile(self.connection.fileno(), file_fd, offset, min(1024 * 1024, length - sendfile_sent))
                        if transferred == 0:
                            break
                        offset += transferred
                        sendfile_sent += transferred
                finally:
                    os.close(file_fd)
            except (OSError, AttributeError):
                sendfile_failed = True
            # Fall back to read/write on sendfile failure, but only if nothing was sent yet.
            # If sendfile partially succeeded, we cannot know exactly how many bytes were sent,
            # so resuming would risk duplicating bytes and breaking Content-Length framing.
            if (sendfile_failed or sendfile_sent < length) and sendfile_sent == 0:
                with path.open("rb") as source:
                    source.seek(start)
                    remaining = length
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        else:
            with path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def send_json(self, status, payload, extra_headers=None):
        data = json.dumps(payload).encode()
        self.send_bytes(status, data, "application/json; charset=utf-8", extra_headers=extra_headers)

    def send_json_compressed(self, status, payload):
        """Send a JSON payload, gzipped for clients that accept it.

        Loopback bandwidth is free but compression wins on two fronts:
        large libraries make /api/library a multi-megabyte payload, and
        gzip shrinks the JSON to a fraction of that while the CPU cost is
        negligible on the local machine.
        """
        data = json.dumps(payload).encode()
        if len(data) >= GZIP_THRESHOLD and "gzip" in self.headers.get("Accept-Encoding", ""):
            compressed = gzip.compress(data)
            if len(compressed) < len(data):
                self.send_bytes(
                    status, compressed, "application/json; charset=utf-8",
                    extra_headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                )
                return
        self.send_bytes(status, data, "application/json; charset=utf-8")

    def _auth_client_ip(self):
        try:
            return self.client_address[0] if getattr(self, "client_address", None) else "127.0.0.1"
        except Exception:
            return "127.0.0.1"

    def _is_auth_rate_limited(self):
        now = time.monotonic()
        ip = self._auth_client_ip()
        with _AUTH_FAILURES_LOCK:
            attempts = _AUTH_FAILURES.get(ip, [])
            attempts = [t for t in attempts if now - t < _AUTH_WINDOW_SECONDS]
            _AUTH_FAILURES[ip] = attempts
            if len(attempts) >= _AUTH_MAX_FAILURES:
                oldest = min(attempts) if attempts else now
                return int(_AUTH_WINDOW_SECONDS - (now - oldest)) + 1
            return 0

    def _record_auth_failure(self):
        now = time.monotonic()
        ip = self._auth_client_ip()
        with _AUTH_FAILURES_LOCK:
            attempts = _AUTH_FAILURES.get(ip, [])
            attempts = [t for t in attempts if now - t < _AUTH_WINDOW_SECONDS]
            attempts.append(now)
            _AUTH_FAILURES[ip] = attempts
            if len(attempts) >= _AUTH_MAX_FAILURES:
                oldest = min(attempts)
                return int(_AUTH_WINDOW_SECONDS - (now - oldest)) + 1
            return 0

    def _clear_auth_failures(self):
        ip = self._auth_client_ip()
        with _AUTH_FAILURES_LOCK:
            _AUTH_FAILURES.pop(ip, None)

    def authorized(self):
        query_token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        provided = self.headers.get("X-OpenBox-Token", "") or query_token
        ok = secrets.compare_digest(provided, TOKEN)
        if ok:
            self._clear_auth_failures()
            return True
        # Wrong token: still check rate limit so handle_unauthorized can
        # decide between 403 and 429, but do not block correct tokens.
        return False

    def handle_unauthorized(self):
        retry = self._record_auth_failure()
        if retry:
            self.send_json(
                429,
                {"error": "Too many authentication failures. Try again later.", "code": "RATE_LIMITED", "retry_after": retry},
                extra_headers={"Retry-After": str(retry)},
            )
        else:
            # Re-check if this failure pushed us over the limit
            retry = self._is_auth_rate_limited()
            if retry:
                self.send_json(
                    429,
                    {"error": "Too many authentication failures. Try again later.", "code": "RATE_LIMITED", "retry_after": retry},
                    extra_headers={"Retry-After": str(retry)},
                )
            else:
                self.send_json(403, {"error": "Unauthorized"})

    def body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise ValueError("Content-Length must be a valid number.") from error
        if length < 0:
            raise ValueError("Content-Length must not be negative.")
        if length > self.MAX_BODY:
            raise ValueError("Request is too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("Request body was truncated.")
        return json.loads(raw or b"{}")

    def _do_GET(self):
        parsed = urlparse(self.path)
        try:
            dispatch_get(self, parsed)
        except ApiError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, json.JSONDecodeError, GameyfinError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
            LOGGER.warning("Request %s failed: %s", parsed.path, error)
            raise BadRequest(_sanitize_error_message(error)) from None

    def _api_get_index(self, parsed):
        if parsed.path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes().decode("utf-8")
            # The launcher opens the page with ?token= in the URL; app.js reads
            # it from location.search. Static assets are public paths and need
            # no token, so the shell itself carries no secrets.
            self.send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
    def _api_get_static(self, parsed):
        # Static UI assets (app.js/app.css plus the ES-module chunks) live
        # next to index.html. Serve them with long-lived caching keyed on
        # mtime+size ETags.
        name = Path(parsed.path).name
        if not (name.endswith(".js") or name in {"app.css", "logo.png"}):
            raise RouteNotFound("Not found")
        if name == "logo.png":
            asset = ROOT / "assets" / "openbox-logo.png"
            if asset.is_file():
                self.send_file(200, asset, "image/png")
                return
            raise RouteNotFound("Not found")
        asset = ROOT / "static" / name
        if not asset.is_file():
            raise RouteNotFound("Not found")
        content_type = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
        self.send_file(200, asset, content_type)
        return

    def _api_get_favicon(self, parsed):
        if parsed.path in ("/favicon.svg", "/favicon.ico"):
            # Browsers request an icon on every initial load; serve the
            # repo icon instead of a 404 console error.
            icon = ROOT / "openbox.svg"
            if icon.is_file():
                self.send_bytes(200, icon.read_bytes(), "image/svg+xml")
                return

    def _api_get_api_events(self, parsed):
        subscriber_queue = queue_module.Queue(maxsize=SSE_QUEUE_SIZE)
        if not register_event_subscriber(subscriber_queue):
            self.send_json(503, {"error": "Too many event streams."})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP_DEFAULT)
            self.end_headers()
            self.connection.settimeout(SSE_WRITE_TIMEOUT)
            while True:
                try:
                    item = subscriber_queue.get(timeout=15)
                except queue_module.Empty:
                    # Keep the connection alive with a heartbeat comment.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                kind, data = item
                event = f"event: {kind}\ndata: {data}\n\n".encode()
                if len(event) > SSE_MAX_EVENT_BYTES:
                    continue
                self.wfile.write(event)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unregister_event_subscriber(subscriber_queue)

    def _do_POST(self):
        try:
            payload = self.body()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            route = urlparse(self.path).path
            dispatch_post(self, route, payload)
        except ApiError:
            raise
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, json.JSONDecodeError, GameyfinError, FileNotFoundError, RuntimeError, subprocess.SubprocessError) as error:
            LOGGER.warning("Request %s failed: %s", urlparse(self.path).path, error)
            raise BadRequest(_sanitize_error_message(error)) from None

    def _loopback_host(self):
        headers = getattr(self, "headers", None)
        host = headers.get("Host", "") if headers is not None else ""
        if not host:
            LOGGER.warning("Rejecting request with missing or empty Host header")
            return False
        hostname = host.rsplit(":", 1)[0].strip("[]")
        return hostname in LOOPBACK_HOSTS

    def _handle_request(self, method):
        path = urlparse(self.path).path
        request_id = secrets.token_hex(4)
        LOGGER.debug("HTTP %s %s started [%s]", method, path, request_id)
        if not self._loopback_host():
            self.send_json(403, {"error": "Request host is not allowed.", "code": "BAD_HOST", "request_id": request_id})
            return
        try:
            getattr(self, f"_{method}")()
        except ApiError as error:
            LOGGER.info("HTTP %s %s [%s] %s: %s", method, path, request_id, error.code, error.message)
            self.send_json(error.status, error.to_payload(request_id))
        except StateCorruptError as error:
            LOGGER.error("OpenBox state is unavailable: %s", error)
            self.send_json(503, {"error": "OpenBox library data needs recovery before this operation can continue.", "code": "STATE_UNAVAILABLE", "request_id": request_id})
        except Exception:
            LOGGER.exception("Unhandled HTTP %s %s [%s]", method, path, request_id)
            self.send_json(500, {"error": "Unexpected server error. Copy the diagnostic log from Settings and include it in your report.", "code": "INTERNAL_ERROR", "request_id": request_id})

    def do_GET(self):
        self._handle_request("do_GET")

    def do_POST(self):
        self._handle_request("do_POST")


def main():
    bootstrap_env(DATA.parent)
    configure_logging(DATA.parent)
    LOGGER.info("OpenBox web UI starting")
    args = sys.argv[1:]
    if "--backup" in args:
        items = []
        keep = 0
        if "--items" in args:
            items = args[args.index("--items") + 1].split(",")
        if "--keep" in args:
            keep = int(args[args.index("--keep") + 1])
        state = load_state()
        archive = create_backup(DATA.parent, state, items or ["library", "settings"], keep=keep, running_map=RUNNING)
        print(archive)
        return
    if "--restore-backup" in args:
        archive = approved_backup_file(args[args.index("--restore-backup") + 1])
        restored = restore_backup(archive, DATA.parent, running_map=RUNNING)
        print(",".join(restored))
        return
    cli_code = handle_cli(args, DATA.parent)
    if cli_code is not None:
        raise SystemExit(cli_code)
    ensure_stock_themes(DATA.parent / "themes", ROOT)
    def bootstrap_state(state):
        purge_demo_games(state)
        profiles = state.setdefault("profiles", {})
        profiles.update(merge_profiles_from_definitions(profiles))
    update_state(bootstrap_state)
    WATCH_STOP.clear()
    JOB_MANAGER.submit("auto-import", auto_import_worker)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    run_configured_commands("startup_commands")
    port = server.server_address[1]
    secure_text_write(DATA.parent / "server.port", str(port))
    secure_text_write(DATA.parent / "server.token", TOKEN)
    _settings = load_state().get("settings", {})
    secure_text_write(
        DATA.parent / "native-host-flags",
        f"{int(bool(_settings.get('tray_enabled')))} {int(bool(_settings.get('minimize_to_tray')))}\n",
    )
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    # Log without the token; the token-bearing URL is only printed to the
    # native host via the token file and to a browser when explicitly opened.
    safe_url = f"http://127.0.0.1:{port}/"
    force_game_mode = "--game-mode" in sys.argv
    guest = is_gamescope_guest(force=force_game_mode)
    # Desktop sessions open the UI in a chrome-less app window by default;
    # flags override.
    if "--app-window" in sys.argv:
        native_window = True
    elif "--no-app-window" in sys.argv:
        native_window = False
    else:
        native_window = not guest
    print(f"http://127.0.0.1:{port}/", flush=True)
    LOGGER.info("OpenBox web UI URL: %s", safe_url)
    if "--no-browser" not in sys.argv:
        opened = open_ui(url, guest=guest, force_game_mode=force_game_mode, native_window=native_window)
        browser_pid = opened.get("pid")
        if opened.get("mode") == "kiosk" and guest and browser_pid:
            browser_name = Path(str(opened.get("browser") or "")).name
            class_hint = "google-chrome" if "chrome" in browser_name.casefold() else browser_name
            threading.Thread(
                target=mark_process_windows,
                kwargs={
                    "pid": browser_pid,
                    "app_id": OPENBOX_STEAM_GAME_ID,
                    "window_class": class_hint,
                },
                daemon=True,
            ).start()
    def stop():
        """Graceful shutdown: stop sessions, then stop accepting requests."""
        with PROCESS_LOCK:
            launch_ids = list(RUNNING.keys())
        for launch_id in launch_ids:
            try:
                control_game_session(launch_id, "stop")
            except ValueError:
                pass
        shutdown_webhooks(wait_seconds=2.0)

    def request_shutdown(_signum, _frame):
        # Serve_forever is running in this thread; raise so the finally block
        # runs the graceful teardown. The host sends SIGTERM after POSTing
        # /api/shutdown, so this is the backstop, not the primary path.
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, request_shutdown)
    previous_sigint = signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop()
        WATCH_STOP.set()
        JOB_MANAGER.cancel("auto-import")
        JOB_MANAGER.shutdown(wait=False, cancel_futures=True)
        server.server_close()
        (DATA.parent / "server.token").unlink(missing_ok=True)
        (DATA.parent / "server.port").unlink(missing_ok=True)
        run_configured_commands("shutdown_commands")
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    main()
