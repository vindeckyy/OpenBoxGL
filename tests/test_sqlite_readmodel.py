#!/usr/bin/env python3
"""Tests for the SQLite read model (1.7.2).

Verifies:
  - rebuild() populates the database from a state dict.
  - query() returns games matching filters.
  - search() finds games by text (FTS5 or LIKE fallback).
  - facets() computes value counts via GROUP BY.
  - invalidate() marks the model stale.
  - Flag-off behavior: all methods are no-ops when disabled.
  - Query parity: SQLite results match JSON path game IDs.
  - FTS5 unavailable: LIKE fallback works.
  - The debounced background rebuild: it runs off the caller's thread, takes
    its state from the provider, and publishes the signature it rebuilt from.
  - A corrupt database is discarded and rebuilt.
  - A parity mismatch drops the model back to the JSON read path.
"""

import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    if (candidate / "runtime_modules.txt").is_file():
        return candidate
    if (candidate.parent / "runtime_modules.txt").is_file():
        return candidate.parent
    return candidate

ROOT = _repo_root()
sys.path.insert(0, str(ROOT))

from pkg.state.sqlite_readmodel import SqliteReadModel, _check_fts5  # noqa: E402


def _make_state(n=100):
    games = []
    for i in range(n):
        games.append({
            "game_id": f"game-{i:04d}",
            "name": f"Test Game {i}",
            "platform": ["PC", "Steam", "GOG", "NES", "SNES"][i % 5],
            "genre": ["Action", "RPG", "Puzzle", "Racing", "Sports"][i % 5],
            "developer": f"Dev {i % 10}",
            "publisher": f"Pub {i % 10}",
            "year": 1990 + (i % 30),
            "favorite": i % 7 == 0,
            "hidden": i % 13 == 0,
            "installed": i % 3 == 0,
            "play_count": i * 10,
            "playtime_seconds": i * 3600,
            "rating": (i % 5) + 0.5,
            "progress": ["", "Playing", "Completed", "Beaten"][i % 4],
            "description": f"A test game number {i} for testing",
        })
    return {"games": games, "settings": {}, "profiles": {}}


def _semantic_state():
    """Corpus for server-search semantics rather than FTS token behavior."""
    return {
        "games": [
            {
                "game_id": "hidden-alpha",
                "name": "Zeta Alpha",
                "title": "Wrong Title",
                "platform": "PC",
                "hidden": True,
            },
            {
                "game_id": "visible-alpha",
                "name": "alpha: beta",
                "platform": "Steam",
                "developer": "Alpha Studios",
            },
            {
                "game_id": "unicode",
                "name": "Straße Quest",
                "platform": "Console",
            },
            {
                "game_id": "metadata-only",
                "name": "Canonical Name",
                "title": "Wrong Title",
                "platform": "Alpha Platform",
                "genre": "Puzzle",
            },
        ],
        "settings": {},
        "profiles": {},
    }


def test_rebuild_and_count():
    """rebuild() should populate the database and count() should return the count."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(50)
        rm.rebuild(state)
        assert rm.count() == 50, f"Expected 50 games, got {rm.count()}"
        rm.close()


def test_query_by_platform():
    """query(platform=...) should return only games on that platform."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(100)
        rm.rebuild(state)
        pc_games = rm.query(platform="PC")
        assert all(g["platform"] == "PC" for g in pc_games), "Non-PC games in PC query"
        assert len(pc_games) == 20, f"Expected 20 PC games, got {len(pc_games)}"
        rm.close()


def test_query_favorite():
    """query(favorite=True) should return only favorite games."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(100)
        rm.rebuild(state)
        favs = rm.query(favorite=True)
        assert all(g["favorite"] for g in favs), "Non-favorite in favorite query"
        assert len(favs) == 15, f"Expected 15 favorites (i%7==0 for 0..99), got {len(favs)}"
        rm.close()


def test_search_fts():
    """search() should find games by title text."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(100)
        rm.rebuild(state)
        results = rm.search("Test Game 5")
        assert len(results) > 0, "Search returned no results"
        assert any("Test Game 5" in g["name"] for g in results), "Search didn't find expected name"
        rm.close()


