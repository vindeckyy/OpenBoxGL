"""Tests for the dependency-free OBS WebSocket v5 bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity import parity_obs_bridge as obs  # noqa: E402


class ScriptedSocket:
    """A socket-shaped scripted transport for handshake/frame tests."""

    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def sendall(self, value):
        self.sent.append(bytes(value))

    def recv(self, size):
        if not self.incoming:
            return b""
        amount = min(int(size), len(self.incoming))
        value = bytes(self.incoming[:amount])
        del self.incoming[:amount]
        return value

    def shutdown(self, how):
        del how

    def close(self):
        self.closed = True


class TimeoutSocket(ScriptedSocket):
    def recv(self, size):
        del size
        raise socket.timeout("timed out")


def _server_frame(payload, *, opcode=1, fin=True):
    return obs.encode_frame(payload, mask=False, opcode=opcode, fin=fin)


def _scripted_transport(*messages, password=False, key="dGhlIHNhbXBsZSBub25jZQ==", hello_data=None):
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    hello_data = hello_data or {"obsWebSocketVersion": "5.0", "rpcVersion": 1}
    if password:
        hello_data["authentication"] = {"salt": "salt", "challenge": "challenge"}
    incoming = (
        f"HTTP/1.1 101 Switching Protocols\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode() + _server_frame({"op": 0, "d": hello_data}) + b"".join(
        _server_frame(message) for message in messages
    )
    return ScriptedSocket(incoming)


class OBSBridgeTests(unittest.TestCase):
    def test_endpoint_and_constructor_validation(self):
        self.assertEqual(obs.parse_endpoint(None), ("127.0.0.1", 4455, "/"))
        self.assertEqual(obs.parse_endpoint("localhost:4456"), ("localhost", 4456, "/"))
        self.assertEqual(
            obs.parse_endpoint("ws://[::1]:4456/obs?rpc=1"),
            ("::1", 4456, "/obs?rpc=1"),
        )
        self.assertEqual(obs.parse_endpoint("ws://localhost"), ("localhost", 4455, "/"))
        with self.assertRaises(ValueError):
            obs.parse_endpoint("wss://localhost")
        with self.assertRaises(ValueError):
            obs.parse_endpoint("ws://user:password@localhost")
        with self.assertRaises(ValueError):
            obs.parse_endpoint("ws://localhost:not-a-port")
        with self.assertRaises(ValueError):
            obs.parse_endpoint("ws:///missing-host")
        with self.assertRaises(ValueError):
            obs.parse_endpoint("ws://localhost/path#fragment")
        with self.assertRaises(ValueError):
            obs.parse_endpoint("ws://localhost/\x01bad")
        with self.assertRaises(ValueError):
            obs.parse_endpoint(None, "\x7f")
        for value in (None, "bad", 0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                obs._bounded_timeout(value)
        self.assertEqual(obs._bounded_timeout(999), obs.MAX_TIMEOUT)
        for value in (None, "bad", 0, 65536):
            with self.assertRaises(ValueError):
                obs._port(value)
        with self.assertRaises(obs.OBSAuthenticationError):
            obs.build_authentication(None, "salt", "challenge")
        with self.assertRaises(ValueError):
            obs.OBSBridge(transport=ScriptedSocket(), rpc_version="bad")
        with self.assertRaises(ValueError):
            obs.OBSBridge(transport=ScriptedSocket(), rpc_version=0)

    def test_frame_helpers_reject_malformed_input(self):
        self.assertIsNone(obs._parse_frame(b""))
        self.assertIsNone(obs._parse_frame(b"\x81"))
        self.assertIsNone(obs._parse_frame(b"\x81\x7e\x00"))
        self.assertIsNone(obs._parse_frame(b"\x81\x80"))
        self.assertIsNone(obs._parse_frame(b"\x81\x03x"))
        with self.assertRaises(obs.OBSProtocolError):
            obs._parse_frame(b"\xc1\x00")
        with self.assertRaises(obs.OBSProtocolError):
            obs._parse_frame(b"\x81\x7f" + struct.pack("!Q", 1 << 63))
        with self.assertRaises(obs.OBSProtocolError):
            obs._parse_frame(b"\x81\x7f" + struct.pack("!Q", obs.MAX_FRAME_BYTES + 1))
        with self.assertRaises(obs.OBSProtocolError):
            obs._parse_frame(b"\x09\x00")
        with self.assertRaises(obs.OBSProtocolError):
            obs.decode_frame(obs.encode_frame(b"ok", mask=False) + b"trailing")
        with self.assertRaises(ValueError):
            obs.encode_frame(b"", opcode=16)
        with self.assertRaises(ValueError):
            obs.encode_frame(b"", opcode=9, fin=False)
        with self.assertRaises(ValueError):
            obs.encode_frame(b"x", mask=True, mask_key=b"bad")
        with self.assertRaises(obs.OBSProtocolError):
            obs._json_bytes(b"x" * (obs.MAX_FRAME_BYTES + 1))

    def test_receive_message_handles_controls_fragments_and_protocol_errors(self):
        payload = b'{"fragmented":true}'
        transport = ScriptedSocket(
            _server_frame(b"pong", opcode=10)
            + _server_frame(b"ping", opcode=9)
            + _server_frame(payload[:8], fin=False)
            + _server_frame(payload[8:], opcode=0)
        )
        bridge = obs.OBSBridge(transport=transport)
        self.assertEqual(bridge._receive_message(bridge._deadline()), {"fragmented": True})
        self.assertEqual(obs.decode_frame(transport.sent[0]), (True, 10, b"ping"))

        for incoming, expected in (
            (_server_frame(b"part", opcode=0), obs.OBSProtocolError),
            (_server_frame(b"one", fin=False) + _server_frame(b"two"), obs.OBSProtocolError),
            (_server_frame(b"closed", opcode=8), obs.OBSUnavailableError),
            (_server_frame(b"binary", opcode=2), obs.OBSProtocolError),
        ):
            with self.subTest(expected=expected.__name__):
                candidate = obs.OBSBridge(transport=ScriptedSocket(incoming))
                with self.assertRaises(expected):
                    candidate._receive_message(candidate._deadline())

    def test_bridge_request_events_errors_and_teardown_failures(self):
        transport = ScriptedSocket()
        bridge = obs.OBSBridge(
            transport=transport,
            request_id_factory=lambda: "wanted",
        )
        bridge._connected = True
        bridge._receive_message = mock.Mock(side_effect=[
            {"op": 5, "d": {"eventType": "SomethingHappened"}},
            {"op": 7, "d": {"requestId": "other", "requestStatus": {"result": True}}},
            {"op": 7, "d": {
                "requestId": "wanted",
                "requestStatus": {"result": True},
                "responseData": {"ok": True},
            }},
        ])
        self.assertEqual(bridge.request(" GetStatus ", {"verbose": True}), {"ok": True})
        self.assertEqual(len(bridge._events), 1)
        request = json.loads(obs.decode_frame(transport.sent[0])[2])
        self.assertEqual(request["d"]["requestType"], "GetStatus")

        for args in (("",), ("bad\nname",)):
            with self.assertRaises(ValueError):
                bridge.request(*args)
        with self.assertRaises(ValueError):
            bridge.request("GetStatus", [])
        bridge._request_id_factory = lambda: ""
        with self.assertRaises(ValueError):
            bridge.request("GetStatus")

        for response in (
            [],
            {"op": 3, "d": {}},
            {"op": 7, "d": {"requestId": "wanted", "requestStatus": {"result": False, "code": 402, "comment": "denied"}}},
            {"op": 7, "d": {"requestId": "wanted", "requestStatus": {"result": False}}},
        ):
            bridge._request_id_factory = lambda: "wanted"
            bridge._receive_message = mock.Mock(return_value=response)
            with self.assertRaises((obs.OBSProtocolError, obs.OBSRequestError)):
                bridge.request("GetStatus")

        bridge._receive_message = mock.Mock(side_effect=socket.timeout("late"))
        with self.assertRaises(obs.OBSTimeoutError):
            bridge.request("GetStatus")
        bridge._receive_message = mock.Mock(side_effect=OSError("gone"))
        with self.assertRaises(obs.OBSUnavailableError):
            bridge.request("GetStatus")

        class CloseErrorSocket(ScriptedSocket):
            def shutdown(self, how):
                del how
                raise OSError("already closed")

            def close(self):
                raise ValueError("already closed")

        bridge.close()
        bridge._socket = CloseErrorSocket()
        bridge.close()

    def test_connection_failures_and_protocol_rejections_close_transport(self):
        with self.assertRaises(obs.OBSTimeoutError):
            obs.OBSBridge(socket_factory=mock.Mock(side_effect=socket.timeout("late"))).connect()
        with self.assertRaises(obs.OBSUnavailableError):
            obs.OBSBridge(socket_factory=mock.Mock(side_effect=OSError("refused"))).connect()

        bad_handshakes = (
            b"not an HTTP response\r\n\r\n",
            b"HTTP/1.1 400 Bad Request\r\n\r\n",
            b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: wrong\r\n\r\n",
        )
        for response in bad_handshakes:
            with self.subTest(response=response[:15]):
                transport = ScriptedSocket(response)
                with self.assertRaises(obs.OBSBridgeError):
                    obs.OBSBridge(transport=transport).connect()
                self.assertTrue(transport.closed)

        hello_cases = (
            ({"obsWebSocketVersion": "4.9"}, None, obs.OBSProtocolError),
            ({"obsWebSocketVersion": "5.0", "authentication": "bad"}, "secret", obs.OBSProtocolError),
            ({"obsWebSocketVersion": "5.0", "authentication": {"salt": ""}}, "secret", obs.OBSProtocolError),
        )
        for hello_data, password, expected in hello_cases:
            with self.subTest(hello_data=hello_data):
                transport = _scripted_transport(hello_data=hello_data)
                bridge = obs.OBSBridge(transport=transport, password=password)
                with self.assertRaises(expected):
                    bridge.connect()
                self.assertTrue(transport.closed)

        for message in (
            {"op": 1, "d": {}},
            {"op": 5, "d": {}},
            {"op": 4, "d": {}},
        ):
            with self.subTest(message=message):
                transport = _scripted_transport(message)
                bridge = obs.OBSBridge(transport=transport)
                with self.assertRaises(obs.OBSProtocolError):
                    bridge.connect()
                self.assertTrue(transport.closed)

    def test_low_level_transport_and_helper_alias_paths(self):
        class SetTimeoutErrorSocket(ScriptedSocket):
            def settimeout(self, value):
                del value
                raise OSError("not supported")

        bridge = obs.OBSBridge(transport=SetTimeoutErrorSocket())
        with self.assertRaises(obs.OBSUnavailableError):
            bridge._set_timeout(bridge._deadline())
        with self.assertRaises(obs.OBSTimeoutError):
            obs.OBSBridge(transport=ScriptedSocket(), clock=lambda: 10)._set_timeout(0)
        with self.assertRaises(obs.OBSUnavailableError):
            obs.OBSBridge(transport=None)._fill(1)
        with self.assertRaises(obs.OBSProtocolError):
            obs.OBSBridge(transport=ScriptedSocket())._read_exact(-1, 1)

        class ReceiveErrorSocket(ScriptedSocket):
            def recv(self, size):
                del size
                raise OSError("broken")

        with self.assertRaises(obs.OBSUnavailableError):
            obs.OBSBridge(transport=ReceiveErrorSocket(), clock=lambda: 0)._fill(1)
        with self.assertRaises(obs.OBSUnavailableError):
            obs.OBSBridge(transport=ScriptedSocket(), clock=lambda: 0)._fill(1)

        self.assertEqual(obs._host_header("::1", 4455), "[::1]:4455")
        self.assertEqual(obs._first_setting({"a": "", "b": 2}, "a", "b"), 2)
        self.assertEqual(obs._first_setting({}, "a", default=3), 3)
        for value in ("/tmp/a.mp4", {"filename": "/tmp/b.mp4"}, {"path": " /tmp/c.mp4 "}):
            self.assertTrue(obs._saved_replay_path(value))
        self.assertEqual(obs._saved_replay_path(None), "")
        self.assertIsNone(obs._saved_replay_event(None))
        self.assertIsNone(obs._saved_replay_event({"op": 5, "d": {"eventType": "Other"}}))
        self.assertEqual(
            obs._saved_replay_event({"op": 5, "d": {"eventType": "ReplayBufferSaved", "eventData": {"path": "/tmp/x"}}}),
            {"savedFilename": "/tmp/x"},
        )
        self.assertFalse(obs.replay_enabled(None))
        self.assertTrue(obs.replay_enabled({"obs_replay_enabled": " YES "}))
        self.assertTrue(obs.replay_enabled({"obs_replay_enabled": 1}))
        self.assertFalse(obs.replay_enabled({"obs_replay_enabled": 0}))

    def test_optional_helpers_cover_owned_and_alias_operations(self):
        class HelperBridge:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def save_replay_buffer(self):
                return {"savedFilename": "clip.mp4"}

            def set_record_directory(self, directory):
                return {"recordDirectory": directory}

            def connect(self):
                return self

            def stop_replay_buffer(self):
                return {"stopped": True}

        client = HelperBridge()
        self.assertEqual(obs.save_replay_buffer({"obs_replay_enabled": True}, bridge=client), {"savedFilename": "clip.mp4"})
        self.assertEqual(obs.save_replay({"obs_replay_enabled": True}, bridge=client), {"savedFilename": "clip.mp4"})
        self.assertEqual(obs.disarm_replay_buffer({"obs_replay_enabled": True}, bridge=client), {"stopped": True})
        self.assertEqual(obs.set_record_directory({"obs_replay_enabled": True}, "/tmp/record", bridge=client), {"recordDirectory": "/tmp/record"})
        self.assertTrue(obs.obs_available({"obs_replay_enabled": True}, bridge=client))
        self.assertIsNone(obs.set_record_directory({"obs_replay_enabled": False}, "/tmp/record", bridge=client))
        self.assertIsNone(obs.set_record_directory(None, "/tmp/record"))

        owned = HelperBridge()
        with mock.patch.object(obs.OBSBridge, "from_settings", return_value=owned):
            self.assertEqual(obs.save_replay_buffer({"obs_replay_enabled": True}), {"savedFilename": "clip.mp4"})
        self.assertTrue(owned.closed)

    def test_authentication_digest_matches_v5_formula(self):
        actual = obs.build_authentication("password", "salt", "challenge")
        secret = base64.b64encode(hashlib.sha256(b"passwordsalt").digest())
        expected = base64.b64encode(hashlib.sha256(secret + b"challenge").digest()).decode()
        self.assertEqual(actual, expected)

    def test_hello_identify_and_save_replay_request_are_framed_and_masked(self):
        transport = _scripted_transport(
            {"op": 2, "d": {"negotiatedRpcVersion": 1}},
            {"op": 7, "d": {
                "requestType": "SaveReplayBuffer",
                "requestId": "req-1",
                "requestStatus": {"result": True, "code": 100},
                "responseData": {"savedFilename": "/tmp/clip.mp4"},
            }},
            password=True,
        )
        bridge = obs.OBSBridge(
            password="secret-password",
            timeout=2,
            transport=transport,
            key_factory=lambda: "dGhlIHNhbXBsZSBub25jZQ==",
            request_id_factory=lambda: "req-1",
        )

        result = bridge.save_replay_buffer()

        self.assertEqual(result, {"savedFilename": "/tmp/clip.mp4"})
        self.assertTrue(bridge.connected)
        self.assertGreaterEqual(len(transport.sent), 3)
        self.assertTrue(transport.sent[0].startswith(b"GET / HTTP/1.1\r\n"))
        self.assertIn(b"Sec-WebSocket-Protocol: obswebsocket.json", transport.sent[0])
        identify = json.loads(obs.decode_frame(transport.sent[1])[2])
        self.assertEqual(identify, {
            "op": 1,
            "d": {
                "rpcVersion": 1,
                "authentication": obs.build_authentication("secret-password", "salt", "challenge"),
            },
        })
        request = json.loads(obs.decode_frame(transport.sent[2])[2])
        self.assertEqual(request["op"], 6)
        self.assertEqual(request["d"], {"requestType": "SaveReplayBuffer", "requestId": "req-1"})
        self.assertNotIn(b"secret-password", transport.sent[1])
        self.assertTrue(all(value <= 2 for value in transport.timeouts))
        bridge.close()
        self.assertTrue(transport.closed)

    def test_save_replay_waits_for_replay_buffer_saved_event(self):
        transport = _scripted_transport(
            {"op": 2, "d": {}},
            {"op": 7, "d": {
                "requestType": "SaveReplayBuffer",
                "requestId": "req-event",
                "requestStatus": {"result": True, "code": 100},
                "responseData": {},
            }},
            {"op": 5, "d": {
                "eventType": "ReplayBufferSaved",
                "eventData": {"savedReplayPath": "/tmp/event-clip.mp4"},
            }},
        )
        bridge = obs.OBSBridge(
            transport=transport,
            request_id_factory=lambda: "req-event",
            key_factory=lambda: "dGhlIHNhbXBsZSBub25jZQ==",
        )
        self.assertEqual(bridge.save_replay_buffer(), {"savedFilename": "/tmp/event-clip.mp4"})
        bridge.close()

    def test_arm_does_not_take_ownership_of_an_already_active_buffer(self):
        class ActiveBridge:
            def get_replay_buffer_status(self):
                return {"outputActive": True}

            def start_replay_buffer(self):
                raise AssertionError("active replay buffer must not be started twice")

        result = obs.arm_replay_buffer(
            {"obs_replay_enabled": True}, bridge=ActiveBridge()
        )
        self.assertEqual(result["armed"], True)
        self.assertEqual(result["owned"], False)

    def test_arm_starts_and_marks_an_inactive_buffer_as_owned(self):
        class InactiveBridge:
            def get_replay_buffer_status(self):
                return {"outputActive": False}

            def start_replay_buffer(self):
                return {}

        result = obs.arm_replay_buffer(
            {"obs_replay_enabled": True}, bridge=InactiveBridge()
        )
        self.assertEqual(result["armed"], True)
        self.assertEqual(result["owned"], True)

    def test_identify_without_authentication_and_record_directory_request(self):
        transport = _scripted_transport(
            {"op": 2, "d": {}},
            {"op": 7, "d": {
                "requestType": "SetRecordDirectory",
                "requestId": "req-2",
                "requestStatus": {"result": True, "code": 100},
            }},
        )
        bridge = obs.OBSBridge(
            transport=transport,
            request_id_factory=lambda: "req-2",
            key_factory=lambda: "dGhlIHNhbXBsZSBub25jZQ==",
        )
        self.assertEqual(bridge.set_record_directory("~/Videos"), {})
        identify = json.loads(obs.decode_frame(transport.sent[1])[2])
        self.assertEqual(identify, {"op": 1, "d": {"rpcVersion": 1}})
        request = json.loads(obs.decode_frame(transport.sent[2])[2])
        self.assertEqual(request["d"]["requestData"]["recordDirectory"], str(Path("~/Videos").expanduser()))
        bridge.close()

    def test_auth_required_without_password_fails_closed(self):
        transport = _scripted_transport({"op": 2, "d": {}}, password=True)
        bridge = obs.OBSBridge(transport=transport, key_factory=lambda: "dGhlIHNhbXBsZSBub25jZQ==")
        with self.assertRaises(obs.OBSAuthenticationError):
            bridge.connect()
        self.assertFalse(bridge.connected)
        self.assertTrue(transport.closed)
        self.assertEqual(len(transport.sent), 1)  # HTTP upgrade only; no unauthenticated identify.

    def test_timeout_is_bounded_and_converted(self):
        transport = TimeoutSocket()
        bridge = obs.OBSBridge(transport=transport, timeout=999)
        with self.assertRaises(obs.OBSTimeoutError):
            bridge.connect()
        self.assertFalse(bridge.connected)
        self.assertTrue(transport.closed)

    def test_frame_length_and_mask_round_trip(self):
        frame = obs.encode_frame("x" * 126, mask=True, mask_key=b"abcd")
        fin, opcode, payload = obs.decode_frame(frame)
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        self.assertEqual(payload, b"x" * 126)
        self.assertEqual(frame[1] & 0x80, 0x80)

    def test_setting_off_never_constructs_or_opens_socket(self):
        factory = mock.Mock(side_effect=AssertionError("socket opened while disabled"))
        self.assertIsNone(obs.save_replay_buffer({"obs_replay_enabled": False}, socket_factory=factory))
        self.assertFalse(obs.obs_available({"obs_replay_enabled": False}, socket_factory=factory))
        factory.assert_not_called()

    def test_optional_helpers_swallow_unavailable_obs(self):
        factory = mock.Mock(side_effect=ConnectionRefusedError("no OBS"))
        settings = {"obs_replay_enabled": True, "obs_timeout": 0.1}
        self.assertIsNone(obs.save_replay_buffer(settings, socket_factory=factory))
        self.assertFalse(obs.obs_available(settings, socket_factory=factory))


if __name__ == "__main__":
    unittest.main()
