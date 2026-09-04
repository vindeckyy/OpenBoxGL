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
"""

import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path

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
            "title": f"Test Game {i}",
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
        assert any("Test Game 5" in g["title"] for g in results), "Search didn't find expected title"
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
    content = (ROOT / "runtime_modules.txt").read_text()
    assert "sqlite_readmodel.py" in content, "sqlite_readmodel.py not in runtime_modules.txt"


def test_singleton_exists():
    """SQLITE_READ_MODEL singleton is importable from pkg.state.cache."""
    from pkg.state.cache import SQLITE_READ_MODEL
    assert SQLITE_READ_MODEL is not None
    assert hasattr(SQLITE_READ_MODEL, "enabled")
    assert hasattr(SQLITE_READ_MODEL, "invalidate")
    assert hasattr(SQLITE_READ_MODEL, "facets")
    assert hasattr(SQLITE_READ_MODEL, "search")


def test_search_route_registered():
    """GET /api/v2/library/search is in the route table."""
    from routes import GET_TABLE
    assert "/api/v2/library/search" in GET_TABLE, "search route not registered"


# ---------------------------------------------------------------------------
# S1 phase 2 (ADR 0037): observability + second-flag filtered queries.
# ---------------------------------------------------------------------------

def _s1_state(n=60):
    """State with both title (sqlite) and name (JSON search) keys populated."""
    games = []
    for i in range(n):
        games.append({
            "game_id": "s1-%04d" % i,
            "title": "S1 Game %d" % i,
            "name": "S1 Game %d" % i,
            "platform": ["PC", "Steam", "GOG"][i % 3],
            "genre": ["Action", "RPG"][i % 2],
            "favorite": i % 5 == 0,
            "hidden": i % 11 == 0,
            "installed": i % 2 == 0,
        })
    return {"games": games, "settings": {}, "profiles": {}}


class _StubHandler:
    """Minimal handler stub: facet/search methods only use send_json."""

    def __init__(self):
        self.captured = []
        self.headers = {}

    def send_json(self, status, payload):
        self.captured.append((status, payload))


class _Parsed:
    def __init__(self, query):
        self.query = query


@contextlib.contextmanager
def _patched_handler_env(state, sig, enabled=True, query_env=None, parity_env=None):
    """Swap the SQLITE_READ_MODEL singleton for a temp-db model.

    Handler methods resolve the singleton at call time, so patching the
    module attribute is sufficient. Rebuilds from *state* when enabled.
    """
    import openbox
    import pkg.state.cache as cache_mod
    import handlers.library as libmod
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "t.db")
        rm._enabled = enabled
        if enabled:
            rm.rebuild(state)
        orig_rm = cache_mod.SQLITE_READ_MODEL
        orig_view = libmod.load_state_view
        orig_sig = openbox.STATE_STORE.signature
        old_q = os.environ.get("OPENBOX_ENABLE_SQLITE_QUERY")
        old_p = os.environ.get("OPENBOX_SQLITE_PARITY_LOG")
        try:
            cache_mod.SQLITE_READ_MODEL = rm
            libmod.load_state_view = lambda: state
            openbox.STATE_STORE.signature = lambda: sig
            if query_env is None:
                os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)
            else:
                os.environ["OPENBOX_ENABLE_SQLITE_QUERY"] = query_env
            if parity_env is None:
                os.environ.pop("OPENBOX_SQLITE_PARITY_LOG", None)
            else:
                os.environ["OPENBOX_SQLITE_PARITY_LOG"] = parity_env
            yield rm
        finally:
            cache_mod.SQLITE_READ_MODEL = orig_rm
            libmod.load_state_view = orig_view
            openbox.STATE_STORE.signature = orig_sig
            if old_q is None:
                os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)
            else:
                os.environ["OPENBOX_ENABLE_SQLITE_QUERY"] = old_q
            if old_p is None:
                os.environ.pop("OPENBOX_SQLITE_PARITY_LOG", None)
            else:
                os.environ["OPENBOX_SQLITE_PARITY_LOG"] = old_p
            rm.close()


class _CapLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _capture_sqlite_logs():
    logger = logging.getLogger("openbox.sqlite_readmodel")
    cap = _CapLog()
    old_level = logger.level
    logger.addHandler(cap)
    logger.setLevel(logging.DEBUG)
    try:
        yield cap
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)


def _expected_filtered(state):
    """Independent expectation for the combined-filter query test."""
    out = []
    for game in state["games"]:
        if game["platform"] != "PC":
            continue
        if game["genre"] != "Action":
            continue
        if not game["favorite"]:
            continue
        if game["hidden"]:
            continue
        if not game["installed"]:
            continue
        out.append(game)
    out.sort(key=lambda game: game["title"])
    return out


def test_query_combined_filters():
    """query() must honor filters together with limit/offset."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _s1_state(60)
        rm.rebuild(state)
        pool = [g for g in state["games"]
                if g["platform"] == "PC" and g["genre"] == "Action" and g["installed"]]
        pool.sort(key=lambda game: game["title"])
        assert len(pool) == 10, "expected 10 PC/Action/installed games, got %d" % len(pool)
        expected = pool[2:9]
        got = rm.query(platform="PC", genre="Action", installed=True, limit=7, offset=2)
        assert [g["game_id"] for g in got] == [g["game_id"] for g in expected], (
            "combined filtered query mismatch: %r" % ([g["game_id"] for g in got],)
        )
        # favorite/hidden participate in combined filtering.
        fav_visible = rm.query(favorite=True, hidden=False, limit=100000)
        assert fav_visible, "expected favorite+visible games"
        assert all(g["favorite"] and not g["hidden"] for g in fav_visible)
        rm.close()