def test_search_like_fallback():
    """search() should work with LIKE fallback when FTS5 is unavailable."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(50)
        rm.rebuild(state)
        # Force LIKE fallback by temporarily disabling FTS5
        import pkg.state.sqlite_readmodel as mod
        old_fts5 = mod._FTS5_AVAILABLE
        mod._FTS5_AVAILABLE = False
        try:
            results = rm.search("Game 4")
            assert len(results) > 0, "LIKE search returned no results"
        finally:
            mod._FTS5_AVAILABLE = old_fts5
        rm.close()


def test_search_semantic_corpus_uses_name_casefold_and_library_order():
    """Search matches canonical names, including hidden games, in input order."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        rm.rebuild(_semantic_state())
        alpha = rm.search("ALPHA")
        assert [game["game_id"] for game in alpha] == ["hidden-alpha", "visible-alpha"]
        assert rm.search("wrong title") == []
        assert [game["game_id"] for game in rm.search("STRASSE")] == ["unicode"]
        # FTS operators and punctuation are ordinary substring text here.
        assert rm.search('alpha AND [broken') == []
        rm.close()


def test_search_semantic_limit_is_clamped():
    """The read model follows the v2 search result bounds."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = {
            "games": [
                {"game_id": f"g-{index}", "name": f"Alpha {index}"}
                for index in range(205)
            ],
            "settings": {},
        }
        rm.rebuild(state)
        assert len(rm.search("alpha", limit=0)) == 1
        assert len(rm.search("alpha", limit=999)) == 200
        rm.close()


def test_facets():
    """facets() should return (value, count) tuples sorted by count."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(100)
        rm.rebuild(state)
        platform_facets = rm.facets("platform")
        assert len(platform_facets) > 0, "No platform facets"
        assert all(isinstance(f, tuple) and len(f) == 2 for f in platform_facets), "Bad facet format"
        # Each platform should have 20 games (100/5)
        top_platform = platform_facets[0]
        assert top_platform[1] == 20, f"Expected 20 per platform, got {top_platform[1]}"
        rm.close()


def test_facets_invalid_field():
    """facets() should return empty list for disallowed fields."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(10)
        rm.rebuild(state)
        result = rm.facets("game_id")  # not in allowed set
        assert result == [], "facets() should return empty for disallowed field"
        rm.close()


def test_invalidate():
    """invalidate() should mark the model stale for rebuild on next ensure_fresh."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(10)
        rm.rebuild(state)
        assert rm.count() == 10
        rm.invalidate()
        # After invalidation, ensure_fresh with a new signature should rebuild
        state2 = _make_state(20)
        rm.ensure_fresh(state2, (1, 2, 3))
        assert rm.count() == 20, f"Expected 20 after rebuild, got {rm.count()}"
        rm.close()


def test_ensure_fresh_no_rebuild_when_same_sig():
    """ensure_fresh() should not rebuild when signature is unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(10)
        sig = (100, 200, 300)
        rm.ensure_fresh(state, sig)
        assert rm.count() == 10
        # Same signature → no rebuild
        state2 = _make_state(20)
        rm.ensure_fresh(state2, sig)
        assert rm.count() == 10, "Rebuilt despite same signature"
        rm.close()


def test_disabled_noop():
    """When disabled, all methods should be no-ops returning empty/zero."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = False
        rm.rebuild(_make_state(10))
        assert rm.count() == 0
        assert rm.query() == []
        assert rm.search("test") == []
        assert rm.facets("platform") == []
        rm.close()


def test_query_parity():
    """SQLite query results should match JSON path game IDs."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(100)
        rm.rebuild(state)
        json_games = state["games"]
        assert rm.query_parity_check(json_games), "Query parity check failed"
        rm.close()


def test_empty_state():
    """rebuild() with empty state should produce an empty database."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        rm.rebuild({"games": [], "settings": {}})
        assert rm.count() == 0
        assert rm.query() == []
        assert rm.search("anything") == []
        assert rm.facets("platform") == []
        rm.close()


