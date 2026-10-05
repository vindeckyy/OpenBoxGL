#!/usr/bin/env python3
"""S7 -- a deleted game's playtime was credited to the last game in the library.

`launch_game` computed ``game_index = games.index(game) if game in games else
-1`` and handed that to `finish_session` as ``fallback_index``. ``-1`` is a
valid Python index -- it means "the last element" -- so the ``except
(IndexError, TypeError, ValueError)`` guarding `_match_fallback_index` could
never fire. With the game deleted, the session carried no name and no path, so
the function's own sanity check (``if not name and not path``) passed
unconditionally and returned ``games[-1]``. The caller then executed
``game["playtime_seconds"] = before + seconds`` on the wrong game, and wrote a
history row naming it.

Trigger: launch a game, quit OpenBox while it runs, delete that game, restart.
`reconcile_sessions_on_startup` reattaches the live PID, `res_game` finds
nothing, and the hours land on whatever game happens to be last.

The invariant these tests pin: **if the game is gone, no game's playtime
moves.** Not "the right game" -- none.
"""

import copy
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webapp_state
from pkg.state.launch import _match_fallback_index, resolve_library_game


def _library():
    return {
        "games": [
            {"game_id": "a", "name": "Alpha", "path": "/roms/alpha.zip", "playtime_seconds": 100},
            {"game_id": "b", "name": "Beta", "path": "/roms/beta.zip", "playtime_seconds": 200},
            {"game_id": "c", "name": "Gamma", "path": "/roms/gamma.zip", "playtime_seconds": 300},
        ],
        "settings": {},
        "history": [],
        "active_sessions": [],
    }


class FallbackIndexTests(unittest.TestCase):
    """The chokepoint itself. Negative indices must never resolve."""

    def setUp(self):
        self.games = _library()["games"]

    def test_a_valid_index_still_resolves(self):
        self.assertEqual(_match_fallback_index(self.games, 1, "", "")["name"], "Beta")

    def test_negative_one_does_not_resolve_to_the_last_game(self):
        """The exact bug: -1 meant "not found" and meant games[-1]."""
        self.assertIsNone(
            _match_fallback_index(self.games, -1, "", ""),
            "-1 is a 'not found' sentinel, not a request for the last game",
        )

    def test_every_negative_index_is_refused_not_just_minus_one(self):
        for index in (-1, -2, -3, -99):
            with self.subTest(index=index):
                self.assertIsNone(_match_fallback_index(self.games, index, "", ""))

    def test_out_of_range_high_is_refused(self):
        self.assertIsNone(_match_fallback_index(self.games, 3, "", ""))
        self.assertIsNone(_match_fallback_index(self.games, 99, "", ""))

    def test_empty_library_resolves_nothing(self):
        self.assertIsNone(_match_fallback_index([], 0, "", ""))
        self.assertIsNone(_match_fallback_index([], -1, "", ""))

    def test_non_numeric_sentinels_are_refused(self):
        for value in ("", "abc", object(), [], {}):
            with self.subTest(value=repr(value)):
                self.assertIsNone(_match_fallback_index(self.games, value, "", ""))

    def test_a_bool_is_refused_not_treated_as_an_index(self):
        """``int(True) == 1`` is true in Python, so a bool would pick game 1."""
        self.assertIsNone(_match_fallback_index(self.games, True, "", ""))
        self.assertIsNone(_match_fallback_index(self.games, False, "", ""))

    def test_a_numeric_string_still_resolves(self):
        """Legacy sessions persisted the index as a JSON number; be tolerant
        of the string form a hand-edited or older file may carry."""
        self.assertEqual(_match_fallback_index(self.games, "1", "", "")["name"], "Beta")

    def test_the_deleted_game_shape_is_exactly_what_the_bug_saw(self):
        """name and path both empty -- the deleted session's identity.

        This is the case that made the old sanity check return a candidate
        unconditionally. With the negative guard in place it resolves to nothing.
        """
        self.assertIsNone(_match_fallback_index(self.games, -1, "", ""))
        self.assertIsNone(resolve_library_game(_library(), {}, fallback_index=-1))

    def test_name_and_path_still_allow_a_legacy_index(self):
        """A real index with a matching name is legitimate, not a fallback guess."""
        found = _match_fallback_index(self.games, 1, "Beta", "")
        self.assertEqual(found["name"], "Beta")

    def test_a_mismatched_legacy_index_is_refused(self):
        """Index 2 is Gamma, but the session says it was Beta."""
        self.assertIsNone(_match_fallback_index(self.games, 2, "Beta", "/roms/beta.zip"))

    def test_identity_wins_over_a_stale_index(self):
        """resolve_library_game resolves by identity first; the index is a last
        resort. A session naming Alpha must get Alpha even with a stale index."""
        state = _library()
        found = resolve_library_game(state, {"game_name": "Alpha", "game_path": "/roms/alpha.zip"}, fallback_index=2)
        self.assertEqual(found["name"], "Alpha")


