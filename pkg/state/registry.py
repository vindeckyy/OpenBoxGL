"""Process registry — single source of truth for session globals.

Consolidates RUNNING, PROCESSES, SESSION_EVENTS, PROCESS_LOCK, and
EVENT_SEQUENCE that were previously scattered across launch.py, sse.py,
and cache.py.
"""

import dataclasses
import threading


@dataclasses.dataclass
class Session:
    """Typed view of one entry in the RUNNING dict."""

    launch_id: str
    stable_game_id: str
    pid: int
    pgid: int
    proc_start_time: float
    cmd_fingerprint: str
    game_name: str


# Maps launch_id → session info dict (see _make_start_mutator / reattach_session).
RUNNING: dict[str, dict] = {}

# Maps launch_id → subprocess.Popen (or _ReattachedProcess).
PROCESSES: dict[str, object] = {}

# Bounded list of session events; kept to the last 100 entries.
SESSION_EVENTS: list[dict] = []

# Lock guarding mutations to RUNNING, PROCESSES, and SESSION_EVENTS.
PROCESS_LOCK = threading.Lock()

# Monotonically increasing event sequence counter.
EVENT_SEQUENCE: int = 0
