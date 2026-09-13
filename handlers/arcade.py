"""Arcade Room kiosk boundary routes (T5).

The kiosk PIN is a convenience boundary for Museum mode, not an account or
security system. Only a salted PBKDF2 digest is stored in local settings; the
digest is never included in public settings responses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from api_errors import BadRequest
from routes.registry import route
from webapp_state import load_state_view, transact_state

PIN_ITERATIONS = 120_000
PIN_SALT_BYTES = 16
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 12


def _pin(value):
    if not isinstance(value, str) or not PIN_MIN_LENGTH <= len(value) <= PIN_MAX_LENGTH or not value.isdigit():
        raise BadRequest(
            f"Museum PIN must be {PIN_MIN_LENGTH}-{PIN_MAX_LENGTH} digits.",
            code="ARCADE_INVALID_PIN",
        )
    return value


def _b64(value):
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value):
    return base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))


def hash_kiosk_pin(pin, *, salt=None, iterations=PIN_ITERATIONS):
    """Return a versioned, salted PBKDF2 digest for a validated PIN."""
    pin = _pin(pin)
    salt = salt or secrets.token_bytes(PIN_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, int(iterations))
    return f"pbkdf2-sha256${int(iterations)}${_b64(salt)}${_b64(digest)}"


def verify_kiosk_pin(stored, pin):
    """Verify a stored digest without raising for malformed persisted data."""
    if not isinstance(stored, str) or not isinstance(pin, str) or not pin.isdigit():
        return False
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        return False
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = stored.split("$", 3)
        iterations = int(raw_iterations)
        if algorithm != "pbkdf2-sha256" or not 50_000 <= iterations <= 500_000:
            return False
        salt = _unb64(raw_salt)
        expected = _unb64(raw_digest)
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError, base64.binascii.Error):
        return False


def _status(state):
    settings = state.get("settings", {}) if isinstance(state, dict) else {}
    return {
        "enabled": bool(settings.get("museum_kiosk_enabled", False)),
        "pin_set": bool(settings.get("museum_kiosk_pin_hash")),
    }


@route("GET", "/api/v2/arcade/kiosk/status", spec="handlers.arcade.kiosk_status")
def kiosk_status(handler, parsed):
    handler.send_json(200, _status(load_state_view()))


@route("POST", "/api/v2/arcade/kiosk/pin", spec="handlers.arcade.kiosk_pin")
def kiosk_pin(handler, payload):
    if not isinstance(payload, dict):
        raise BadRequest("Museum kiosk request must be an object.", code="ARCADE_INVALID_REQUEST")
    supplied = payload.get("pin")
    clear = bool(payload.get("clear", False))
    if supplied not in (None, "") and clear:
        raise BadRequest("Choose a PIN or clear the existing PIN, not both.", code="ARCADE_INVALID_REQUEST")
    pin = _pin(supplied) if supplied not in (None, "") else None
    enabled = payload.get("enabled")

    def mutate(state):
        settings = state.setdefault("settings", {})
        if pin is not None:
            settings["museum_kiosk_pin_hash"] = hash_kiosk_pin(pin)
        elif clear:
            settings.pop("museum_kiosk_pin_hash", None)
        if isinstance(enabled, bool):
            settings["museum_kiosk_enabled"] = enabled
        return _status(state)

    _state, result = transact_state(mutate)
    handler.send_json(200, result)


@route("POST", "/api/v2/arcade/kiosk/verify", spec="handlers.arcade.kiosk_verify")
def kiosk_verify(handler, payload):
    if not isinstance(payload, dict):
        raise BadRequest("Museum kiosk request must be an object.", code="ARCADE_INVALID_REQUEST")
    pin = payload.get("pin")
    if not isinstance(pin, str) or len(pin) > PIN_MAX_LENGTH:
        raise BadRequest("Museum PIN must be text.", code="ARCADE_INVALID_PIN")
    settings = load_state_view().get("settings", {})
    handler.send_json(200, {"ok": verify_kiosk_pin(settings.get("museum_kiosk_pin_hash", ""), pin)})


__all__ = ["hash_kiosk_pin", "verify_kiosk_pin", "kiosk_status", "kiosk_pin", "kiosk_verify"]
