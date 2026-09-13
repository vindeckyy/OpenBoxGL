"""Direct route tests for the T2 Time Machine handlers."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_errors import BadRequest, Conflict  # noqa: E402
from handlers import timemachine as handler_module  # noqa: E402
from handlers.timemachine import TimeMachineHandlers  # noqa: E402
from pkg.parity.parity_library_sync import (  # noqa: E402
    SyncStaleError,
    SyncValidationError,
)


class MockHandler(TimeMachineHandlers):
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload):
        self.responses.append((status, payload))


def _parsed(query=""):
    return SimpleNamespace(query=query)


class EventsRouteTests(unittest.TestCase):
    def test_events_passes_filters_and_paginates(self):
        state = {"games": []}
        result = {"events": [], "total": 0, "offset": 2, "limit": 10}
        handler = MockHandler()
        with mock.patch.object(handler_module, "load_state", return_value=state), mock.patch.object(
            handler_module.time_machine, "list_events", return_value=result
        ) as list_events:
            handler._api_get_api_v2_library_time_machine_events(
                _parsed("days=30&kind=edit&game=g1&offset=2&limit=10")
            )
        list_events.assert_called_once_with(
            state, days="30", kind="edit", game_id="g1", offset=2, limit=10
        )
        self.assertEqual(handler.responses, [(200, result)])

    def test_events_rejects_bad_pagination(self):
        with mock.patch.object(handler_module, "load_state", return_value={}):
            with self.assertRaises(BadRequest) as raised:
                MockHandler()._api_get_api_v2_library_time_machine_events(
                    _parsed("offset=nope")
                )
        self.assertEqual(raised.exception.code, "TM_INVALID_REQUEST")

    def test_events_translates_engine_errors(self):
        handler = MockHandler()
        with mock.patch.object(handler_module, "load_state", return_value={}), mock.patch.object(
            handler_module.time_machine,
            "list_events",
            side_effect=ValueError("bad event filter"),
        ):
            with self.assertRaises(BadRequest) as raised:
                handler._api_get_api_v2_library_time_machine_events(_parsed())
        self.assertEqual(raised.exception.code, "TM_INVALID_REQUEST")


class AsOfRouteTests(unittest.TestCase):
    def test_as_of_parses_date_and_materializes(self):
        state = {"games": []}
        when = object()
        result = {"games": [], "count": 0}
        handler = MockHandler()
        with mock.patch.object(handler_module, "load_state", return_value=state), mock.patch.object(
            handler_module.time_machine, "parse_as_of_date", return_value=when
        ) as parse_date, mock.patch.object(
            handler_module.time_machine, "materialize_as_of", return_value=result
        ) as materialize:
            handler._api_get_api_v2_library_time_machine_as_of(_parsed("date=2026-09-12"))
        parse_date.assert_called_once_with("2026-09-12")
        materialize.assert_called_once_with(state, when)
        self.assertEqual(handler.responses, [(200, result)])

    def test_as_of_requires_a_date(self):
        with self.assertRaises(BadRequest) as raised:
            MockHandler()._api_get_api_v2_library_time_machine_as_of(_parsed())
        self.assertEqual(raised.exception.code, "TM_INVALID_DATE")

    def test_as_of_translates_invalid_date(self):
        handler = MockHandler()
        with mock.patch.object(
            handler_module.time_machine,
            "parse_as_of_date",
            side_effect=SyncValidationError("bad date"),
        ):
            with self.assertRaises(BadRequest) as raised:
                handler._api_get_api_v2_library_time_machine_as_of(_parsed("date=bad"))
        self.assertEqual(raised.exception.code, "TM_INVALID_DATE")


class RevertRouteTests(unittest.TestCase):
    def test_revert_preview_does_not_transact(self):
        plan = {"format": "time-machine-revert-v1", "base_token": "old"}
        handler = MockHandler()
        with mock.patch.object(handler_module, "load_state", return_value={}), mock.patch.object(
            handler_module.time_machine, "plan_revert", return_value=plan
        ), mock.patch.object(handler_module, "transact_state") as transact:
            handler._api_post_api_v2_library_time_machine_revert(
                {"event_id": "event-1", "base_token": "client-token"}
            )
        transact.assert_not_called()
        self.assertEqual(
            handler.responses,
            [(200, {"preview": True, "plan": {**plan, "base_token": "client-token"}})],
        )

    def test_revert_rejects_missing_event_and_bad_body(self):
        for payload in (None, {}, {"event_id": ""}):
            with self.subTest(payload=payload):
                with mock.patch.object(handler_module, "load_state", return_value={}):
                    with self.assertRaises(BadRequest) as raised:
                        MockHandler()._api_post_api_v2_library_time_machine_revert(
                            payload
                        )
            self.assertEqual(raised.exception.code, "TM_INVALID_REQUEST")

    def test_revert_translates_plan_validation(self):
        handler = MockHandler()
        with mock.patch.object(handler_module, "load_state", return_value={}), mock.patch.object(
            handler_module.time_machine,
            "plan_revert",
            side_effect=SyncValidationError("event not in journal"),
        ):
            with self.assertRaises(BadRequest) as raised:
                handler._api_post_api_v2_library_time_machine_revert(
                    {"event_id": "missing"}
                )
        self.assertEqual(raised.exception.code, "TM_INVALID_REQUEST")

    def test_revert_apply_transacts_and_returns_result(self):
        state = {"games": []}
        plan = {"format": "time-machine-revert-v1", "base_token": "token"}
        result = {"changed": True, "applied": 1}
        handler = MockHandler()

        def transact(mutator):
            return state, mutator(state)

        with mock.patch.object(handler_module, "load_state", return_value=state), mock.patch.object(
            handler_module.time_machine, "plan_revert", return_value=plan
        ), mock.patch.object(
            handler_module.time_machine, "apply_revert", return_value=result
        ) as apply_revert, mock.patch.object(
            handler_module, "transact_state", side_effect=transact
        ):
            handler._api_post_api_v2_library_time_machine_revert(
                {"event_id": "event-1", "base_token": "client-token", "apply": True}
            )
        apply_revert.assert_called_once_with(state, plan)
        self.assertEqual(handler.responses, [(200, {"preview": False, **result})])

    def test_revert_apply_translates_stale_and_validation_errors(self):
        plan = {"format": "time-machine-revert-v1", "base_token": "token"}
        for error, expected in (
            (SyncStaleError("changed"), "TM_REVERT_STALE"),
            (SyncValidationError("invalid"), "TM_INVALID_REQUEST"),
        ):
            with self.subTest(error=type(error).__name__):
                handler = MockHandler()
                with mock.patch.object(handler_module, "load_state", return_value={}), mock.patch.object(
                    handler_module.time_machine, "plan_revert", return_value=plan
                ), mock.patch.object(
                    handler_module,
                    "transact_state",
                    side_effect=error,
                ):
                    with self.assertRaises((BadRequest, Conflict)) as raised:
                        handler._api_post_api_v2_library_time_machine_revert(
                            {"event_id": "event-1", "base_token": "client-token", "apply": True}
                        )
                self.assertEqual(raised.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
