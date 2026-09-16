#!/usr/bin/env python3
"""Tests for smart collections (1.12.0)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pkg.parity.parity_collections import (  # noqa: E402
    EXPORT_FORMAT,
    MAX_COLLECTIONS,
    MAX_IMPORT_ITEMS,
    collections_with_counts,
    delete_collection,
    evaluate_collection,
    export_collections,
    import_collections,
    list_collections,
    save_collection,
)


def _state():
    return {
        "games": [
            {"name": "Quake", "platform": "PC", "genre": "FPS", "year": 1996,
             "progress": "Beaten", "rating": 5, "playtime_seconds": 40000},
            {"name": "Stardew Valley", "platform": "PC", "genre": "Farming",
             "progress": "Playing", "rating": 4.5, "playtime_seconds": 72000},
            {"name": "Tetris", "platform": "GB", "genre": "Puzzle",
             "progress": "", "rating": 0, "playtime_seconds": 0},
        ],
    }


def test_save_and_list():
    state = _state()
    assert save_collection(state, "Retro", "retro") == "Retro"
    assert list_collections(state) == [{"name": "Retro", "query": "retro"}]


def test_save_upserts_by_name():
    state = _state()
    save_collection(state, "Fav", "rating:4")
    save_collection(state, "Fav", "beaten")
    items = list_collections(state)
    assert len(items) == 1
    assert items[0]["query"] == "beaten"


def test_save_validates():
    state = _state()
    for bad in ({"name": "", "query": "x"}, {"name": "x", "query": ""},
                {"name": "x" * 81, "query": "x"}, {"name": "x", "query": "q" * 501}):
        try:
            save_collection(state, bad["name"], bad["query"])
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid collection {bad}")
    for i in range(MAX_COLLECTIONS):
        save_collection(state, f"c{i}", "anything")
    try:
        save_collection(state, "one too many", "x")
    except ValueError:
        pass
    else:
        raise AssertionError("collection cap not enforced")


def test_delete():
    state = _state()
    save_collection(state, "Retro", "retro")
    assert delete_collection(state, "Retro") is True
    assert delete_collection(state, "Retro") is False
    assert list_collections(state) == []


def test_evaluate_and_counts():
    state = _state()
    assert evaluate_collection(state, "beaten") == 1
    assert evaluate_collection(state, "platform:pc") == 2
    assert evaluate_collection(state, "no-such-grammar-token-zzz") >= 0
    save_collection(state, "PC", "platform:pc")
    save_collection(state, "Done", "beaten")
    counts = {item["name"]: item["count"] for item in collections_with_counts(state)}
    assert counts == {"PC": 2, "Done": 1}


def test_export_import_round_trip():
    state = _state()
    save_collection(state, "Faves", "favorite")
    save_collection(state, "PC", "platform:pc")
    exported = export_collections(state)
    assert exported["format"] == EXPORT_FORMAT
    assert exported["collections"] == list_collections(state)

    target = _state()
    result = import_collections(target, exported["collections"])
    assert result == {"imported": 2, "skipped": 0, "total": 2}
    assert list_collections(target) == exported["collections"]


def test_import_upserts_replaces_and_skips_invalid_rows():
    state = _state()
    save_collection(state, "Faves", "favorite")
    save_collection(state, "Old", "retro")
    result = import_collections(state, [
        {"name": "Faves", "query": "platform:pc"},
        {"name": "", "query": "x"},
        "not-a-dict",
    ])
    assert result["imported"] == 1
    assert result["skipped"] == 2
    assert list_collections(state) == [
        {"name": "Faves", "query": "platform:pc"},
        {"name": "Old", "query": "retro"},
    ]

    replace = _state()
    save_collection(replace, "Old", "retro")
    result = import_collections(replace, [{"name": "New", "query": "beaten"}], replace=True)
    assert result["imported"] == 1
    assert list_collections(replace) == [{"name": "New", "query": "beaten"}]

    try:
        import_collections(state, [{"name": f"c{i}", "query": "x"} for i in range(MAX_IMPORT_ITEMS + 1)])
    except ValueError:
        pass
    else:
        raise AssertionError("import cap not enforced")


if __name__ == "__main__":
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        fn()
        print(f"ok {name}")
    print("all tests passed")