def test_malformed_games():
    """rebuild() should skip non-dict and missing-game_id entries."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = {"games": [
            {"game_id": "g1", "title": "Good"},
            {"title": "No ID"},  # missing game_id
            "not a dict",  # not a dict
            {"game_id": "g2", "title": "Also Good"},
        ], "settings": {}}
        rm.rebuild(state)
        assert rm.count() == 2, f"Expected 2 valid games, got {rm.count()}"
        rm.close()


def test_fts5_check():
    """_check_fts5() should return a boolean."""
    result = _check_fts5()
    assert isinstance(result, bool), f"Expected bool, got {type(result)}"


def test_query_limit_offset():
    """query() should respect limit and offset parameters."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(50)
        rm.rebuild(state)
        page1 = rm.query(limit=10, offset=0)
        page2 = rm.query(limit=10, offset=10)
        assert len(page1) == 10
        assert len(page2) == 10
        page1_ids = {g["game_id"] for g in page1}
        page2_ids = {g["game_id"] for g in page2}
        assert page1_ids.isdisjoint(page2_ids), "Pages overlap"
        rm.close()


def test_runtime_modules_has_sqlite():
    """runtime_modules.txt must include sqlite_readmodel.py."""
    content = (ROOT / "runtime_modules.txt").read_text(encoding="utf-8")
    assert "sqlite_readmodel.py" in content, "sqlite_readmodel.py not in runtime_modules.txt"


def test_singleton_exists():
    """SQLITE_READ_MODEL singleton is importable from pkg.state.cache."""
    from pkg.state.cache import SQLITE_READ_MODEL
    assert SQLITE_READ_MODEL is not None
    assert hasattr(SQLITE_READ_MODEL, "enabled")
    assert hasattr(SQLITE_READ_MODEL, "invalidate")
    assert hasattr(SQLITE_READ_MODEL, "facets")
    assert hasattr(SQLITE_READ_MODEL, "search")


def test_auto_enable_threshold():
    """should_auto_enable() latches on at SQLITE_AUTO_THRESHOLD (1.12.0)."""
    from pkg.state.sqlite_readmodel import SQLITE_AUTO_THRESHOLD
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = False
        assert rm.should_auto_enable(SQLITE_AUTO_THRESHOLD - 1) is False
        assert rm.enabled is False
        assert rm.should_auto_enable(SQLITE_AUTO_THRESHOLD) is True
        assert rm.enabled is True  # latched: stays on afterwards
        assert rm.should_auto_enable(0) is True
        rm.close()


def test_auto_enable_rejects_garbage():
    """should_auto_enable() treats unparsable counts as small."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = False
        assert rm.should_auto_enable(None) is False
        assert rm.should_auto_enable("many") is False
        rm.close()


def test_search_route_registered():
    """GET /api/v2/library/search is in the route table."""
    from routes import GET_TABLE
    assert "/api/v2/library/search" in GET_TABLE, "search route not registered"


def test_a_scheduled_rebuild_runs_off_the_caller_thread():
    """The rebuild lands shortly after scheduling, without blocking the caller."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(20)
        signature = (1, 2, 3)
        assert rm.is_fresh(signature) is False
        caller = threading.current_thread().name
        assert rm.schedule_rebuild(state, signature, delay=0.05) is True
        for _ in range(100):
            if rm.is_fresh(signature):
                break
            time.sleep(0.05)
        assert rm.is_fresh(signature) is True
        assert rm.count() == 20
        # The rebuild ran on its own thread, not on the one that asked for it.
        assert rm._rebuild_thread_name != caller
        rm.close()


def test_a_scheduled_rebuild_takes_its_state_from_the_provider():
    """With no state supplied, the provider hands over the current snapshot."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(5)
        calls = []

        def provider():
            calls.append(threading.current_thread().name)
            return state, (9, 9, 9)

        rm.set_state_provider(provider)
        rm.schedule_rebuild(delay=0.05)
        for _ in range(100):
            if rm.is_fresh((9, 9, 9)):
                break
            time.sleep(0.05)
        assert rm.is_fresh((9, 9, 9)) is True
        assert rm.count() == 5
        assert calls, "the state provider was never called"
        rm.close()


def test_a_disabled_model_schedules_nothing():
    """A disabled model stays a no-op, including the background rebuild."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = False
        assert rm.schedule_rebuild(_make_state(3), (1, 1, 1)) is False
        time.sleep(0.2)
        assert rm.count() == 0
        rm.close()


