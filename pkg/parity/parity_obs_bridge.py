"""Small, dependency-free OBS WebSocket 5 bridge.

The bridge deliberately implements only the part of obs-websocket needed by
Record That: the HTTP/WebSocket upgrade, Hello/Identify, and request frames.
It does not depend on a WebSocket package because OBS is an optional local
integration.  The low-level client raises :class:`OBSBridgeError` subclasses
for diagnostics; the settings helpers below turn an unavailable, disabled,
or misconfigured OBS into a quiet ``None``/``False`` result.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import socket
import struct
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4455
DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 30.0
MAX_HANDSHAKE_BYTES = 64 * 1024
MAX_FRAME_BYTES = 8 * 1024 * 1024
RPC_VERSION = 1


class OBSBridgeError(RuntimeError):
    """Base error for an OBS protocol or transport failure."""


class OBSUnavailableError(OBSBridgeError):
    """OBS is not listening, or the connection disappeared."""


class OBSTimeoutError(OBSBridgeError):
    """The bounded connection or request deadline expired."""


class OBSProtocolError(OBSBridgeError):
    """The peer sent an invalid WebSocket or OBS protocol message."""


class OBSAuthenticationError(OBSBridgeError):
    """The OBS server requires credentials that were not supplied."""


class OBSRequestError(OBSBridgeError):
    """OBS rejected a request after a valid protocol exchange."""


# Short aliases are useful to embedders that used the names from the
# obs-websocket documentation while keeping the more explicit names public.
OBSWebSocketError = OBSBridgeError
OBSAuthError = OBSAuthenticationError


def _bounded_timeout(value) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("OBS timeout must be a positive number.") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OBS timeout must be a positive number.")
    return min(timeout, MAX_TIMEOUT)


def _port(value) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("OBS port must be an integer.") from error
    if not 1 <= port <= 65535:
        raise ValueError("OBS port must be between 1 and 65535.")
    return port


def _host(value) -> str:
    host = str(value or "").strip()
    if not host or any(ord(character) < 32 or ord(character) == 127 for character in host):
        raise ValueError("OBS host is invalid.")
    return host


def parse_endpoint(endpoint, default_host=DEFAULT_HOST, default_port=DEFAULT_PORT):
    """Return ``(host, port, request_target)`` for a local ``ws://`` URL.

    A bare ``host:port`` is accepted as a convenience.  TLS is intentionally
    not guessed or silently downgraded: the default OBS endpoint is local and
    an unsupported ``wss://`` setting fails closed.
    """

    host = _host(default_host)
    port = _port(default_port)
    if endpoint in (None, ""):
        return host, port, "/"
    text = str(endpoint).strip()
    if "://" not in text:
        text = f"ws://{text}"
    parsed = urlsplit(text)
    if parsed.scheme.casefold() != "ws":
        raise ValueError("OBS WebSocket endpoint must use ws://.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("OBS WebSocket credentials belong in settings, not the URL.")
    try:
        parsed_host = parsed.hostname
        parsed_port = parsed.port
    except ValueError as error:
        raise ValueError("OBS WebSocket endpoint has an invalid port.") from error
    if not parsed_host:
        raise ValueError("OBS WebSocket endpoint has no host.")
    host = _host(parsed_host)
    if parsed_port is not None:
        port = _port(parsed_port)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    if parsed.fragment:
        raise ValueError("OBS WebSocket endpoint must not contain a fragment.")
    if any(ord(character) < 32 or ord(character) == 127 for character in target):
        raise ValueError("OBS WebSocket endpoint path is invalid.")
    return host, port, target


def build_authentication(password, salt, challenge) -> str:
    """Build the obs-websocket v5 challenge response.

    OBS never receives the password itself.  The protocol is
    ``base64(sha256(base64(sha256(password + salt)) + challenge))``.
    """

    if password is None:
        raise OBSAuthenticationError("OBS requires a WebSocket password.")
    password_bytes = str(password).encode("utf-8")
    salt_bytes = str(salt).encode("utf-8")
    challenge_bytes = str(challenge).encode("utf-8")
    # codeql[py/weak-sensitive-data-hashing]: OBS WebSocket v5 requires SHA-256 here; this is not password storage.
    secret = base64.b64encode(hashlib.sha256(password_bytes + salt_bytes).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge_bytes).digest()).decode("ascii")


def _json_bytes(message) -> bytes:
    if isinstance(message, bytes):
        payload = message
    elif isinstance(message, str):
        payload = message.encode("utf-8")
    else:
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise OBSProtocolError("OBS WebSocket frame is too large.")
    return payload


def encode_frame(payload, *, mask=True, mask_key=None, opcode=1, fin=True) -> bytes:
    """Encode one RFC 6455 frame.

    Client frames are masked by default, as required by the WebSocket
    protocol.  ``payload`` may be bytes, text, or a JSON-serializable object.
    """

    if not 0 <= int(opcode) <= 15:
        raise ValueError("WebSocket opcode must fit in four bits.")
    payload_bytes = _json_bytes(payload)
    if int(opcode) >= 8 and (not fin or len(payload_bytes) > 125):
        raise ValueError("WebSocket control frames must be final and at most 125 bytes.")
    if mask:
        key = secrets.token_bytes(4) if mask_key is None else bytes(mask_key)
        if len(key) != 4:
            raise ValueError("WebSocket mask key must contain four bytes.")
        encoded = bytes(value ^ key[index % 4] for index, value in enumerate(payload_bytes))
    else:
        key = b""
        encoded = payload_bytes
    length = len(encoded)
    if length < 126:
        length_bytes = bytes([length])
    elif length <= 0xFFFF:
        length_bytes = b"\x7e" + struct.pack("!H", length)
    else:
        length_bytes = b"\x7f" + struct.pack("!Q", length)
    first = (0x80 if fin else 0) | int(opcode)
    second = (0x80 if mask else 0) | length_bytes[0]
    if length < 126:
        header = bytes((first, second))
    else:
        header = bytes((first, second)) + length_bytes[1:]
    return header + key + encoded


def _parse_frame(buffer: bytes | bytearray, offset=0):
    """Parse a complete frame from ``buffer``.

    Returns ``(fin, opcode, payload, next_offset)``.  ``None`` means that the
    buffer is incomplete; malformed input raises ``OBSProtocolError``.
    """

    if len(buffer) - offset < 2:
        return None
    first, second = buffer[offset], buffer[offset + 1]
    fin = bool(first & 0x80)
    if first & 0x70:
        raise OBSProtocolError("OBS WebSocket reserved bits are not supported.")
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length_code = second & 0x7F
    cursor = offset + 2
    if length_code == 126:
        if len(buffer) - cursor < 2:
            return None
        length = struct.unpack("!H", bytes(buffer[cursor:cursor + 2]))[0]
        cursor += 2
    elif length_code == 127:
        if len(buffer) - cursor < 8:
            return None
        length = struct.unpack("!Q", bytes(buffer[cursor:cursor + 8]))[0]
        cursor += 8
        if length & (1 << 63):
            raise OBSProtocolError("OBS WebSocket frame length is invalid.")
    else:
        length = length_code
    if length > MAX_FRAME_BYTES:
        raise OBSProtocolError("OBS WebSocket frame is too large.")
    if opcode >= 8 and (not fin or length > 125):
        raise OBSProtocolError("OBS WebSocket control frame is invalid.")
    if masked:
        if len(buffer) - cursor < 4:
            return None
        mask = bytes(buffer[cursor:cursor + 4])
        cursor += 4
    else:
        mask = None
    if len(buffer) - cursor < length:
        return None
    payload = bytes(buffer[cursor:cursor + length])
    if mask is not None:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return fin, opcode, payload, cursor + length


def decode_frame(frame: bytes):
    """Decode one complete frame, returning ``(fin, opcode, payload)``."""

    parsed = _parse_frame(frame)
    if parsed is None or parsed[3] != len(frame):
        raise OBSProtocolError("WebSocket frame is incomplete or has trailing bytes.")
    return parsed[:3]


class OBSBridge:
    """A single bounded obs-websocket v5 connection.

    The class is intentionally lazy: constructing it never opens a socket.
    ``connect`` is called explicitly or automatically by ``request``.  A
    ``socket_factory`` accepting ``((host, port), timeout)`` and an optional
    ``transport`` socket make the protocol straightforward to test without a
    live OBS process.
    """

    def __init__(
        self,
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
        password=None,
        timeout=DEFAULT_TIMEOUT,
        *,
        endpoint=None,
        url=None,
        socket_factory=None,
        transport=None,
        rpc_version=RPC_VERSION,
        request_id_factory=None,
        key_factory=None,
        clock=None,
    ):
        if endpoint is None:
            endpoint = url
        if endpoint is None and isinstance(host, str) and "://" in host:
            endpoint, host = host, DEFAULT_HOST
        parsed_host, parsed_port, request_target = parse_endpoint(endpoint, host, port)
        self.host = parsed_host
        self.port = parsed_port
        self.request_target = request_target
        self.password = None if password is None else str(password)
        self.timeout = _bounded_timeout(timeout)
        try:
            self.rpc_version = int(rpc_version)
        except (TypeError, ValueError) as error:
            raise ValueError("OBS rpc_version must be an integer.") from error
        if self.rpc_version < 1:
            raise ValueError("OBS rpc_version must be positive.")
        self._socket_factory = socket_factory or socket.create_connection
        self._socket = transport
        self._buffer = bytearray()
        self._connected = False
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self._key_factory = key_factory or (lambda: base64.b64encode(secrets.token_bytes(16)).decode("ascii"))
        self._clock = clock or time.monotonic
        self.hello = None
        self._events = []

    def __repr__(self):
        # Never expose the configured password in logs or debugging output.
        state = "connected" if self._connected else "closed"
        return f"OBSBridge(host={self.host!r}, port={self.port!r}, state={state!r})"

    @property
    def connected(self) -> bool:
        return self._connected

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def _deadline(self):
        return self._clock() + self.timeout

    def _set_timeout(self, deadline):
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise OBSTimeoutError("OBS WebSocket operation timed out.")
        if self._socket is not None and hasattr(self._socket, "settimeout"):
            try:
                self._socket.settimeout(remaining)
            except (OSError, ValueError) as error:
                raise OBSUnavailableError("OBS WebSocket timeout could not be set.") from error

    def _send(self, payload, *, opcode=1, fin=True):
        if self._socket is None:
            raise OBSUnavailableError("OBS WebSocket is not connected.")
        frame = encode_frame(payload, mask=True, opcode=opcode, fin=fin)
        try:
            self._socket.sendall(frame)
        except (OSError, ValueError) as error:
            raise OBSUnavailableError("OBS WebSocket send failed.") from error

    def _fill(self, deadline, minimum=1):
        if self._socket is None:
            raise OBSUnavailableError("OBS WebSocket is not connected.")
        while len(self._buffer) < minimum:
            self._set_timeout(deadline)
            try:
                chunk = self._socket.recv(max(4096, minimum - len(self._buffer)))
            except (socket.timeout, TimeoutError) as error:
                raise OBSTimeoutError("OBS WebSocket operation timed out.") from error
            except OSError as error:
                raise OBSUnavailableError("OBS WebSocket receive failed.") from error
            if not chunk:
                raise OBSUnavailableError("OBS WebSocket peer closed the connection.")
            self._buffer.extend(chunk)

    def _read_until(self, marker: bytes, deadline, limit: int):
        while True:
            position = self._buffer.find(marker)
            if position >= 0:
                end = position + len(marker)
                result = bytes(self._buffer[:end])
                del self._buffer[:end]
                return result
            if len(self._buffer) > limit:
                raise OBSProtocolError("OBS WebSocket handshake is too large.")
            self._fill(deadline, len(self._buffer) + 1)

    def _read_exact(self, size: int, deadline) -> bytes:
        if size < 0 or size > MAX_FRAME_BYTES + 14:
            raise OBSProtocolError("OBS WebSocket read size is invalid.")
        self._fill(deadline, size)
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def _receive_frame(self, deadline):
        self._fill(deadline, 2)
        first, second = self._buffer[0], self._buffer[1]
        if first & 0x70:
            raise OBSProtocolError("OBS WebSocket reserved bits are not supported.")
        length_code = second & 0x7F
        extra = 0 if length_code < 126 else (2 if length_code == 126 else 8)
        header = self._read_exact(2 + extra, deadline)
        if extra == 0:
            length = length_code
        elif extra == 2:
            length = struct.unpack("!H", header[2:])[0]
        else:
            length = struct.unpack("!Q", header[2:])[0]
            if length & (1 << 63):
                raise OBSProtocolError("OBS WebSocket frame length is invalid.")
        if length > MAX_FRAME_BYTES:
            raise OBSProtocolError("OBS WebSocket frame is too large.")
        mask = self._read_exact(4, deadline) if second & 0x80 else None
        payload = self._read_exact(length, deadline)
        if mask is not None:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if opcode >= 8 and (not fin or length > 125):
            raise OBSProtocolError("OBS WebSocket control frame is invalid.")
        return fin, opcode, payload

    def _receive_message(self, deadline):
        initial_opcode = None
        fragments = []
        total = 0
        while True:
            fin, opcode, payload = self._receive_frame(deadline)
            if opcode == 9:  # ping
                self._send(payload, opcode=10)
                continue
            if opcode == 10:  # unsolicited pong
                continue
            if opcode == 8:
                raise OBSUnavailableError("OBS WebSocket peer closed the connection.")
            if opcode == 0:
                if initial_opcode is None:
                    raise OBSProtocolError("OBS WebSocket continuation has no start frame.")
            else:
                if initial_opcode is not None:
                    raise OBSProtocolError("OBS WebSocket message is interleaved.")
                initial_opcode = opcode
            total += len(payload)
            if total > MAX_FRAME_BYTES:
                raise OBSProtocolError("OBS WebSocket message is too large.")
            fragments.append(payload)
            if fin:
                if initial_opcode != 1:
                    raise OBSProtocolError("OBS WebSocket message is not JSON text.")
                try:
                    return json.loads(b"".join(fragments).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise OBSProtocolError("OBS WebSocket message is not valid JSON.") from error

    def _handshake(self, deadline):
        key = str(self._key_factory())
        request = (
            f"GET {self.request_target} HTTP/1.1\r\n"
            f"Host: {_host_header(self.host, self.port)}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Protocol: obswebsocket.json\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            self._socket.sendall(request)
        except (OSError, ValueError) as error:
            raise OBSUnavailableError("OBS WebSocket handshake send failed.") from error
        response = self._read_until(b"\r\n\r\n", deadline, MAX_HANDSHAKE_BYTES)
        try:
            header_text = response.decode("iso-8859-1")
            lines = header_text[:-4].split("\r\n")
            status = int(lines[0].split(" ", 2)[1])
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().casefold()] = value.strip()
        except (IndexError, ValueError, UnicodeDecodeError) as error:
            raise OBSProtocolError("OBS WebSocket handshake response is invalid.") from error
        if status != 101:
            raise OBSUnavailableError(f"OBS WebSocket upgrade failed with HTTP {status}.")
        if headers.get("upgrade", "").casefold() != "websocket":
            raise OBSProtocolError("OBS WebSocket upgrade header is invalid.")
        if "upgrade" not in {part.strip().casefold() for part in headers.get("connection", "").split(",")}:
            raise OBSProtocolError("OBS WebSocket connection header is invalid.")
        expected = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise OBSProtocolError("OBS WebSocket handshake key was rejected.")

    def connect(self):
        """Open the socket, complete Hello/Identify, and return ``self``."""

        if self._connected:
            return self
        deadline = self._deadline()
        if self._socket is None:
            try:
                self._socket = self._socket_factory((self.host, self.port), self.timeout)
            except (socket.timeout, TimeoutError) as error:
                raise OBSTimeoutError("OBS WebSocket connection timed out.") from error
            except (OSError, ValueError) as error:
                raise OBSUnavailableError("OBS WebSocket is unavailable.") from error
        try:
            self._set_timeout(deadline)
            self._handshake(deadline)
            hello = self._receive_message(deadline)
            if not isinstance(hello, dict) or hello.get("op") != 0 or not isinstance(hello.get("d"), dict):
                raise OBSProtocolError("OBS WebSocket Hello message is invalid.")
            self.hello = hello
            hello_data = hello["d"]
            version = str(hello_data.get("obsWebSocketVersion") or "")
            if version and not version.startswith("5."):
                raise OBSProtocolError("OBS WebSocket peer is not version 5.")
            identify = {"rpcVersion": self.rpc_version}
            authentication = hello_data.get("authentication")
            if authentication:
                if not isinstance(authentication, dict):
                    raise OBSProtocolError("OBS WebSocket authentication data is invalid.")
                salt = authentication.get("salt")
                challenge = authentication.get("challenge")
                if not salt or not challenge:
                    raise OBSProtocolError("OBS WebSocket authentication challenge is incomplete.")
                identify["authentication"] = build_authentication(self.password, salt, challenge)
            self._send({"op": 1, "d": identify})
            while True:
                identified = self._receive_message(deadline)
                if not isinstance(identified, dict):
                    raise OBSProtocolError("OBS WebSocket Identified message is invalid.")
                if identified.get("op") == 2:
                    self._connected = True
                    return self
                if identified.get("op") == 5:
                    continue
                raise OBSProtocolError("OBS WebSocket Identify was not accepted.")
        except OBSBridgeError:
            self.close()
            raise
        except (socket.timeout, TimeoutError) as error:
            self.close()
            raise OBSTimeoutError("OBS WebSocket connection timed out.") from error
        except (OSError, ValueError) as error:
            self.close()
            raise OBSUnavailableError("OBS WebSocket connection failed.") from error

    def close(self):
        """Close the transport, swallowing teardown errors."""

        transport = self._socket
        self._socket = None
        self._connected = False
        self.hello = None
        self._buffer.clear()
        self._events.clear()
        if transport is None:
            return
        try:
            shutdown = getattr(transport, "shutdown", None)
            if shutdown is not None:
                shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
        try:
            transport.close()
        except (OSError, ValueError):
            pass

    def _ensure_connected(self):
        if not self._connected:
            self.connect()

    def request(self, request_type, request_data=None, *, request_id=None):
        """Send an op=6 request and return its ``responseData`` mapping."""

        name = str(request_type or "").strip()
        if not name or any(ord(character) < 32 for character in name):
            raise ValueError("OBS request type is invalid.")
        if request_data is not None and not isinstance(request_data, dict):
            raise ValueError("OBS request data must be a mapping.")
        self._ensure_connected()
        identifier = str(request_id or self._request_id_factory())
        if not identifier:
            raise ValueError("OBS request id is invalid.")
        request = {
            "op": 6,
            "d": {
                "requestType": name,
                "requestId": identifier,
            },
        }
        if request_data is not None:
            request["d"]["requestData"] = request_data
        deadline = self._deadline()
        try:
            self._send(request)
            while True:
                response = self._receive_message(deadline)
                if not isinstance(response, dict):
                    raise OBSProtocolError("OBS request response is invalid.")
                if response.get("op") == 5:
                    if len(self._events) >= 64:
                        del self._events[: len(self._events) - 63]
                    self._events.append(response)
                    continue
                if response.get("op") != 7 or not isinstance(response.get("d"), dict):
                    raise OBSProtocolError("OBS request response opcode is invalid.")
                data = response["d"]
                if str(data.get("requestId") or "") != identifier:
                    continue
                status = data.get("requestStatus")
                if not isinstance(status, dict) or not status.get("result"):
                    code = status.get("code") if isinstance(status, dict) else "unknown"
                    comment = status.get("comment") if isinstance(status, dict) else ""
                    suffix = f": {comment}" if comment else ""
                    raise OBSRequestError(f"OBS request {name} failed ({code}){suffix}")
                result = data.get("responseData")
                return result if isinstance(result, dict) else {}
        except OBSBridgeError:
            raise
        except (socket.timeout, TimeoutError) as error:
            raise OBSTimeoutError("OBS request timed out.") from error
        except (OSError, ValueError) as error:
            raise OBSUnavailableError("OBS request failed.") from error

    def toggle_replay_buffer(self):
        """Toggle OBS's replay buffer and return response data."""

        return self.request("ToggleReplayBuffer")

    toggle_replay = toggle_replay_buffer

    def get_replay_buffer_status(self):
        """Return OBS's current replay-buffer status without changing it."""

        return self.request("GetReplayBufferStatus")

    def start_replay_buffer(self):
        """Start OBS's replay buffer explicitly."""

        return self.request("StartReplayBuffer")

    def stop_replay_buffer(self):
        """Stop OBS's replay buffer explicitly."""

        return self.request("StopReplayBuffer")

    def _take_saved_replay_event(self):
        for index, message in enumerate(self._events):
            event = _saved_replay_event(message)
            if event is not None:
                del self._events[index]
                return event
        return None

    def save_replay_buffer(self):
        """Save the current replay buffer and return OBS response data."""

        result = self.request("SaveReplayBuffer")
        if _saved_replay_path(result):
            return result
        event = self._take_saved_replay_event()
        if event is not None:
            return event
        deadline = self._deadline()
        while True:
            message = self._receive_message(deadline)
            event = _saved_replay_event(message)
            if event is not None:
                return event

    # The shorter spelling is convenient for deeplink/callback callers.
    save_replay = save_replay_buffer

    def set_record_directory(self, directory):
        """Ask OBS to use ``directory`` for future recordings."""

        value = str(Path(directory).expanduser())
        if not value or "\x00" in value:
            raise ValueError("OBS recording directory is invalid.")
        return self.request("SetRecordDirectory", {"recordDirectory": value})

    @classmethod
    def from_settings(cls, settings, **overrides):
        """Construct a bridge from common OpenBox OBS setting names."""

        values = settings if isinstance(settings, dict) else {}
        endpoint = overrides.pop("endpoint", None)
        if endpoint is None:
            endpoint = _first_setting(values, "obs_websocket_url", "obs_ws_url", "obs_url", "obs_endpoint")
        host = overrides.pop("host", None)
        if host is None:
            host = _first_setting(values, "obs_websocket_host", "obs_ws_host", "obs_host", default=DEFAULT_HOST)
        port = overrides.pop("port", None)
        if port is None:
            port = _first_setting(values, "obs_websocket_port", "obs_ws_port", "obs_port", default=DEFAULT_PORT)
        password = overrides.pop("password", None)
        if password is None:
            password = _first_setting(values, "obs_websocket_password", "obs_ws_password", "obs_password")
        timeout = overrides.pop("timeout", None)
        if timeout is None:
            timeout = _first_setting(values, "obs_websocket_timeout", "obs_timeout", default=DEFAULT_TIMEOUT)
        return cls(host, port, password, timeout, endpoint=endpoint, **overrides)


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _host_header(host, port):
    rendered = host
    if ":" in rendered and not rendered.startswith("["):
        rendered = f"[{rendered}]"
    return f"{rendered}:{port}"


