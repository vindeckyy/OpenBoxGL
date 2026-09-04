#!/usr/bin/env python3
"""Reliability #B4-residual: per-game MangoHud tri-state through the launch path.

game["mangohud"] == "on"/"off" wins over the global mangohud_enabled setting;
"inherit" (or missing) falls back to the global. Failing test first.
"""
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pkg.parity  # noqa: F401  # register flat-import finder


def _env_for(game, settings):
    from pkg.state.launch import _apply_mangohud_from_state

    with mock.patch.dict(os.environ, {}, clear=True):
        return _apply_mangohud_from_state({"settings": settings}, game)


def main():
    from pkg.state.launch import _apply_mangohud_from_state

    # Per-game on beats global off.
    assert _env_for({"mangohud": "on"}, {"mangohud_enabled": False}).get("MANGOHUD") == "1"
    # Per-game off beats global on.
    assert "MANGOHUD" not in _env_for({"mangohud": "off"}, {"mangohud_enabled": True})
    # Inherit (explicit, missing, or junk) falls back to the global.
    assert _env_for({"mangohud": "inherit"}, {"mangohud_enabled": True}).get("MANGOHUD") == "1"
    assert "MANGOHUD" not in _env_for({"mangohud": "inherit"}, {"mangohud_enabled": False})
    assert _env_for({}, {"mangohud_enabled": True}).get("MANGOHUD") == "1"
    assert "MANGOHUD" not in _env_for({"mangohud": "ludicrous"}, {"mangohud_enabled": False})
    # Legacy single-arg call still honors the global setting.
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _apply_mangohud_from_state({"settings": {"mangohud_enabled": True}}).get("MANGOHUD") == "1"

    # Launch path wires the game record through (game on + global off sets env).
    import pkg.state.launch as launch

    seen = {}

    class _FakeProcess:
        pid = 4242

        def poll(self):
            return None

    def _fake_popen(args, **kwargs):
        seen.update(kwargs)
        return _FakeProcess()

    state = {
        "games": [{"game_id": "g-mango", "name": "Mango", "path": "/bin/true", "launch": "true", "mangohud": "on"}],
        "profiles": {},
        "history": [],
        "settings": {"mangohud_enabled": False},
    }
    with mock.patch.object(launch, "load_state", return_value=state), \
         mock.patch.object(launch, "update_state", side_effect=lambda mutate: mutate(state)), \
         mock.patch("subprocess.Popen", side_effect=_fake_popen), \
         mock.patch.object(launch, "threading") as _threads:
        _threads.Thread.return_value = mock.Mock(start=mock.Mock())
        launch._LAUNCH_LEASES.clear()
        try:
            launch.start_game(0, stable_game_id="g-mango")
        finally:
            launch._LAUNCH_LEASES.clear()
    assert seen.get("env", {}).get("MANGOHUD") == "1", f"launch env missing MANGOHUD: {sorted(seen.get('env', {}))}"
    print("mangohud per-game self-test: ok")


def test_launch_lease_prune_and_release():
    """Lease housekeeping: expired claims are pruned, failed launches release."""
    import time

    import pkg.state.launch as launch

    launch._LAUNCH_LEASES.clear()
    try:
        launch._LAUNCH_LEASES["stale-game"] = ("old-launch", time.monotonic() - 10.0)
        assert launch._claim_launch_lease("fresh-game", "new-launch") is None
        assert "stale-game" not in launch._LAUNCH_LEASES
        assert launch._claim_launch_lease("fresh-game", "other-launch") == "new-launch"
        # A release for a launch_id that does not own the key is a no-op.
        launch._release_launch_lease("fresh-game", "other-launch")
        assert launch._LAUNCH_LEASES["fresh-game"][0] == "new-launch"
        launch._release_launch_lease("fresh-game", "new-launch")
        assert "fresh-game" not in launch._LAUNCH_LEASES

        # A launch that fails after claiming must release, so retry can proceed.
        state = {
            "games": [{"game_id": "g-boom", "name": "Boom", "path": "/bin/true", "launch": "true"}],
            "profiles": {},
            "history": [],
            "settings": {},
        }
        with mock.patch.object(launch, "load_state", return_value=state), \
             mock.patch.object(launch, "update_state", side_effect=lambda mutate: mutate(state)), \
             mock.patch("subprocess.Popen", side_effect=OSError("noexec")), \
             mock.patch.object(launch, "threading") as _threads:
            _threads.Thread.return_value = mock.Mock(start=mock.Mock())
            try:
                launch.start_game(0, stable_game_id="g-boom")
            except OSError:
                pass
            else:
                raise AssertionError("expected the spawn failure to propagate")
        assert "g-boom" not in launch._LAUNCH_LEASES
        # And the retry goes through instead of hitting its own stale lease.
        assert launch._claim_launch_lease("g-boom", "retry-launch") is None
    finally:
        launch._LAUNCH_LEASES.clear()
    print("launch lease housekeeping self-test: ok")


if __name__ == "__main__":
    main()
    test_launch_lease_prune_and_release()
