#!/usr/bin/env python3
"""Tests for backup diff functionality (1.7.2)."""

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

from pkg.parity.parity_backup import diff_manifests, create_backup  # noqa: E402


def _make_state(n=10):
    games = []
    for i in range(n):
        games.append({
            "game_id": f"game-{i:04d}",
            "title": f"Test Game {i}",
            "platform": "PC",
            "genre": "Action",
            "rating": i % 5,
            "favorite": i % 3 == 0,
            "play_count": i * 10,
        })
    return {"games": games, "settings": {"locale": "en"}, "profiles": {}}


def test_diff_no_changes():
    """diff_manifests() should report no changes when state matches backup."""
    with tempfile.TemporaryDirectory() as tmp:
        state = _make_state(10)
        archive = create_backup(Path(tmp), state, ["library"], keep=0)
        result = diff_manifests(state, archive)
        assert result["added"] == [], f"Unexpected additions: {result['added']}"
        assert result["removed"] == [], f"Unexpected removals: {result['removed']}"
        assert result["changed"] == [], f"Unexpected changes: {result['changed']}"


def test_diff_added_games():
    """diff_manifests() should detect games added since the backup."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        # Add 5 more games to current state
        current_state = _make_state(15)
        result = diff_manifests(current_state, archive)
        assert len(result["added"]) == 5, f"Expected 5 added, got {len(result['added'])}"
        assert "game-0010" in result["added"]
        assert result["removed"] == []


def test_diff_removed_games():
    """diff_manifests() should detect games removed since the backup."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        # Remove 3 games from current state
        current_state = _make_state(7)
        result = diff_manifests(current_state, archive)
        assert len(result["removed"]) == 3, f"Expected 3 removed, got {len(result['removed'])}"
        assert "game-0007" in result["removed"]
        assert result["added"] == []


def test_diff_changed_games():
    """diff_manifests() should detect games with changed fields."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        # Modify some games in current state
        current_state = _make_state(10)
        current_state["games"][0]["title"] = "Modified Title"
        current_state["games"][1]["rating"] = 99
        result = diff_manifests(current_state, archive)
        assert len(result["changed"]) == 2, f"Expected 2 changed, got {len(result['changed'])}"
        assert "game-0000" in result["changed"]
        assert "game-0001" in result["changed"]


def test_diff_summary():
    """diff_manifests() should include a summary with counts."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        current_state = _make_state(15)
        current_state["games"][0]["title"] = "Modified"
        result = diff_manifests(current_state, archive)
        assert "summary" in result
        assert result["summary"]["added"] == 5
        assert result["summary"]["removed"] == 0
        assert result["summary"]["changed"] == 1
        assert result["summary"]["total"] == 15


def test_diff_invalid_archive():
    """diff_manifests() should raise ValueError for invalid archives."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake.zip"
        fake.write_bytes(b"not a zip file")
        try:
            diff_manifests(_make_state(5), fake)
            raise AssertionError("Should have raised ValueError")
        except ValueError:
            pass


def test_diff_empty_current():
    """diff_manifests() should handle empty current state."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        result = diff_manifests({"games": [], "settings": {}}, archive)
        assert result["added"] == []
        assert len(result["removed"]) == 10
        assert result["changed"] == []


def test_diff_empty_backup():
    """diff_manifests() should handle empty backup."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = {"games": [], "settings": {}}
        archive = create_backup(Path(tmp), backup_state, ["library"], keep=0)
        current_state = _make_state(10)
        result = diff_manifests(current_state, archive)
        assert len(result["added"]) == 10
        assert result["removed"] == []
        assert result["changed"] == []


def test_diff_route_registered():
    """The /api/v2/backup/diff route should be in routes.py."""
    content = (ROOT / "routes.py").read_text()
    assert "/api/v2/backup/diff" in content, "backup/diff route not in routes.py"


def test_diff_route_in_public_get_paths():
    """The backup diff route should not need to be in PUBLIC_GET_PATHS (it's not public)."""
    content = (ROOT / "routes.py").read_text()
    # It should be in GET_TABLE but not in PUBLIC_GET_PATHS
    assert "/api/v2/backup/diff" in content


def test_diff_handler_exists():
    """The health handler should have the diff method."""
    from handlers.health import HealthHandlers
    assert hasattr(HealthHandlers, "_api_get_api_v2_backup_diff"), \
        "HealthHandlers missing _api_get_api_v2_backup_diff"


def test_diff_settings_changed():
    """diff_manifests() should detect settings changes."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library", "settings"], keep=0)
        current_state = _make_state(10)
        current_state["settings"]["locale"] = "es"
        result = diff_manifests(current_state, archive)
        assert result["settings_changed"] is True


def test_diff_settings_unchanged():
    """diff_manifests() should report settings_changed=False when settings match."""
    with tempfile.TemporaryDirectory() as tmp:
        backup_state = _make_state(10)
        archive = create_backup(Path(tmp), backup_state, ["library", "settings"], keep=0)
        current_state = _make_state(10)
        result = diff_manifests(current_state, archive)
        assert result["settings_changed"] is False


def run_all_tests():
    tests = [
        test_diff_no_changes,
        test_diff_added_games,
        test_diff_removed_games,
        test_diff_changed_games,
        test_diff_summary,
        test_diff_invalid_archive,
        test_diff_empty_current,
        test_diff_empty_backup,
        test_diff_route_registered,
        test_diff_route_in_public_get_paths,
        test_diff_handler_exists,
        test_diff_settings_changed,
        test_diff_settings_unchanged,
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