def _first_setting(settings, *keys, default=None):
    for key in keys:
        if key in settings and settings[key] not in (None, ""):
            return settings[key]
    return default


def _saved_replay_path(value):
    """Find a path in a SaveReplayBuffer response or event payload."""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("savedFilename", "saved_filename", "savedReplayPath", "saved_replay_path", "filename", "path"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _saved_replay_event(message):
    """Project an OBS ReplayBufferSaved event to the clip response shape."""
    if not isinstance(message, dict) or message.get("op") != 5:
        return None
    data = message.get("d")
    if not isinstance(data, dict) or str(data.get("eventType") or "") != "ReplayBufferSaved":
        return None
    event_data = data.get("eventData")
    path = _saved_replay_path(event_data)
    return {"savedFilename": path} if path else None


def replay_enabled(settings) -> bool:
    """Return true only for an explicit ``obs_replay_enabled`` opt-in."""

    if not isinstance(settings, dict):
        return False
    value = settings.get("obs_replay_enabled", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return value is True or (isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0)


def _safe_replay_call(settings, method, *, bridge=None, **kwargs):
    if settings is not None and not replay_enabled(settings):
        return None
    if bridge is None and settings is None:
        return None
    owned = bridge is None
    client = None
    try:
        client = bridge or OBSBridge.from_settings(settings, **kwargs)
        return getattr(client, method)()
    except Exception:  # noqa: BLE001 - optional OBS must never block the caller
        return None
    finally:
        if owned and client is not None:
            client.close()


def save_replay_buffer(settings=None, *, bridge=None, **kwargs):
    """Best-effort replay save; disabled or unavailable OBS returns ``None``."""

    return _safe_replay_call(settings, "save_replay_buffer", bridge=bridge, **kwargs)


def save_replay(settings=None, *, bridge=None, **kwargs):
    """Alias for :func:`save_replay_buffer` used by clip actions."""

    return save_replay_buffer(settings, bridge=bridge, **kwargs)


def arm_replay_buffer(settings=None, *, bridge=None, **kwargs):
    """Start replay only when it is not already active.

    The returned ``owned`` flag lets the session lifecycle stop only a buffer
    that OpenBox started; a user's pre-armed OBS buffer is left alone.
    """

    status = _safe_replay_call(settings, "get_replay_buffer_status", bridge=bridge, **kwargs)
    if status is None:
        return None
    active = any(
        status.get(key) is True
        for key in ("outputActive", "replayBufferActive", "replay_buffer_active", "active")
        if isinstance(status, dict)
    )
    if active:
        return {"armed": True, "owned": False, "status": status}
    started = _safe_replay_call(settings, "start_replay_buffer", bridge=bridge, **kwargs)
    if started is None:
        return None
    return {"armed": True, "owned": True, "status": started}


def disarm_replay_buffer(settings=None, *, bridge=None, **kwargs):
    """Best-effort explicit stop for a buffer owned by the caller."""

    return _safe_replay_call(settings, "stop_replay_buffer", bridge=bridge, **kwargs)


def set_record_directory(settings, directory, *, bridge=None, **kwargs):
    """Best-effort recording-directory request for enabled OBS."""

    if settings is not None and not replay_enabled(settings):
        return None
    if bridge is None and settings is None:
        return None
    owned = bridge is None
    client = None
    try:
        client = bridge or OBSBridge.from_settings(settings, **kwargs)
        return client.set_record_directory(directory)
    except Exception:  # noqa: BLE001 - optional OBS must never block the caller
        return None
    finally:
        if owned and client is not None:
            client.close()


def obs_available(settings, *, bridge=None, **kwargs) -> bool:
    """Probe enabled OBS without allowing a failure to escape."""

    if settings is not None and not replay_enabled(settings):
        return False
    if bridge is None and settings is None:
        return False
    owned = bridge is None
    client = None
    try:
        client = bridge or OBSBridge.from_settings(settings, **kwargs)
        client.connect()
        return True
    except Exception:  # noqa: BLE001 - optional OBS must never block the caller
        return False
    finally:
        if owned and client is not None:
            client.close()


# Names used by small integrations and older spikes.
OBSWebSocket = OBSBridge
OBSClient = OBSBridge
ObsBridge = OBSBridge
ObsWebSocket = OBSBridge
ObsWebsocket = OBSBridge
OBSWebsocket = OBSBridge
build_auth = build_authentication
make_authentication = build_authentication
encode_websocket_frame = encode_frame
decode_websocket_frame = decode_frame


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_TIMEOUT",
    "OBSBridge",
    "OBSClient",
    "OBSWebSocket",
    "ObsBridge",
    "ObsWebSocket",
    "ObsWebsocket",
    "OBSWebsocket",
    "OBSBridgeError",
    "OBSWebSocketError",
    "OBSUnavailableError",
    "OBSTimeoutError",
    "OBSProtocolError",
    "OBSAuthenticationError",
    "OBSAuthError",
    "OBSRequestError",
    "parse_endpoint",
    "build_authentication",
    "make_authentication",
    "encode_frame",
    "decode_frame",
    "replay_enabled",
    "save_replay_buffer",
    "save_replay",
    "arm_replay_buffer",
    "disarm_replay_buffer",
    "set_record_directory",
    "obs_available",
]
