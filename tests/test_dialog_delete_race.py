#!/usr/bin/env python3
"""Reliability #22: deleting the selected game while a dialog is open.

The shipped static/dialogs.js rebind function must clear stale selectedId /
editingId / metadataGameId / contextGameId and close dialogs bound to games
that no longer exist. The function body is extracted from the shipped file
and executed under node with DOM stubs, so the test runs the real code.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pkg.parity  # noqa: F401,E402  # register flat-import finder

HARNESS = """\
import { readFileSync } from 'node:fs';
const src = readFileSync(%(dialogs)s, 'utf8');
const match = src.match(/function rebindOpenDialogsToLibrary\\(\\) \\{[\\s\\S]*?\\n\\}/);
if (!match) { console.log(JSON.stringify({ ok: false, reason: 'missing-function' })); process.exit(0); }
const scenarios = JSON.parse(readFileSync(%(cases)s, 'utf8'));
const results = [];
for (const scenario of scenarios) {
  const closed = [];
  const AppState = JSON.parse(JSON.stringify(scenario.state));
  AppState.games = scenario.games;
  const openDialogs = {};
  for (const [id, open] of Object.entries(scenario.open)) openDialogs[id] = { id, open };
  const $ = id => openDialogs[id] || null;
  const closeDialog = dialog => { closed.push(dialog.id); dialog.open = false; };
  const closeContextMenu = () => { closed.push('contextMenu'); };
  const fn = new Function('AppState', '$', 'closeDialog', 'closeContextMenu', match[0] + '; return rebindOpenDialogsToLibrary();');
  const rebound = fn(AppState, $, closeDialog, closeContextMenu);
  results.push({ closed: closed.sort(), rebound: Boolean(rebound), state: { selectedId: AppState.selectedId, editingId: AppState.editingId, metadataGameId: AppState.metadataGameId, contextGameId: AppState.contextGameId } });
}
console.log(JSON.stringify({ ok: true, results }));
"""


def _run_harness(scenarios):
    with tempfile.TemporaryDirectory() as directory:
        cases = Path(directory) / "cases.json"
        cases.write_text(json.dumps(scenarios), encoding="utf-8")
        runner = Path(directory) / "runner.mjs"
        runner.write_text(
            HARNESS % {"dialogs": json.dumps(str(ROOT / "static" / "dialogs.js")), "cases": json.dumps(str(cases))},
            encoding="utf-8",
        )
        completed = subprocess.run(["node", str(runner)], capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 0, f"node harness failed: {completed.stderr[-2000:]}"
    return json.loads(completed.stdout)


def main():
    dialogs = (ROOT / "static" / "dialogs.js").read_text(encoding="utf-8")
    assert "function rebindOpenDialogsToLibrary()" in dialogs, "dialogs.js must define rebindOpenDialogsToLibrary"
    assert "rebindOpenDialogsToLibrary" in (ROOT / "static" / "library.js").read_text(encoding="utf-8"), (
        "library.js refresh must call rebindOpenDialogsToLibrary"
    )
    assert re.search(r"app:state-refreshed.*rebindOpenDialogsToLibrary|rebindOpenDialogsToLibrary.*app:state-refreshed", dialogs, re.DOTALL), (
        "dialogs.js must rebind on app:state-refreshed"
    )

    deleted = {
        "games": [{"id": 1}],
        "state": {"selectedId": 7, "editingId": 7, "metadataGameId": 7, "contextGameId": 7},
        "open": {"gameDialog": True, "metadataDialog": True},
    }
    intact = {
        "games": [{"id": 1}],
        "state": {"selectedId": 1, "editingId": 1, "metadataGameId": 1, "contextGameId": 1},
        "open": {"gameDialog": True, "metadataDialog": True},
    }
    report = _run_harness([deleted, intact])
    assert report.get("ok"), f"rebind function missing: {report}"
    gone, kept = report["results"]

    assert gone["state"] == {"selectedId": None, "editingId": None, "metadataGameId": None, "contextGameId": None}, gone
    assert "gameDialog" in gone["closed"], gone
    assert "metadataDialog" in gone["closed"], gone
    assert "contextMenu" in gone["closed"], gone
    assert gone["rebound"] is True

    assert kept["state"] == {"selectedId": 1, "editingId": 1, "metadataGameId": 1, "contextGameId": 1}, kept
    assert kept["closed"] == [], kept

    # Backend half: resolving a dialog's stale id must raise a clean lookup
    # error, never crash on a missing selection.
    from pkg.state.launch import game_from_payload

    state = {"games": [{"game_id": "g1", "name": "Kept"}]}
    try:
        game_from_payload(state, {"game_id": "deleted-id", "id": 99})
    except (IndexError, KeyError, ValueError):
        pass
    else:
        raise AssertionError("stale dialog id must raise a clean lookup error")

    print("dialog delete-race self-test: ok")


if __name__ == "__main__":
    main()
