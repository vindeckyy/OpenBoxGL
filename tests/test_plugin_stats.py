#!/usr/bin/env python3
"""Plugin stats provider tests (P10.3)."""

import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

from plugins import set_plugin_enabled, set_plugin_permissions  # noqa: E402
from pkg.parity import parity_plugin_stats as stats  # noqa: E402


def _make_plugin(root, *, plugin_id="vendor.stats", hooks=("stats",), capabilities=None):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    (package / "plugin.py").write_text("pass\n", encoding="utf-8")
    manifest = {
        "id": plugin_id,
        "name": "Vendor Stats",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin.py",
        "hooks": list(hooks),
        "permissions": ["network"],
        "capabilities": capabilities or {
            "stats": {"id": "vendor.stats", "name": "Vendor Stats"},
        },
    }
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    set_plugin_permissions(Path(root), plugin_id, ["network"])
    return Path(root)


def _hook(result, calls=None):
    def runner(directory, plugin_id, hook, payload, *, timeout=None):
        if calls is not None:
            calls.append((hook, payload))
        if isinstance(result, Exception):
            raise result
        return result, ""
    return runner


def _state():
    return {"games": [
        {"game_id": "g1", "name": "Game One", "platform": "PC"},
        {"game_id": "g2", "name": "Game Two", "platform": "PC"},
    ]}


def test_refresh_caches_and_loads():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        calls = []
        result = {
            "games": [
                {
                    "game_id": "g1",
                    "playtime_seconds": 3600,
                    "last_played": "2026-01-01T00:00:00Z",
                    "achievements": {
                        "source": "GOG", "name": "GOG", "earned": 3, "total": 10,
                        "items": [{"id": "a", "title": "First", "earned": True, "points": 10}],
                    },
                },
                {
                    "game_id": "g2",
                    "playtime_seconds": 10,
                    "achievements": {"source": "gog", "name": "GOG", "earned": 1, "total": 2},
                },
                {"game_id": "unknown", "playtime_seconds": 5},
            ],
        }
        with mock.patch.object(stats, "run_plugin_hook", _hook(result, calls)):
            summaries = stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
        assert calls[0][0] == "stats"
        assert calls[0][1]["action"] == "fetch"
        assert [row["game_id"] for row in calls[0][1]["games"]] == ["g1", "g2"]
        assert summaries == [{
            "source": "plugin:vendor.stats:gog",
            "name": "GOG",
            "games": 2,
            "updated_at": summaries[0]["updated_at"],
        }]
        cache = stats.cache_path("vendor.stats", data_root)
        assert cache.is_file()
        assert stat.S_IMODE(os.stat(cache).st_mode) == 0o600
        raw = json.loads(cache.read_text(encoding="utf-8"))
        assert raw["plugin_id"] == "vendor.stats"
        assert "unknown" not in raw["games"]
        assert raw["games"]["g1"]["sources"]["plugin:vendor.stats:gog"]["earned"] == 3

        loaded = stats.load_plugin_stats(data_root)
        assert set(loaded) == {"g1", "g2"}
        assert loaded["g1"]["sources"]["plugin:vendor.stats:gog"]["total"] == 10
        assert len(loaded["g1"]["sources"]["plugin:vendor.stats:gog"]["items"]) == 1
        assert loaded["g2"]["sources"]["plugin:vendor.stats:gog"]["earned"] == 1


def test_playtime_merge_is_monotonic():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        result = {"games": [
            {"game_id": "g1", "playtime_seconds": 100, "last_played": "2026-01-01T00:00:00Z"},
            {"game_id": "g1", "playtime_seconds": 50, "last_played": "2025-01-01T00:00:00Z"},
        ]}
        with mock.patch.object(stats, "run_plugin_hook", _hook(result)):
            stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
        entry = stats.load_plugin_stats(data_root)["g1"]
        assert entry["playtime_seconds"] == 100
        assert entry["last_played"] == "2026-01-01T00:00:00Z"


