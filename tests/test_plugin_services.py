#!/usr/bin/env python3
"""P9.4 allowlisted read-only plugin service tests."""

import copy
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity import parity_plugin_services as services  # noqa: E402


def _state():
    return {
        "games": [
            {
                "game_id": "g1", "name": "Zelda", "platform": "SNES",
                "progress": "Playing", "favorite": True, "playtime_seconds": 120,
                "last_played": "2026-01-01T00:00:00+00:00", "source": "Steam",
                "ra_progress_pct": 40.0, "ra_achievements_earned": 4,
                "ra_achievements_total": 10, "ra_mastered": False,
                "path": "/home/user/games/zelda.sfc",
                "achievement_sources": {
                    "steam": {
                        "name": "Steam", "earned": 1, "total": 5, "progress_pct": 20.0,
                        "items": [{"id": "a1", "title": "First", "earned": True, "path": "/secret"}],
                    },
                },
            },
            {
                "game_id": "g2", "name": "Mario", "platform": "NES",
                "playtime_seconds": 60, "path": "/home/user/games/mario.nes",
            },
            {
                "game_id": "g3", "name": "Zelda II", "platform": "SNES",
                "playtime_seconds": 30, "favorite": False,
            },
        ],
        "history": [
            {"game": "Mario", "started": "2026-01-01T00:00:00", "seconds": 60, "exit_code": 0, "game_id": "g2"},
            {"game": "Zelda", "started": "2026-01-02T00:00:00", "seconds": 120, "exit_code": [0, False], "game_id": "g1"},
        ],
        "household": {},
    }


def test_rejections():
    for action in ("nope", "", "library.query ", "services"):
        try:
            services.handle_service(action, {}, {})
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {action!r}")
    try:
        services.handle_service("library.query", [], {})
    except ValueError:
        pass
    else:
        raise AssertionError("non-object params must be rejected")
    assert services.SERVICES == (
        "library.query", "achievements", "profile", "activity",
        "leaderboards", "catalog.search", "metadata.search", "media.search",
    )


def test_library_query():
    state = _state()
    before = copy.deepcopy(state)
    result = services.handle_service("library.query", {}, state)
    assert state == before
    assert len(result["games"]) == 3 and result["truncated"] is False
    first = result["games"][0]
    assert set(first) == {
        "game_id", "name", "platform", "progress", "favorite",
        "playtime_seconds", "last_played", "source", "ra_progress_pct",
    }
    assert "path" not in str(result)
    assert services.handle_service("library.query", {"query": "zelda"}, state)["games"][0]["game_id"] == "g1"
    assert [row["game_id"] for row in services.handle_service(
        "library.query", {"platform": "snes"}, state,
    )["games"]] == ["g1", "g3"]
    truncated = services.handle_service("library.query", {"limit": 2}, state)
    assert len(truncated["games"]) == 2 and truncated["truncated"] is True
    assert services.handle_service("library.query", {"limit": 99999}, state)["truncated"] is False


def test_achievements_and_profile():
    state = _state()
    result = services.handle_service("achievements", {"game_ids": ["g1", "g2"]}, state)
    assert [game["game_id"] for game in result["games"]] == ["g1", "g2"]
    sources = result["games"][0]["achievement_sources"]
    assert sources["steam"]["earned"] == 1
    assert sources["steam"]["items"][0]["title"] == "First"
    assert "path" not in str(result)
    # A game without projected sources falls back to stored RA totals.
    state["games"][1]["ra_achievements_total"] = 3
    state["games"][1]["ra_achievements_earned"] = 1
    result = services.handle_service("achievements", {"game_ids": ["g2"]}, state)
    assert result["games"][0]["achievement_sources"]["retroachievements"]["total"] == 3
    # Item caps are enforced per source.
    state["games"][0]["achievement_sources"]["steam"]["items"] = [
        {"id": f"i{index}", "title": "x"} for index in range(services.MAX_ACHIEVEMENT_ITEMS + 5)
    ]
    capped = services.handle_service("achievements", {"game_ids": ["g1"]}, state)
    steam = capped["games"][0]["achievement_sources"]["steam"]
    assert len(steam["items"]) == services.MAX_ACHIEVEMENT_ITEMS and steam["truncated"] is True
    for bad in ({}, {"game_ids": "g1"}, {"game_ids": [f"g{i}" for i in range(101)]}):
        try:
            services.handle_service("achievements", bad, state)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for game_ids")

    profile = services.handle_service("profile", {}, _state())
    assert profile == {
        "games": 3,
        "playtime_seconds": 210,
        "achievements_earned": 1,
        "achievements_total": 5,
        "mastered": 0,
        "favorite_count": 1,
    }


def test_activity_and_leaderboards():
    result = services.handle_service("activity", {}, _state())
    assert [entry["game_id"] for entry in result["entries"]] == ["g1", "g2"]
    assert result["entries"][0] == {
        "game_id": "g1", "name": "Zelda", "started": "2026-01-02T00:00:00",
        "seconds": 120, "exit_code": 0,
    }
    assert result["truncated"] is False
    board = services.handle_service("leaderboards", {}, {"household": {}})
    assert board["entries"] == [] and board["available"] is False
    state = _state()
    state["settings"] = {"household_stats_sharing": True}
    assert services.handle_service("leaderboards", {}, state)["available"] is True


