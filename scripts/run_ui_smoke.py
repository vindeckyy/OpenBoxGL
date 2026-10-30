#!/usr/bin/env python3
"""Cross-platform UI smoke runner (ui_smoke.sh needs bash, mktemp and /tmp).

Boots web_app.py against a throwaway data dir, seeds the same two games as
ui_smoke.sh, runs the puppeteer smoke, then repeats with --bigbox. Exit code is
non-zero if either pass fails.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_MAIN = [
    {"name": "Quake", "platform": "PC", "genre": "FPS", "year": "1996", "developer": "id Software",
     "path": "/bin/true", "favorite": True, "rating": 5, "progress": "Beaten", "play_count": 12,
     "playtime_seconds": 5400, "last_played": "2026-10-18T20:15:00"},
    {"name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "path": "/bin/true"},
]
SEED_BB = [
    {"name": "Quake", "platform": "PC", "genre": "FPS", "year": "1996", "developer": "id Software", "path": "/bin/true"},
    {"name": "Chrono Trigger", "platform": "SNES", "genre": "RPG", "year": "1995", "path": "/bin/true"},
]


def run_pass(script, extra_args, games, settings, extra_env, schema=7):
    data = Path(tempfile.mkdtemp(prefix="obx-ui-smoke-"))
    env = dict(os.environ, OPENBOX_DATA_DIR=str(data), PYTHONPATH=str(ROOT))
    log = open(data / "server.log", "wb")
    if schema == 6:
        # A v6 library: the server migrates it at startup, which is what the
        # migration gate is for.
        (data / "library.json").write_text(json.dumps({
            "schema_version": 6, "games": games, "profiles": {}, "history": [],
            "settings": settings, "playlists": [], "queue": [], "notifications": [],
            "ui_state": {}, "active_sessions": [], "trash": [], "party_decks": [],
            "emulator_settings": {}, "achievement_overrides": {},
        }), encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-B", "web_app.py", "--no-browser", *extra_args],
                              cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            if (data / "server.token").exists() and (data / "server.port").exists():
                break
            time.sleep(0.5)
        else:
            print("server did not start:", (data / "server.log").read_text(errors="replace"), file=sys.stderr)
            return 1
        if schema != 6:
            subprocess.run([sys.executable, "-B", "-c",
                            "from openbox import save_state; save_state(%r)" % {
                                "games": games, "profiles": {}, "history": [], "settings": settings, "playlists": []}],
                           cwd=ROOT, env=env, check=True)
        env.update(TOKEN=(data / "server.token").read_text().strip(),
                   PORT=(data / "server.port").read_text().strip(), **extra_env)
        return subprocess.run(["node", script], cwd=ROOT, env=env, check=False).returncode
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        shutil.rmtree(data, ignore_errors=True)


def main():
    main_rc = run_pass("scripts/ui_smoke.cjs", [], SEED_MAIN, {}, {})
    bb_rc = run_pass("scripts/ui_smoke_bigbox.cjs", ["--bigbox"], SEED_BB,
                     {"bigbox_mode": "hybrid", "bigbox_start_at_launch": True}, {"SMOKE_DEEPLINK": "bigbox"})
    migration_rc = run_pass("scripts/ui_smoke_migration.cjs", [], SEED_MAIN, {}, {}, schema=6)
    if main_rc or bb_rc or migration_rc:
        print(f"UI SMOKE FAILED (main={main_rc} bigbox={bb_rc} migration={migration_rc})", file=sys.stderr)
        return 1
    print("UI SMOKE PASSED (including Big Box boot + OSK and the migration gate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())