def test_reserved_source_rejected():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        for source in ("steam", "RetroAchievements"):
            result = {"games": [{
                "game_id": "g1",
                "achievements": {"source": source, "name": "Fake", "earned": 1, "total": 2},
            }]}
            with mock.patch.object(stats, "run_plugin_hook", _hook(result)):
                try:
                    stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
                    raise AssertionError("reserved sources must be rejected")
                except ValueError as error:
                    assert "reserved" in str(error).lower()


def test_invalid_totals_rejected():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        for achievements in (
            {"source": "gog", "earned": 1, "total": 20_000},
            {"source": "gog", "earned": 5, "total": 2},
            {"source": "gog", "earned": "three", "total": 2},
        ):
            result = {"games": [{"game_id": "g1", "achievements": achievements}]}
            with mock.patch.object(stats, "run_plugin_hook", _hook(result)):
                try:
                    stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
                    raise AssertionError(f"{achievements!r} must be rejected")
                except ValueError:
                    pass


def test_items_capped_and_cache_bounded():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        items = [{"id": f"i{index}", "title": f"Item {index}", "earned": True} for index in range(600)]
        result = {"games": [{
            "game_id": "g1",
            "achievements": {"source": "gog", "name": "GOG", "earned": 600, "total": 600, "items": items},
        }]}
        with mock.patch.object(stats, "run_plugin_hook", _hook(result)):
            stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
        loaded = stats.load_plugin_stats(data_root)["g1"]["sources"]["plugin:vendor.stats:gog"]
        assert len(loaded["items"]) == stats.MAX_ITEMS


def test_malformed_cache_is_ignored():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        _make_plugin(data_root / "plugins")
        root = data_root / "plugin-stats"
        root.mkdir()
        (root / "broken.json").write_text("{not json", encoding="utf-8")
        (root / "foreign.json").write_text(json.dumps({
            "plugin_id": "vendor.stats",
            "games": {"g1": {"sources": {
                "plugin:vendor.stats:gog": {"name": "GOG", "earned": 1, "total": 2},
                "evil": {"name": "Fake", "earned": 1, "total": 2},
            }}},
        }), encoding="utf-8")
        loaded = stats.load_plugin_stats(data_root)
        assert list(loaded["g1"]["sources"]) == ["plugin:vendor.stats:gog"]
        assert stats.plugin_stats_summaries(data_root) == []


def test_disabled_plugin_cache_is_not_merged():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        root = _make_plugin(data_root / "plugins")
        result = {"games": [{
            "game_id": "g1",
            "playtime_seconds": 120,
            "achievements": {"source": "gog", "name": "GOG", "earned": 1, "total": 2},
        }]}
        with mock.patch.object(stats, "run_plugin_hook", _hook(result)):
            stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
        assert stats.load_plugin_stats(data_root)["g1"]["playtime_seconds"] == 120
        assert stats.plugin_stats_summaries(data_root)
        set_plugin_enabled(root, "vendor.stats", False)
        assert stats.load_plugin_stats(data_root) == {}
        assert stats.plugin_stats_summaries(data_root) == []


def test_hook_failure_and_disabled_plugin():
    with TemporaryDirectory() as directory:
        data_root = Path(directory)
        root = _make_plugin(data_root / "plugins")
        with mock.patch.object(stats, "run_plugin_hook", lambda *a, **k: (None, "boom")):
            try:
                stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
                raise AssertionError("hook error must be visible")
            except ValueError as error:
                assert "boom" in str(error)
        set_plugin_enabled(root, "vendor.stats", False)
        try:
            stats.refresh_plugin_stats("vendor.stats", _state(), data_dir=data_root)
            raise AssertionError("disabled plugin must be refused")
        except ValueError as error:
            assert "disabled" in str(error).lower()
        assert stats.load_plugin_stats(data_root) == {}


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"ok {name}")
    print("test_plugin_stats.py OK")