def test_catalog_search():
    rows = [
        {"database_id": 7, "name": "Zelda", "platform": "SNES", "year": "1991", "path": "/x"},
        {"database_id": 8, "name": "Zelda II", "platform": "NES", "path": "/y"},
    ]
    with mock.patch.object(services, "_search_catalog", return_value=rows) as search:
        result = services.handle_service("catalog.search", {"query": "zelda", "platform": "SNES"}, {})
    search.assert_called_once_with("zelda", "SNES", services.MAX_CATALOG_DEFAULT)
    assert result["candidates"][0] == {
        "database_id": 7, "name": "Zelda", "platform": "SNES", "year": "1991",
    }
    assert "path" not in str(result)
    for bad in ({}, {"query": "   "}):
        try:
            services.handle_service("catalog.search", bad, {})
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
    with mock.patch.object(services, "_search_catalog", side_effect=ValueError("Metadata database is not available.")):
        try:
            services.handle_service("catalog.search", {"query": "x"}, {})
        except ValueError as error:
            assert "not available" in str(error)


def test_metadata_search():
    candidates = [
        {
            "id": "c1", "source": "launchbox", "name": "Zelda", "platform": "SNES",
            "year": "1991", "fields": {"genre": "Adventure", "path": "/x"},
            "media": {"cover": ["https://93.184.216.34/c.jpg"] * 12, "bad": "x"},
            "path": "/secret",
        },
        "not-a-dict",
    ]
    with mock.patch.object(services, "_metadata_search_all", return_value=candidates):
        result = services.handle_service("metadata.search", {"query": "zelda", "limit": 5}, {})
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["fields"] == {"genre": "Adventure"}
    assert len(candidate["media"]["cover"]) == 10
    assert "bad" not in candidate["media"]
    assert "secret" not in str(result)
    for bad in ({"game": "nope"}, {}):
        with mock.patch.object(services, "_metadata_search_all", return_value=[]):
            try:
                services.handle_service("metadata.search", bad, {})
            except ValueError:
                pass
            else:
                raise AssertionError("expected ValueError")
    # Limits are clamped, never trusted.
    with mock.patch.object(services, "_metadata_search_all", return_value=[]) as search:
        services.handle_service("metadata.search", {"query": "z", "limit": 50}, {})
    assert search.call_args.args[2] == services.MAX_METADATA


def test_media_search_and_url_safety():
    candidates = [
        {"url": "https://93.184.216.34/cover.jpg", "title": "Cover", "width": 600},
        {"url": "http://93.184.216.34/plain.jpg"},
        {"url": "https://127.0.0.1/local.jpg"},
        {"url": "https://10.0.0.1/private.jpg"},
        "https://93.184.216.34/raw.jpg",
        {"url": "not a url"},
        {"title": "missing"},
    ]
    with mock.patch.object(services, "_media_candidates", return_value=candidates), \
         mock.patch.object(services, "_media_kinds", return_value=frozenset({"cover"})):
        result = services.handle_service("media.search", {"game_id": "g1", "kind": "cover"}, _state())
    urls = [candidate["url"] for candidate in result["candidates"]]
    assert urls == ["https://93.184.216.34/cover.jpg", "https://93.184.216.34/raw.jpg"]
    assert result["candidates"][0]["width"] == 600
    assert "path" not in str(result)
    with mock.patch.object(services, "_media_kinds", return_value=frozenset({"cover"})):
        for bad in (
            {},
            {"game_id": "g1", "kind": "hologram"},
            {"game_id": "missing", "kind": "cover"},
        ):
            try:
                services.handle_service("media.search", bad, _state())
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {bad}")

    for unsafe in (
        "http://93.184.216.34/x.jpg",
        "https://user:pass@93.184.216.34/x.jpg",
        "https://127.0.0.1/x.jpg",
        "https://10.1.2.3/x.jpg",
        "https://169.254.1.1/x.jpg",
        "https://[::1]/x.jpg",
        "https:///" + "x" * 3000,
        "",
    ):
        try:
            services.safe_media_url(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {unsafe!r}")
    assert services.safe_media_url("https://93.184.216.34/a.jpg") == "https://93.184.216.34/a.jpg"


def test_service_state_does_not_recurse_during_build():
    from tempfile import TemporaryDirectory

    import openbox
    import plugins
    from pkg.state.cache import BUILDING_PUBLIC_STATE

    with TemporaryDirectory() as directory:
        with mock.patch.object(openbox, "load_state_readonly", return_value=_state()), \
             mock.patch.object(openbox, "DATA", Path(directory) / "library.json"), \
             mock.patch("webapp_state.public_state", side_effect=AssertionError("recursed")):
            token = BUILDING_PUBLIC_STATE.set(True)
            try:
                state = plugins._service_state()
            finally:
                BUILDING_PUBLIC_STATE.reset(token)
    assert state["games"][0]["name"] == "Zelda"


def test():
    test_rejections()
    test_library_query()
    test_achievements_and_profile()
    test_activity_and_leaderboards()
    test_catalog_search()
    test_metadata_search()
    test_media_search_and_url_safety()
    test_service_state_does_not_recurse_during_build()
    print("plugin services: ok")


if __name__ == "__main__":
    test()