def test_a_corrupt_database_is_discarded_and_rebuilt():
    """A corrupt projection is not data loss: it is dropped and rebuilt."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db_path.write_bytes(b"definitely not a sqlite database" * 20)
        rm = SqliteReadModel(db_path)
        rm._enabled = True
        rm.rebuild(_make_state(4))
        assert rm.count() == 4
        rm.close()


def test_a_corrupt_database_is_released_before_it_is_discarded():
    """The failed connection must be closed before the file is removed.

    Windows refuses to unlink a file that is still open, so a projection that
    keeps the handle would never be discarded and the read model would stay
    broken for the life of the process. The check runs while the discard
    happens, because that is the moment the file is unlinked.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db_path.write_bytes(b"definitely not a sqlite database" * 20)
        rm = SqliteReadModel(db_path)
        rm._enabled = True
        opened = []
        real_connect = sqlite3.connect

        def spy_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def check_then_discard(self):
            for conn in opened:
                try:
                    conn.execute("SELECT 1")
                except sqlite3.ProgrammingError:
                    continue  # closed, which is what the discard needs
                except sqlite3.Error as error:
                    raise AssertionError(
                        f"a connection to the corrupt database was left open: {error}"
                    ) from error
                raise AssertionError("a connection to the corrupt database was left open")
            real_discard(self)

        real_discard = SqliteReadModel._discard_database
        with mock.patch("pkg.state.sqlite_readmodel.sqlite3.connect", spy_connect), \
             mock.patch.object(SqliteReadModel, "_discard_database", check_then_discard):
            rm.rebuild(_make_state(4))
        assert opened, "expected the read model to open a connection"
        assert rm.count() == 4
        rm.close()


def test_a_parity_mismatch_drops_the_model_back_to_the_json_path():
    """A projection that disagrees with the canonical state is not trusted."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _make_state(6)
        signature = (7, 7, 7)
        rm.query_parity_check = lambda games: False
        rm.schedule_rebuild(state, signature, delay=0.05)
        for _ in range(100):
            if rm._signature == signature:
                break
            time.sleep(0.05)
        # The rebuild landed, but the parity check failed, so reads must fall
        # back to the JSON path.
        assert rm.is_fresh(signature) is False
        rm.close()


def test_close_cancels_a_pending_rebuild():
    """Closing the model stops the pending rebuild instead of leaking a timer."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        rm.schedule_rebuild(_make_state(3), (1, 1, 1), delay=5.0)
        rm.close()
        assert rm._timer is None
        assert rm._pending_state is None


def run_all_tests():
    tests = [
        test_rebuild_and_count,
        test_query_by_platform,
        test_query_favorite,
        test_search_fts,
        test_search_like_fallback,
        test_search_semantic_corpus_uses_name_casefold_and_library_order,
        test_search_semantic_limit_is_clamped,
        test_facets,
        test_facets_invalid_field,
        test_invalidate,
        test_ensure_fresh_no_rebuild_when_same_sig,
        test_disabled_noop,
        test_query_parity,
        test_empty_state,
        test_malformed_games,
        test_fts5_check,
        test_query_limit_offset,
        test_runtime_modules_has_sqlite,
        test_singleton_exists,
        test_search_route_registered,
        test_auto_enable_threshold,
        test_auto_enable_rejects_garbage,
        test_a_scheduled_rebuild_runs_off_the_caller_thread,
        test_a_scheduled_rebuild_takes_its_state_from_the_provider,
        test_a_disabled_model_schedules_nothing,
        test_a_corrupt_database_is_discarded_and_rebuilt,
        test_a_corrupt_database_is_released_before_it_is_discarded,
        test_a_parity_mismatch_drops_the_model_back_to_the_json_path,
        test_close_cancels_a_pending_rebuild,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            print(f"FAIL {test.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"ERROR {test.__name__}: {e}")
            failures += 1
    if failures:
        print(f"\n{failures} test(s) failed")
        return 1
    print(f"\nALL PASS ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