class DeletedGamePlaytimeTests(unittest.TestCase):
    """End-to-end through finish_session: no game's playtime may move."""

    #: finish_session resolves these through _ns(), which prefers a
    #: webapp_state attribute. Some are only in the _deps registry, so they are
    #: created here rather than left to run for real against the data dir.
    STUBBED_DEPS = (
        "backup_saves", "enforce_backup_limit", "auto_attach_obs_recording",
        "close_store_client", "session_event", "_publish_session_event",
    )

    def _run_finish(self, state, game_index, running, seconds=3600, wait_result=0):
        """Drive the real finish_session mutate against an in-memory state."""
        state = copy.deepcopy(state)
        started = datetime.now() - timedelta(seconds=seconds)
        process = MagicMock()
        process.pid = 4242
        process.poll.return_value = 0
        lease = MagicMock()

        def fake_update(mutator):
            mutator(state)
            return state

        from pkg.state import launch as launch_module

        launch_id = "launch-x"
        with patch.dict(launch_module.RUNNING, {launch_id: running}, clear=True), \
             patch.object(webapp_state, "load_state", return_value=state), \
             patch.object(webapp_state, "update_state", side_effect=fake_update), \
             patch.object(webapp_state, "wait_for_exit", return_value=wait_result), \
             patch.object(webapp_state, "run_plugins"), \
             patch.object(webapp_state, "start_game"), \
             patch("pkg.state.cache.STATE_LOCK"), \
             ExitStack() as stack:
            for name in self.STUBBED_DEPS:
                stack.enter_context(patch.object(webapp_state, name, create=True))
            launch_module.finish_session(launch_id, game_index, started, process, lease)
        return state

    def test_no_game_earns_the_deleted_sessions_hours(self):
        """The reported bug, end to end: legacy -1, game gone from the library."""
        state = _library()
        state["games"] = [g for g in state["games"] if g["game_id"] != "a"]
        before = {g["game_id"]: g.get("playtime_seconds") for g in state["games"]}
        running = {
            "stable_game_id": "a", "game": "Alpha",
            "game_path": "/roms/alpha.zip", "game_name": "Alpha",
        }

        after = self._run_finish(state, -1, running, seconds=7200)

        moved = {
            game["game_id"]: game.get("playtime_seconds")
            for game in after["games"]
            if game.get("playtime_seconds") != before.get(game["game_id"])
        }
        self.assertEqual(
            moved, {},
            f"a deleted game's playtime was credited elsewhere: {moved} "
            f"(expected no game to change from {before})",
        )

    def test_none_fallback_leaves_every_game_alone(self):
        """The current sentinel: the game is not in the library at all."""
        state = _library()
        state["games"] = [g for g in state["games"] if g["game_id"] != "a"]
        before = {g["game_id"]: g.get("playtime_seconds") for g in state["games"]}
        running = {
            "stable_game_id": "a", "game": "Alpha",
            "game_path": "/roms/alpha.zip", "game_name": "Alpha",
        }

        after = self._run_finish(state, None, running, seconds=7200)

        self.assertEqual(
            {g["game_id"]: g.get("playtime_seconds") for g in after["games"]}, before
        )

    def test_a_live_game_still_earns_its_hours(self):
        """The fix must not disable legitimate attribution."""
        state = _library()
        running = {
            "stable_game_id": "b", "game": "Beta",
            "game_path": "/roms/beta.zip", "game_name": "Beta",
        }
        after = self._run_finish(state, 1, running, seconds=1800)
        playtime = {g["game_id"]: g.get("playtime_seconds") for g in after["games"]}
        self.assertEqual(
            playtime, {"a": 100, "b": 200 + 1800, "c": 300},
            "a live game's session must still be credited, and only to that game",
        )

    def test_a_live_game_is_found_by_index_alone_when_identity_is_blank(self):
        """A session with no stable id still resolves through the index path."""
        state = _library()
        after = self._run_finish(state, 2, {}, seconds=900)
        playtime = {g["game_id"]: g.get("playtime_seconds") for g in after["games"]}
        self.assertEqual(playtime, {"a": 100, "b": 200, "c": 300 + 900})


