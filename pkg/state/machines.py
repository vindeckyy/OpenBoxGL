"""Declared state machines for every lifecycle the app runs.

One table per entity: legal states, terminal states, allowed transitions.
Standard library only — the operation service, tests, and tooling all read it.

Machines are catalogued even when their state still lives in scattered flags
(DOM overlays, native hosts); ``storage`` says where the truth is and
``declared`` in the docstring of each entry notes read-only cataloguing.
Transitions use ``None`` for "no record yet" (create) and for "record goes
away" (spawn-new-record or delete).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Machine:
    name: str
    states: frozenset
    terminal: frozenset
    transitions: dict
    storage: str
    persisted: bool
    exposed_via: str
    active: frozenset | None = None
    derived: frozenset = field(default_factory=frozenset)
    # ``derived`` = literals the API projection may expose that the app never
    # persists (it can overlap ``states`` where the value is also a legal
    # field literal, e.g. setup_preview's projected "interrupted").

    def allows(self, action: str, from_state: str | None, to_state: str | None) -> bool:
        """True when ``action`` may move ``from_state`` to ``to_state``."""
        return (from_state, to_state) in self.transitions.get(action, frozenset())

    def targets(self, action: str, from_state: str | None) -> frozenset:
        """Every state ``action`` can move ``from_state`` to (None = new/removed)."""
        return frozenset(
            to for source, to in self.transitions.get(action, frozenset()) if source == from_state
        )


_OPERATION_STATES = (
    "queued",
    "running",
    "cancelling",
    "done",
    "partial",
    "error",
    "cancelled",
    "interrupted",
)
_OPERATION_ACTIVE = frozenset({"queued", "running", "cancelling"})
_OPERATION_TERMINAL = frozenset({"done", "partial", "error", "cancelled", "interrupted"})

_LEGACY_STATES = ("queued", "running", "done", "error", "cancelled")
_LEGACY_TERMINAL = frozenset({"done", "error", "cancelled"})

_FACADE_STATES = (
    "idle",
    "installing",
    "updating",
    "downloading",
    "running",
    "done",
    "partial",
    "error",
)
_FACADE_TERMINAL = frozenset({"done", "partial", "error"})
_FACADE_ACTIVE = frozenset({"installing", "updating", "downloading", "running"})

_SESSION_STATES = ("active", "stopping", "abandoned")

_SETUP_PREVIEW_STATES = ("queued", "running", "cancelling", "ready", "error", "interrupted")
_MATCH_PREVIEW_STATES = ("queued", "running", "cancelling", "ready", "expired", "error")
_PREVIEW_TERMINAL = frozenset({"ready", "error"})
_MATCH_PREVIEW_TERMINAL = frozenset({"ready", "expired", "error"})

_STOREFRONT_STATES = ("pending", "running", "skipped", "ok", "timeout", "error")
_STOREFRONT_TERMINAL = frozenset({"skipped", "ok", "timeout", "error"})

_MEDIA_WORKER_STATES = ("stopped", "running", "paused", "cancelled")

_PROGRESS_STATES = ("", "Unplayed", "Playing", "Paused", "Beaten", "Completed", "Mastered", "Abandoned")

_CHALLENGE_STATES = ("open", "active", "closed", "cancelled", "completed")

_GAMEYFIN_STATES = ("idle", "installing", "done", "error")

_WIZARD_STEPS = tuple(str(step) for step in range(1, 9))
_PARTY_STATES = ("setup", "spinning", "spun", "winner")
_PICKER_STATES = ("idle", "spinning", "result", "empty")
_OVERLAY_LAYERS = (
    "bigbox",
    "menu",
    "pause",
    "screensaver",
    "party",
    "startup_video",
    "backdrop",
)
_REPAIR_STATES = ("scan", "preview", "applied")


def _everything(states) -> frozenset:
    return frozenset((source, target) for source in states for target in states)


MACHINES: dict[str, Machine] = {
    # Authoritative, durable operation record (ADR 0011). The only adopted
    # machine whose transitions are guarded in production code today.
    "operation": Machine(
        name="operation",
        states=frozenset(_OPERATION_STATES),
        terminal=_OPERATION_TERMINAL,
        transitions={
            "submit": frozenset({(None, "queued")}),
            "start": frozenset({("queued", "running"), ("running", "running")}),
            "progress": frozenset((state, state) for state in _OPERATION_ACTIVE),
            "cancel": frozenset((state, "cancelling") for state in _OPERATION_ACTIVE),
            "finish": frozenset(
                (source, target)
                for source in _OPERATION_STATES
                for target in _OPERATION_TERMINAL
            ),
            "interrupt": frozenset((state, "interrupted") for state in _OPERATION_ACTIVE),
            "retry": frozenset((state, None) for state in _OPERATION_TERMINAL),
            "resume": frozenset({("interrupted", None)}),
        },
        storage="operations.json operations[].state",
        persisted=True,
        exposed_via="/api/v2/jobs, /api/jobs, job.* SSE",
        active=_OPERATION_ACTIVE,
    ),
    # Process-memory mirror of the job the public jobs list used to expose.
    # Public /api/jobs and /api/v1/jobs return 410. The live surface is /api/v2/jobs.
    "legacy_job": Machine(
        name="legacy_job",
        states=frozenset(_LEGACY_STATES),
        terminal=_LEGACY_TERMINAL,
        transitions={
            "submit": frozenset({(None, "queued")}),
            "start": frozenset({("queued", "running")}),
            "finish": frozenset({("running", "done"), ("running", "error")}),
            "cancel": frozenset((state, "cancelled") for state in ("queued", "running")),
            "supersede": frozenset((state, "cancelled") for state in ("queued", "running")),
            "interrupt": frozenset((state, "cancelled") for state in ("queued", "running")),
        },
        storage="job_manager.JobManager._jobs[name]['state']",
        persisted=False,
        exposed_via="/api/v2/jobs",
        active=frozenset({"queued", "running"}),
    ),
    # Legacy per-feature status dicts (INSTALLS, METADATA_JOB, MATCH_JOB, MEDIA_JOB).
    "job_facade": Machine(
        name="job_facade",
        states=frozenset(_FACADE_STATES),
        terminal=_FACADE_TERMINAL,
        transitions={
            "set": frozenset((None, state) for state in _FACADE_STATES if state != "idle"),
            "reset": frozenset((state, None) for state in _FACADE_STATES),
        },
        storage="webapp_state facades; idle is a read-time default and never stored",
        persisted=False,
        exposed_via="/api/emulators, /api/metadata/status, /api/media/*",
        active=_FACADE_ACTIVE,
    ),
    "session": Machine(
        name="session",
        states=frozenset(_SESSION_STATES),
        terminal=frozenset({"abandoned"}),
        transitions={
            "start": frozenset({(None, "active")}),
            "stop": frozenset({("active", "stopping")}),
            "abandon": frozenset({("active", "abandoned"), ("stopping", "abandoned")}),
            "remove": frozenset((state, None) for state in _SESSION_STATES),
        },
        storage="library.json active_sessions[].status (sessions.json crash mirror)",
        persisted=True,
        exposed_via="/api/running, /api/sessions, session.* SSE",
    ),
    "setup_preview": Machine(
        name="setup_preview",
        states=frozenset(_SETUP_PREVIEW_STATES),
        terminal=_PREVIEW_TERMINAL,
        transitions={
            "create": frozenset({(None, "ready")}),
            "queue": frozenset({("ready", "queued")}),
            "start": frozenset({("queued", "running")}),
            "complete": frozenset({("running", "ready")}),
            "fail": frozenset({("running", "error")}),
            "interrupt": frozenset({("queued", "interrupted"), ("running", "interrupted")}),
        },
        storage="previews/<preview_id>.json state",
        persisted=True,
        exposed_via="/api/v2/setup/preview",
        derived=frozenset({"interrupted"}),
    ),
    "match_preview": Machine(
        name="match_preview",
        states=frozenset(_MATCH_PREVIEW_STATES),
        terminal=_MATCH_PREVIEW_TERMINAL,
        transitions={
            "create": frozenset({(None, "queued")}),
            "default": frozenset({(None, "ready")}),
            "start": frozenset({("queued", "running")}),
            "complete": frozenset({("running", "ready")}),
            "expire": frozenset({("ready", "expired")}),
            "fail": frozenset({("running", "error")}),
        },
        storage="match_previews/<preview_id>.json state",
        persisted=True,
        exposed_via="/api/v2/metadata/matches/preview",
        derived=frozenset({"expired"}),
    ),
    "storefront_source": Machine(
        name="storefront_source",
        states=frozenset(_STOREFRONT_STATES),
        terminal=_STOREFRONT_TERMINAL,
        transitions={
            "pending": frozenset({(None, "pending")}),
            "start": frozenset({("pending", "running")}),
            "skip": frozenset({("pending", "skipped"), ("running", "skipped")}),
            "finish": frozenset({("running", "ok")}),
            "timeout": frozenset({("running", "timeout")}),
            "fail": frozenset({("running", "error")}),
        },
        storage="storefront-status.json sources[].status",
        persisted=True,
        exposed_via="/api/v2/storefront/status",
    ),
    "media_queue_worker": Machine(
        name="media_queue_worker",
        states=frozenset(_MEDIA_WORKER_STATES),
        terminal=frozenset({"stopped", "cancelled"}),
        transitions={
            "start": frozenset(
                {
                    (None, "running"),
                    ("stopped", "running"),
                    ("cancelled", "running"),
                    ("paused", "running"),
                }
            ),
            "pause": frozenset({("running", "paused")}),
            "resume": frozenset({("paused", "running")}),
            "cancel": frozenset(
                {("running", "cancelled"), ("paused", "cancelled"), ("stopped", "cancelled")}
            ),
            "stop": frozenset({("running", "stopped")}),
        },
        storage="pkg/state/imports.py _MEDIA_WORKER state derived from events/thread",
        persisted=False,
        exposed_via="/api/media/queue",
        active=frozenset({"running", "paused"}),
    ),
    "media_queue_item": Machine(
        name="media_queue_item",
        states=frozenset({"queued", "failed"}),
        terminal=frozenset({"failed"}),
        transitions={
            "queue": frozenset({(None, "queued")}),
            "fail": frozenset({("queued", "failed")}),
            "retry": frozenset({("failed", "queued")}),
        },
        storage="media-queue.json items[].state",
        persisted=True,
        exposed_via="/api/media/queue",
    ),
    "game_progress": Machine(
        name="game_progress",
        states=frozenset(_PROGRESS_STATES),
        terminal=frozenset(),
        transitions={"set": _everything(_PROGRESS_STATES)},
        storage="library.json games[].progress",
        persisted=True,
        exposed_via="/api/v2/library/progress/set, /api/game",
    ),
    "household_challenge": Machine(
        name="household_challenge",
        states=frozenset(_CHALLENGE_STATES),
        terminal=frozenset({"closed", "cancelled", "completed"}),
        transitions={
            "create": frozenset({(None, "open")}),
            "activate": frozenset({("open", "active")}),
            "complete": frozenset({("open", "completed"), ("active", "completed")}),
            "close": frozenset({("open", "closed"), ("active", "closed")}),
            "cancel": frozenset({("open", "cancelled"), ("active", "cancelled")}),
            "merge": _everything(_CHALLENGE_STATES),
        },
        storage="library.json household.records[].payload.status",
        persisted=True,
        exposed_via="/api/v2/household/*",
        derived=frozenset({"complete", "expired"}),
    ),
    "gameyfin_install": Machine(
        name="gameyfin_install",
        states=frozenset(_GAMEYFIN_STATES),
        terminal=frozenset({"done", "error"}),
        transitions={
            "start": frozenset({(None, "installing")}),
            "complete": frozenset({("installing", "done")}),
            "fail": frozenset({("installing", "error")}),
        },
        storage="webapp_state INSTALLS['gameyfin:<id>']['state']",
        persisted=False,
        exposed_via="/api/gameyfin/install/status",
        active=frozenset({"installing"}),
    ),
    # Declared-only frontend machines: their state lives in DOM flags and
    # module variables, catalogued here so the inventory is complete.
    "setup_wizard": Machine(
        name="setup_wizard",
        states=frozenset(_WIZARD_STEPS),
        terminal=frozenset({"8"}),
        transitions={"goto": _everything(_WIZARD_STEPS)},
        storage="static/setup.js state.step + state.advanced (DOM)",
        persisted=False,
        exposed_via="setup wizard UI",
    ),
    "party_wheel": Machine(
        name="party_wheel",
        states=frozenset(_PARTY_STATES),
        terminal=frozenset(),
        transitions={
            "build": frozenset({(None, "setup")}),
            "spin": frozenset({("setup", "spinning"), ("spun", "spinning")}),
            "settle": frozenset({("spinning", "spun")}),
            "win": frozenset({("spun", "winner")}),
            "next": frozenset({("winner", "spinning")}),
            "reset": frozenset((state, "setup") for state in _PARTY_STATES),
        },
        storage="static/party.js queue/spinning/spun (DOM)",
        persisted=False,
        exposed_via="party overlay UI",
    ),
    "picker_phase": Machine(
        name="picker_phase",
        states=frozenset(_PICKER_STATES),
        terminal=frozenset({"result", "empty"}),
        transitions={
            "open": frozenset({(None, "idle")}),
            "pick": frozenset({("idle", "spinning")}),
            "result": frozenset({("spinning", "result")}),
            "empty": frozenset({("spinning", "empty")}),
            "close": frozenset((state, "idle") for state in _PICKER_STATES),
        },
        storage="static/picker.js #pickerSpinning/#pickerResult hidden flags (DOM)",
        persisted=False,
        exposed_via="picker dialog",
    ),
    "overlay_stack": Machine(
        name="overlay_stack",
        states=frozenset(_OVERLAY_LAYERS),
        terminal=frozenset(),
        transitions={
            "show": frozenset((None, layer) for layer in _OVERLAY_LAYERS),
            "hide": frozenset((layer, None) for layer in _OVERLAY_LAYERS),
        },
        storage="static/bigbox.js per-layer hidden flags (DOM); attract = all hidden",
        persisted=False,
        exposed_via="Big Box UI",
    ),
    "repair_wizard": Machine(
        name="repair_wizard",
        states=frozenset(_REPAIR_STATES),
        terminal=frozenset({"applied"}),
        transitions={
            "scan": frozenset({(None, "scan")}),
            "preview": frozenset({("scan", "preview")}),
            "apply": frozenset({("preview", "applied")}),
            "reset": frozenset((state, "scan") for state in _REPAIR_STATES),
        },
        storage="static/library.js repair* variables (DOM)",
        persisted=False,
        exposed_via="library repair wizard",
    ),
}


FACADE_TO_OPERATION = {
    "installing": "running",
    "updating": "running",
    "downloading": "running",
    "running": "running",
    "done": "done",
    "partial": "partial",
    "error": "error",
}


def job_result_state(state: str | None) -> str:
    """Project an operation state onto a preview record's state.

    ``None`` (no linked job) and finished states read as ``ready`` so a
    preview whose job completed is usable; in-flight states pass through.
    """
    if not state:
        return "ready"
    if state in {"done", "partial", "error"}:
        return "ready"
    return state


SESSION_ACTIVE = "active"
SESSION_STOPPING = "stopping"
SESSION_ABANDONED = "abandoned"

__all__ = [
    "Machine",
    "MACHINES",
    "FACADE_TO_OPERATION",
    "job_result_state",
    "SESSION_ACTIVE",
    "SESSION_STOPPING",
    "SESSION_ABANDONED",
]