def test_query_flag_defaults_off():
    """Second flag defaults off; query_enabled requires both flags."""
    old = os.environ.get("OPENBOX_ENABLE_SQLITE_QUERY")
    os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)
    try:
        from pkg.state.sqlite_readmodel import is_query_enabled
        assert is_query_enabled() is False, "query flag must default off"
        with tempfile.TemporaryDirectory() as tmp:
            rm = SqliteReadModel(Path(tmp) / "test.db")
            rm._enabled = True
            assert rm.query_enabled is False, "query_enabled must be False without env flag"
            rm.close()
            rm2 = SqliteReadModel(Path(tmp) / "test2.db")
            rm2._enabled = False
            os.environ["OPENBOX_ENABLE_SQLITE_QUERY"] = "1"
            try:
                assert is_query_enabled() is True
                assert rm2.query_enabled is False, "query_enabled needs read flag too"
            finally:
                os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)
            rm2.close()
    finally:
        if old is None:
            os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)
        else:
            os.environ["OPENBOX_ENABLE_SQLITE_QUERY"] = old


def test_parse_optional_bool():
    """parse_optional_bool() maps query-string flags to True/False/None."""
    from pkg.state.sqlite_readmodel import parse_optional_bool
    assert parse_optional_bool(None) is None
    for raw in ("1", "true", "True", "TRUE", "yes", "on"):
        assert parse_optional_bool(raw) is True, raw
    for raw in ("0", "false", "False", "FALSE", "no", "off"):
        assert parse_optional_bool(raw) is False, raw
    assert parse_optional_bool("banana") is None
    assert parse_optional_bool("") is None


def test_apply_json_filters():
    """apply_json_filters() mirrors sqlite query() filter semantics."""
    from pkg.state.sqlite_readmodel import apply_json_filters
    state = _s1_state(60)
    got = apply_json_filters(
        state["games"], platform="PC", genre="Action",
        favorite=True, hidden=False, installed=True,
    )
    assert [g["game_id"] for g in got] == [g["game_id"] for g in _expected_filtered(state)]
    assert len(apply_json_filters(state["games"])) == 60
    assert apply_json_filters(state["games"], platform="Nope") == []
    assert all(g["platform"] == "Steam" for g in apply_json_filters(state["games"], platform="Steam"))
    assert all(not g["hidden"] for g in apply_json_filters(state["games"], hidden=False))
    assert "not a dict" not in apply_json_filters(["not a dict"] + state["games"][:1])


def test_parity_mismatch_message_counts_only():
    """Mismatch message carries signature + counts, never per-game ids."""
    from pkg.state.sqlite_readmodel import parity_mismatch_message
    msg = parity_mismatch_message((1, 2, 3), 10, 60)
    assert "json_count=10" in msg
    assert "sqlite_count=60" in msg
    assert "(1, 2, 3)" in msg
    assert "s1-" not in msg


def test_log_parity_mismatch_warns_counts_only():
    """Mismatch logs one warning with counts and no per-game dump."""
    from pkg.state.sqlite_readmodel import log_parity_mismatch
    with _capture_sqlite_logs() as cap:
        msg = log_parity_mismatch((4, 5, 6), 10, 60, details=["s1-0001", "s1-0002"])
    warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
    assert warnings, "expected a parity-mismatch warning"
    text = " ".join(r.getMessage() for r in warnings)
    assert "json_count=10" in text
    assert "sqlite_count=60" in text
    assert "s1-0001" not in text, "warning must be counts-only without verbose flag"
    assert msg in text


