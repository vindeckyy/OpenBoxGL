"""Per-game story — a deterministic narrative timeline for one library game (1.12.0).

Pure read projection: events are derived from the game record (``added_at``,
progress, play stats), the session journal (``state["history"]``), and the
game's Moments. Nothing is written; the story is always consistent with the
journal and can never go stale.

Event kinds: ``added``, ``first_played``, ``session`` (longest only),
``milestone`` (playtime marks crossed), ``progress``, ``moment``. Each event
is ``{kind, at, title, detail}`` sorted by timestamp then declaration order.
"""

from __future__ import annotations

from datetime import datetime

MILESTONE_SECONDS = (3600, 5 * 3600, 10 * 3600, 25 * 3600, 50 * 3600, 100 * 3600)
_MILESTONE_LABELS = {s: f"{s // 3600}h played" for s in MILESTONE_SECONDS}
_PROGRESS_ORDER = ("Playing", "Paused", "Beaten", "Completed", "Mastered")


def _stamp(value):
    """Parse an ISO timestamp to ``(epoch, raw)``; unparseable sorts first."""
    text = str(value or "")
    try:
        return (int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()), text)
    except (TypeError, ValueError, OverflowError, OSError):
        return (0, text)


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _hours(seconds):
    hours = seconds / 3600.0
    return f"{hours:.1f}h" if hours < 10 else f"{round(hours)}h"


def game_story(state, game):
    """Build the story payload for one game record within *state*.

    ``game`` is the canonical record (with ``game_id``); history rows match on
    ``game_id`` first and fall back to name for legacy rows without one.
    """
    game = game if isinstance(game, dict) else {}
    game_id = str(game.get("game_id") or "")
    name = str(game.get("name") or "Untitled")
    events = []

    added_at = str(game.get("added_at") or "")
    if added_at:
        events.append({"kind": "added", "at": added_at, "title": "Added to library",
                       "detail": str(game.get("source") or "")})

    sessions = []
    history = state.get("history", []) if isinstance(state, dict) else []
    for row in history if isinstance(history, list) else []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("game_id") or "")
        # A row carrying a game_id belongs to that keyed record only — even
        # when this game has no id of its own, a name match must not claim
        # another game's sessions.
        if row_id and row_id != game_id:
            continue
        if not row_id and str(row.get("game") or "") != name:
            continue
        sessions.append(row)
    sessions.sort(key=lambda row: _stamp(row.get("started")))

    if sessions:
        first = sessions[0]
        events.append({"kind": "first_played", "at": str(first.get("started") or ""),
                       "title": "First played",
                       "detail": _hours(_num(first.get("seconds"))) if _num(first.get("seconds")) else ""})
        longest = max(sessions, key=lambda row: _num(row.get("seconds")))
        if _num(longest.get("seconds")) > 0:
            events.append({"kind": "session", "at": str(longest.get("started") or ""),
                           "title": "Longest session",
                           "detail": _hours(_num(longest.get("seconds")))})

    playtime = _num(game.get("playtime_seconds"))
    # Milestones are point-in-time achievements; date them by the session that
    # crossed the mark when determinable, else by last_played.
    if playtime > 0:
        cumulative = 0.0
        crossed = set()
        for row in sessions:
            cumulative += _num(row.get("seconds"))
            for mark in MILESTONE_SECONDS:
                if mark not in crossed and cumulative >= mark:
                    crossed.add(mark)
                    events.append({"kind": "milestone", "at": str(row.get("started") or ""),
                                   "title": _MILESTONE_LABELS[mark],
                                   "detail": ""})
        # Playtime recorded outside the journal (imports, stat edits) still
        # earns its milestones, dated at last_played.
        for mark in MILESTONE_SECONDS:
            if mark not in crossed and playtime >= mark:
                events.append({"kind": "milestone", "at": str(game.get("last_played") or ""),
                               "title": _MILESTONE_LABELS[mark], "detail": ""})

    progress = str(game.get("progress") or "")
    if progress in _PROGRESS_ORDER[2:]:
        events.append({"kind": "progress", "at": str(game.get("last_played") or ""),
                       "title": progress, "detail": ""})

    for item in game.get("moments") or []:
        if not isinstance(item, dict):
            continue
        events.append({"kind": "moment", "at": str(item.get("created_at") or ""),
                       "title": str(item.get("title") or "Moment"),
                       "detail": str(item.get("note") or "")[:200]})

    order = {"added": 0, "first_played": 1, "session": 2, "milestone": 3, "progress": 4, "moment": 5}
    events.sort(key=lambda ev: (_stamp(ev.get("at")), order.get(ev.get("kind"), 9)))

    return {
        "game_id": game_id,
        "name": name,
        "platform": str(game.get("platform") or ""),
        "events": events,
        "totals": {
            "sessions": len(sessions),
            "playtime_seconds": int(playtime),
            "moments": sum(1 for ev in events if ev["kind"] == "moment"),
            "progress": progress,
        },
    }
