#!/usr/bin/env python3
"""Reliability #13: wrong RA/EmuMovies credentials must be 401 "check credentials".

Wrong credentials must surface HTTP 401 with a message telling the user to
check credentials in settings, never a generic 400/500.
"""
import sys
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401  # register flat-import finder


class _DummyHandler:
    def __init__(self):
        self.responses = []

    def send_json(self, status, payload, **kwargs):
        self.responses.append((status, payload, kwargs))


def _assert_check_credentials_401(callable_, label):
    try:
        callable_()
    except Exception as error:  # noqa: BLE001 - asserting on the mapped error contract
        status = getattr(error, "status", None)
        assert status == 401, f"{label}: expected status 401, got {status} ({error!r})"
        assert "credential" in str(getattr(error, "message", error)).casefold(), (
            f"{label}: 401 must say check credentials, got {error!r}"
        )
        return error
    raise AssertionError(f"{label}: expected a 401 check-credentials error, call succeeded")


def main():
    import handlers.media as media_handlers
    import handlers.settings as settings_handlers
    from handlers.media import MediaHandlers
    from handlers.settings import SettingsHandlers

    def ra_401(*args, **kwargs):
        raise HTTPError("https://retroachievements.org/API/x", 401, "Unauthorized", {}, None)

    # 1. GET /api/ra/settings with rejected credentials -> 401.
    handler = _DummyHandler()
    bound = SettingsHandlers._api_get_api_ra_settings.__get__(handler, _DummyHandler)
    with mock.patch.object(settings_handlers, "load_ra_credentials", return_value={"username": "bad", "api_key": "bad"}), \
         mock.patch.object(settings_handlers, "ra_api_get", side_effect=ra_401):
        _assert_check_credentials_401(lambda: bound(mock.Mock(query="")), "ra settings GET")

    # 2. POST /api/ra/settings with rejected credentials -> 401 (not a 500).
    handler = _DummyHandler()
    bound = SettingsHandlers.save_ra_settings.__get__(handler, _DummyHandler)
    with mock.patch.object(
        settings_handlers, "save_ra_credentials",
        side_effect=ValueError("RetroAchievements rejected those credentials."),
    ):
        _assert_check_credentials_401(lambda: bound({"username": "bad", "api_key": "bad"}), "ra settings POST")

    # 3. Non-credential RA failure stays a generic error (no false 401).
    handler = _DummyHandler()
    bound = SettingsHandlers._api_get_api_ra_settings.__get__(handler, _DummyHandler)
    with mock.patch.object(settings_handlers, "load_ra_credentials", return_value={"username": "u", "api_key": "k"}), \
         mock.patch.object(settings_handlers, "ra_api_get", side_effect=OSError("disk gone")):
        try:
            bound(mock.Mock(query=""))
        except Exception as error:  # noqa: BLE001
            assert getattr(error, "status", None) != 401, f"non-auth failure must not be 401: {error!r}"
        else:
            raise AssertionError("expected an error for OSError")

    # 4. EmuMovies download with a 401 from the service -> 401 check-credentials.
    def emumovies_401(*args, **kwargs):
        try:
            raise HTTPError("https://api.emumovies.com/v1/media/box", 401, "Unauthorized", {}, None)
        except HTTPError as inner:
            raise ValueError(f"EmuMovies request failed: {inner}") from inner

    handler = _DummyHandler()
    bound = MediaHandlers.emumovies_download.__get__(handler, _DummyHandler)
    fake_state = {"games": [{"game_id": "g1", "name": "Game", "platform": "Arcade"}]}
    with mock.patch.object(media_handlers, "load_emumovies_credentials", return_value={"username": "u", "password": "p"}), \
         mock.patch.object(media_handlers, "load_state", return_value=fake_state), \
         mock.patch.object(media_handlers, "game_from_payload", return_value=dict(fake_state["games"][0])), \
         mock.patch.object(media_handlers, "download_emumovies_media", side_effect=emumovies_401):
        _assert_check_credentials_401(lambda: bound({"game_id": "g1"}), "emumovies download")

    # 5. Non-credential EmuMovies failure is not rewritten to 401.
    handler = _DummyHandler()
    bound = MediaHandlers.emumovies_download.__get__(handler, _DummyHandler)
    with mock.patch.object(media_handlers, "load_emumovies_credentials", return_value={"username": "u", "password": "p"}), \
         mock.patch.object(media_handlers, "load_state", return_value=fake_state), \
         mock.patch.object(media_handlers, "game_from_payload", return_value=dict(fake_state["games"][0])), \
         mock.patch.object(media_handlers, "download_emumovies_media", side_effect=ValueError("EmuMovies request failed: disk full")):
        try:
            bound({"game_id": "g1"})
        except Exception as error:  # noqa: BLE001
            assert getattr(error, "status", None) != 401, f"non-auth failure must not be 401: {error!r}"
        else:
            raise AssertionError("expected an error for disk failure")

    # 6. Non-credential RA save failure is not rewritten to 401.
    handler = _DummyHandler()
    bound = SettingsHandlers.save_ra_settings.__get__(handler, _DummyHandler)
    with mock.patch.object(
        settings_handlers, "save_ra_credentials",
        side_effect=ValueError("RetroAchievements username and web API key are required."),
    ):
        try:
            bound({"username": "", "api_key": ""})
        except Exception as error:  # noqa: BLE001
            assert getattr(error, "status", None) != 401, f"non-auth failure must not be 401: {error!r}"
            assert isinstance(error, ValueError)
        else:
            raise AssertionError("expected an error for missing credentials")

    # 7. Auth-failure helpers: numeric/string/bogus codes and cause chains.
    from retroachievements import is_auth_failure
    from parity_integrations import is_emumovies_auth_failure

    assert is_auth_failure(HTTPError("https://x", 403, "Forbidden", {}, None)) is True
    assert is_auth_failure(ValueError("RetroAchievements rejected those credentials.")) is True
    assert is_auth_failure(OSError("disk gone")) is False
    class _BogusCode(Exception):
        code = "bogus"
    assert is_auth_failure(_BogusCode("boom")) is False
    chained = ValueError("EmuMovies request failed: boom")
    chained.__cause__ = HTTPError("https://x", 401, "Unauthorized", {}, None)
    assert is_emumovies_auth_failure(chained) is True

    print("credentials 401 self-test: ok")


if __name__ == "__main__":
    main()
