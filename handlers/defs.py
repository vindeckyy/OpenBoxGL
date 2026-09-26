"""Emulator definition update routes (ADR 0049).

``GET  /api/v2/emulators/defs/status``  local definition state: what the
bundled set provides, what the channel installed, and which files are
user-owned.
``POST /api/v2/emulators/defs/update``  fetch, verify, and install the signed
community pack. A signature failure is reported as a security notification
rather than a routine error, because that is what it is.
``POST /api/v2/emulators/defs/rollback`` remove definitions this channel
installed, restoring the bundled set.

Additive v2 surface; the frozen v1 route table is untouched. Verification is
delegated to ``updates.verify_artifact`` -- this handler never reimplements
signature checking.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pkg.parity  # noqa: F401,E402  # installs the flat parity_* import finder
import notifications  # noqa: E402
import openbox  # noqa: E402
from parity_emulator_defs_update import (  # noqa: E402
    DefinitionError,
    SignatureError,
    check as _check,
    install as _install,
    rollback as _rollback,
    status as _status,
)
from routes.registry import route  # noqa: E402


def _data_parent():
    # Resolved per call so tests that rebind openbox.DATA see the new store.
    return openbox.DATA.parent


@route("GET", "/api/v2/emulators/defs/status", spec="handlers.defs._api_get_api_v2_emulators_defs_status")
def _api_get_api_v2_emulators_defs_status(handler, parsed):
    return _status(_data_parent())


@route("GET", "/api/v2/emulators/defs/update", spec="handlers.defs._api_get_api_v2_emulators_defs_update")
def _api_get_api_v2_emulators_defs_update(handler, parsed):
    """Report whether an update is available, without fetching the pack."""
    return _check(data_dir=_data_parent())


@route("POST", "/api/v2/emulators/defs/update", spec="handlers.defs._api_post_api_v2_emulators_defs_update")
def _api_post_api_v2_emulators_defs_update(handler, parsed):
    try:
        result = _install(data_dir=_data_parent())
    except SignatureError as exc:
        # A failed signature is a security event, so it earns a notification
        # rather than disappearing into a job log.
        notifications.add_notification(
            openbox.load_state(),
            kind="security",
            level="error",
            title="Emulator definition update rejected",
            body=(
                "A downloaded emulator definition pack failed signature verification "
                f"and was discarded. Nothing was changed. Details: {exc}"
            ),
        )
        handler.send_json(400, {"error": "signature_verification_failed", "detail": str(exc)})
        return
    except DefinitionError as exc:
        handler.send_json(400, {"error": "invalid_definition_pack", "detail": str(exc)})
        return
    handler.send_json(200, result)


@route("POST", "/api/v2/emulators/defs/rollback", spec="handlers.defs._api_post_api_v2_emulators_defs_rollback")
def _api_post_api_v2_emulators_defs_rollback(handler, parsed):
    handler.send_json(200, _rollback(_data_parent()))