class StoppedEventPayloadTests(unittest.TestCase):
    """S26 -- the serialized `stopped` events carry a scalar exit code.

    The stubs above are MagicMocks, which accept any keyword, so they hid a
    `timed_out=` the real `session_event` did not take. Here `session_event` is
    real and the payload is read back off an SSE subscriber as JSON, the way
    the client sees it.
    """

    STUBBED_DEPS = tuple(
        name for name in DeletedGamePlaytimeTests.STUBBED_DEPS
        if name not in ("session_event", "_publish_session_event")
    )
    _run_finish = DeletedGamePlaytimeTests._run_finish

    def _stopped_payloads(self, wait_result):
        import json
        import queue

        from pkg.parity.parity_tracking import WaitResult
        from pkg.state import sse

        subscriber = queue.Queue(maxsize=16)
        self.assertTrue(sse.register_event_subscriber(subscriber))
        try:
            with patch.object(webapp_state, "_publish_session_event", create=True) as publish:
                running = {"stable_game_id": "b", "game": "Beta", "game_path": "/roms/beta.zip", "game_name": "Beta"}
                self._run_finish(_library(), 1, running, seconds=2, wait_result=WaitResult(*wait_result))
        finally:
            sse.unregister_event_subscriber(subscriber)
        events = []
        while not subscriber.empty():
            events.append(subscriber.get_nowait())
        stopped = [json.loads(data) for kind, data in events if kind == "stopped"]
        self.assertEqual(len(stopped), 1, f"expected one stopped event, got {events}")
        envelope = json.loads(json.dumps(publish.call_args.args[0]))
        self.assertEqual(envelope["type"], "session.stopped")
        return stopped[0], envelope["data"]

    def test_a_failed_exit_is_an_int_on_both_events(self):
        for payload in self._stopped_payloads((1, False)):
            self.assertIs(type(payload["exit_code"]), int, payload)
            self.assertEqual(payload["exit_code"], 1)
            self.assertIs(payload["timed_out"], False)

    def test_a_timeout_is_a_separate_bool(self):
        for payload in self._stopped_payloads((-1, True)):
            self.assertEqual(payload["exit_code"], -1)
            self.assertIs(payload["timed_out"], True)


class SentinelShapeTests(unittest.TestCase):
    """'Not found' must be a value that cannot index."""

    def test_reattach_session_stores_none_not_minus_one(self):
        import inspect

        from pkg.state import launch

        source = inspect.getsource(launch.reattach_session)
        self.assertIn(
            "else None", source,
            "reattach_session must record None for 'not found'; -1 indexes the last game",
        )
        self.assertNotIn("else -1", source)

    def test_start_game_already_rejects_negative_indices(self):
        """The invariant -1 was violating is stated elsewhere in the module."""
        import inspect

        from pkg.state import launch

        self.assertIn("index < 0", inspect.getsource(launch._resolve_start_game))


if __name__ == "__main__":
    unittest.main()
