#!/usr/bin/env python3
"""P2-16: facet counting lives in static/worker.search.js.

The worker mirrors the server's explorer_facets contract (hidden games
excluded, comma-split genres, blank labels, count/casefold ordering) so the
UI thread can fall back to identical results without a worker.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "static" / "worker.search.js"

NODE_SCRIPT = r"""
const worker = require(process.argv[1]);
const games = JSON.parse(process.argv[2]);
const requests = JSON.parse(process.argv[3]);
const out = requests.map(([field, limit]) => worker.facetCounts(games, field, limit));
process.stdout.write(JSON.stringify(out));
"""

FIXTURE = [
    {"name": "A", "platform": "PC", "genre": "Action, RPG", "developer": "Alice", "publisher": "", "progress": "", "esrb": ""},
    {"name": "B", "platform": "PC", "genre": "Action", "developer": "Bob", "publisher": "Pub", "progress": "Playing", "esrb": "T"},
    {"name": "C", "platform": "SNES", "genre": "", "developer": "", "publisher": "", "progress": "Playing", "esrb": "T"},
    {"name": "D", "platform": "", "genre": "RPG", "developer": "Alice", "publisher": "Pub", "progress": "", "esrb": "", "hidden": True},
]


def _expected(games, field, limit=40):
    counts = {}
    for game in games:
        if game.get("hidden"):
            continue
        if field == "genre":
            for part in str(game.get("genre", "")).split(","):
                label = part.strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
        elif field == "developer":
            label = str(game.get("developer", "")).strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        elif field == "publisher":
            label = str(game.get("publisher", "")).strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        elif field == "platform":
            label = str(game.get("platform", "Unspecified")).strip() or "Unspecified"
            counts[label] = counts.get(label, 0) + 1
        elif field == "progress":
            label = str(game.get("progress", "")).strip() or "Unset"
            counts[label] = counts.get(label, 0) + 1
        elif field == "esrb":
            label = str(game.get("esrb", "")).strip() or "Unrated"
            counts[label] = counts.get(label, 0) + 1
    items = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    return [{"value": value, "count": count} for value, count in items[:limit]]


@unittest.skipIf(shutil.which("node") is None, "node is not available")
class WorkerFacetTests(unittest.TestCase):
    def _run(self, requests):
        result = subprocess.run(
            ["node", "-e", NODE_SCRIPT, str(WORKER), json.dumps(FIXTURE), json.dumps(requests)],
            capture_output=True, text=True, check=False, cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_fields_match_server_contract(self):
        requests = [["platform", 40], ["genre", 40], ["developer", 40], ["publisher", 40], ["progress", 40], ["esrb", 40]]
        results = self._run(requests)
        for (field, limit), facets in zip(requests, results, strict=False):
            self.assertEqual(facets, _expected(FIXTURE, field, limit), field)

    def test_limit_and_invalid_field(self):
        limited = self._run([["developer", 1]])[0]
        self.assertEqual(limited, _expected(FIXTURE, "developer", 1))
        self.assertEqual(self._run([["nonsense", 5]])[0], None)


if __name__ == "__main__":
    unittest.main()