def test_log_parity_verbose_includes_details():
    """OPENBOX_SQLITE_PARITY_LOG=1 reveals per-game details (escape hatch)."""
    from pkg.state.sqlite_readmodel import log_parity_mismatch
    old = os.environ.get("OPENBOX_SQLITE_PARITY_LOG")
    os.environ["OPENBOX_ENABLE_SQLITE_QUERY"] = "0"  # unrelated flag untouched
    os.environ["OPENBOX_SQLITE_PARITY_LOG"] = "1"
    try:
        with _capture_sqlite_logs() as cap:
            log_parity_mismatch((7, 8, 9), 10, 60, details=["s1-0001", "s1-0002"])
        text = " ".join(r.getMessage() for r in cap.records)
        assert "s1-0001" in text, "verbose flag must reveal per-game details"
    finally:
        if old is None:
            os.environ.pop("OPENBOX_SQLITE_PARITY_LOG", None)
        else:
            os.environ["OPENBOX_SQLITE_PARITY_LOG"] = old
        os.environ.pop("OPENBOX_ENABLE_SQLITE_QUERY", None)


def test_filtered_query_single_ensure_fresh():
    """Filtered queries reuse one ensure_fresh call (no double-invalidate)."""
    with tempfile.TemporaryDirectory() as tmp:
        rm = SqliteReadModel(Path(tmp) / "test.db")
        rm._enabled = True
        state = _s1_state(30)
        calls = []
        orig = rm.ensure_fresh

        def counting(state_arg, sig):
            calls.append(sig)
            return orig(state_arg, sig)

        rm.ensure_fresh = counting
        try:
            got = rm.filtered_query(state, (9, 9, 9), platform="PC", limit=5)
        finally:
            rm.ensure_fresh = orig
        assert len(calls) == 1, "expected exactly one ensure_fresh call, got %d" % len(calls)
        assert all(g["platform"] == "PC" for g in got)
        assert len(got) == 5
        rm.close()


def test_facet_handler_observability_fields():
    """Enabled facets carry source/parity_ok/timings_ms (additive)."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (11, 22, 33), enabled=True):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_explorer_facets(handler, _Parsed("field=platform"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["parity_ok"] is True
        assert payload["field"] == "platform"
        assert isinstance(payload["facets"], list) and payload["facets"]
        timings = payload["timings_ms"]
        assert set(timings.keys()) == {"sqlite", "json"}
        assert timings["sqlite"] >= 0.0


def test_facet_handler_parity_mismatch_fallback():
    """Parity mismatch falls back to JSON with warning (counts-only)."""
    import handlers.library as libmod
    from parity_filter_presets import explorer_facets
    state = _s1_state(60)
    with _patched_handler_env(state, (1, 2, 3), enabled=True) as rm:
        rm.ensure_fresh(state, (1, 2, 3))
        # Same signature but swapped games => stale projection, mismatch.
        state["games"] = _s1_state(10)["games"]
        with _capture_sqlite_logs() as cap:
            handler = _StubHandler()
            libmod.LibraryHandlers._api_get_api_explorer_facets(handler, _Parsed("field=platform"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "json"
        assert payload["parity_ok"] is False
        assert "timings_ms" in payload
        assert payload["facets"] == explorer_facets(state["games"], "platform")
        warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
        assert warnings, "expected a parity-mismatch warning"
        text = " ".join(r.getMessage() for r in warnings)
        assert "json_count=10" in text
        assert "sqlite_count=60" in text
        assert "s1-" not in text, "warning must be counts-only without verbose flag"


def test_facet_handler_verbose_mismatch_details():
    """Verbose flag reveals mismatch ids on the facet path."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (2, 3, 4), enabled=True, parity_env="1") as rm:
        rm.ensure_fresh(state, (2, 3, 4))
        state["games"] = _s1_state(10)["games"]
        with _capture_sqlite_logs() as cap:
            handler = _StubHandler()
            libmod.LibraryHandlers._api_get_api_explorer_facets(handler, _Parsed("field=genre"))
        status, payload = handler.captured[0]
        assert payload["source"] == "json"
        assert payload["parity_ok"] is False
        text = " ".join(r.getMessage() for r in cap.records)
        assert "s1-" in text, "verbose flag must reveal mismatch ids"


def test_facet_handler_flag_off_byte_identical():
    """Flag-off facets return exactly the legacy shape (byte-identical)."""
    import handlers.library as libmod
    from parity_filter_presets import explorer_facets
    state = _s1_state(30)
    with _patched_handler_env(state, (7, 7, 7), enabled=False):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_explorer_facets(handler, _Parsed("field=genre"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert set(payload.keys()) == {"field", "facets"}
        assert payload == {"field": "genre", "facets": explorer_facets(state["games"], "genre")}


def test_search_handler_observability_fields():
    """Enabled search carries source/parity_ok/timings_ms (additive)."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (21, 22, 23), enabled=True):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(handler, _Parsed("q=S1+Game+5&limit=50"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["parity_ok"] is True
        assert payload["count"] == len(payload["results"])
        assert payload["results"], "expected sqlite text search hits"
        timings = payload["timings_ms"]
        assert set(timings.keys()) == {"sqlite", "json"}


def test_search_handler_parity_mismatch_fallback():
    """Search parity mismatch falls back to JSON title match + warning."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (5, 5, 5), enabled=True) as rm:
        rm.ensure_fresh(state, (5, 5, 5))
        state["games"] = _s1_state(10)["games"]
        with _capture_sqlite_logs() as cap:
            handler = _StubHandler()
            libmod.LibraryHandlers._api_get_api_v2_library_search(handler, _Parsed("q=s1+game&limit=50"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "json"
        assert payload["parity_ok"] is False
        assert "timings_ms" in payload
        assert payload["results"], "JSON fallback must still match titles"
        assert all("s1 game" in str(g.get("name", "")).lower() for g in payload["results"])
        warnings = [r for r in cap.records if r.levelno >= logging.WARNING]
        assert warnings, "expected a parity-mismatch warning"
        text = " ".join(r.getMessage() for r in warnings)
        assert "s1-" not in text


def test_search_handler_flag_off_byte_identical():
    """Flag-off search returns exactly the legacy 3-key shape."""
    import handlers.library as libmod
    state = _s1_state(30)
    with _patched_handler_env(state, (8, 8, 8), enabled=False):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(handler, _Parsed("q=S1+Game+1&limit=50"))
        assert len(handler.captured) == 1
        status, payload = handler.captured[0]
        assert status == 200
        assert set(payload.keys()) == {"results", "source", "count"}
        assert payload["source"] == "json"
        assert payload["count"] == len(payload["results"])
        assert payload["results"]


def test_search_handler_flag_off_with_filters():
    """Flag-off search honors filter params via the JSON path (old shape)."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (80, 80, 80), enabled=False):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(
            handler, _Parsed("platform=Steam&limit=100"))
        status, payload = handler.captured[0]
        assert set(payload.keys()) == {"results", "source", "count"}
        assert payload["source"] == "json"
        assert payload["results"]
        assert all(g["platform"] == "Steam" for g in payload["results"])


def test_search_handler_empty_query_unchanged():
    """Empty query without filters keeps the exact legacy response."""
    import handlers.library as libmod
    state = _s1_state(10)
    with _patched_handler_env(state, (9, 9, 9), enabled=True, query_env="1"):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(handler, _Parsed("q=+&limit=50"))
        assert handler.captured[0][1] == {"results": [], "source": "json"}


def test_search_handler_query_flag_filtered():
    """QUERY=1 serves structured filters from sqlite with limit/offset."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (31, 32, 33), enabled=True, query_env="1") as rm:
        calls = []
        orig = rm.ensure_fresh

        def counting(state_arg, sig):
            calls.append(sig)
            return orig(state_arg, sig)

        rm.ensure_fresh = counting
        try:
            handler = _StubHandler()
            libmod.LibraryHandlers._api_get_api_v2_library_search(
                handler,
                _Parsed("platform=PC&favorite=true&installed=true&limit=5&offset=0"),
            )
        finally:
            rm.ensure_fresh = orig
        assert len(calls) == 1, "expected one ensure_fresh per request, got %d" % len(calls)
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["parity_ok"] is True
        pool = [g for g in state["games"]
                if g["platform"] == "PC" and g["favorite"] and g["installed"]]
        pool.sort(key=lambda game: game["title"])
        assert [g["game_id"] for g in payload["results"]] == [g["game_id"] for g in pool[:5]]
        assert len(payload["results"]) == 2
        # hidden filter participates end to end.
        handler_hidden = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(
            handler_hidden, _Parsed("hidden=false&limit=100"))
        _, payload_hidden = handler_hidden.captured[0]
        assert len(payload_hidden["results"]) == 54
        assert all(not g["hidden"] for g in payload_hidden["results"])
        # Garbage booleans are ignored (treated as absent).
        handler2 = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(
            handler2, _Parsed("platform=PC&favorite=banana&limit=100"))
        _, payload2 = handler2.captured[0]
        assert all(g["platform"] == "PC" for g in payload2["results"])
        assert len(payload2["results"]) == 20
        # Text + filters intersect on the sqlite path.
        handler3 = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(
            handler3, _Parsed("q=S1+Game+1&platform=PC&limit=100"))
        _, payload3 = handler3.captured[0]
        assert payload3["results"]
        assert all(g["platform"] == "PC" for g in payload3["results"])


def test_search_handler_query_flag_off_python_filter():
    """READ on + QUERY off honors filters via Python over sqlite results."""
    import handlers.library as libmod
    state = _s1_state(60)
    with _patched_handler_env(state, (41, 42, 43), enabled=True, query_env=None):
        handler = _StubHandler()
        libmod.LibraryHandlers._api_get_api_v2_library_search(
            handler, _Parsed("platform=GOG&installed=true&limit=100"))
        status, payload = handler.captured[0]
        assert status == 200
        assert payload["source"] == "sqlite"
        assert payload["parity_ok"] is True
        assert "timings_ms" in payload
        assert payload["results"]
        assert all(g["platform"] == "GOG" and g["installed"] for g in payload["results"])


def test_transact_single_invalidate():
    """transact_state() invalidates the read model exactly once per write."""
    import pkg.state.cache as cache_mod
    calls = []

    class _CountingRM:
        def invalidate(self):
            calls.append(1)

    orig_rm = cache_mod.SQLITE_READ_MODEL
    orig_update = cache_mod.update_state_with_result
    orig_epoch = cache_mod.CACHE_EPOCH._invalidate_all
    orig_clear = cache_mod.clear_discovery_cache
    fake_state = {"games": []}
    try:
        cache_mod.SQLITE_READ_MODEL = _CountingRM()
        cache_mod.update_state_with_result = lambda mutator: (fake_state, mutator(fake_state))
        cache_mod.CACHE_EPOCH._invalidate_all = lambda **kwargs: None
        cache_mod.clear_discovery_cache = lambda: None
        try:
            import webapp_state as webapp_state_mod
            has_ws = True
        except ImportError:
            webapp_state_mod = None
            has_ws = False
        orig_ws_update = None
        if has_ws:
            orig_ws_update = webapp_state_mod.update_state_with_result
            webapp_state_mod.update_state_with_result = cache_mod.update_state_with_result
        try:
            cache_mod.transact_state(lambda state: "marker")
        finally:
            if has_ws:
                webapp_state_mod.update_state_with_result = orig_ws_update
    finally:
        cache_mod.SQLITE_READ_MODEL = orig_rm
        cache_mod.update_state_with_result = orig_update
        cache_mod.CACHE_EPOCH._invalidate_all = orig_epoch
        cache_mod.clear_discovery_cache = orig_clear
    assert calls == [1], "expected exactly one invalidate per transact, got %r" % (calls,)


def run_all_tests():
    tests = [
        test_rebuild_and_count,
        test_query_by_platform,
        test_query_favorite,
        test_search_fts,
        test_search_like_fallback,
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
        test_query_combined_filters,
        test_query_flag_defaults_off,
        test_parse_optional_bool,
        test_apply_json_filters,
        test_parity_mismatch_message_counts_only,
        test_log_parity_mismatch_warns_counts_only,
        test_log_parity_verbose_includes_details,
        test_filtered_query_single_ensure_fresh,
        test_facet_handler_observability_fields,
        test_facet_handler_parity_mismatch_fallback,
        test_facet_handler_verbose_mismatch_details,
        test_facet_handler_flag_off_byte_identical,
        test_search_handler_observability_fields,
        test_search_handler_parity_mismatch_fallback,
        test_search_handler_flag_off_byte_identical,
        test_search_handler_flag_off_with_filters,
        test_search_handler_empty_query_unchanged,
        test_search_handler_query_flag_filtered,
        test_search_handler_query_flag_off_python_filter,
        test_transact_single_invalidate,
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
