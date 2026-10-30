#!/usr/bin/env python3
"""Frontend endpoint contract (plan W2-05, ADR 0091).

Every literal path passed to the frontend ``api()`` helper must name a route the server
registers, after the same unversioned-to-v2 mapping that ``static/util.js`` applies.
Template paths (``${...}``) and query strings are cut at the first ``${`` or ``?``; for
those the remaining prefix must lead to at least one registered route. No frontend file
may name the retired ``/api/v1`` surface, and direct ``fetch`` or ``EventSource`` calls
must already be versioned or use the host channel.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes  # noqa: E402
from routes import registry  # noqa: E402

API_CALL = re.compile(r"""\bapi\(\s*[`'"](/api/[^`'"]*)""")
DIRECT_FETCH = re.compile(r"""(?:fetch|EventSource)\(\s*[`'"](/api/[^`'"]*)""")
V1_PATH = re.compile(r"/api/v1\b")


def frontend_sources():
    files = sorted((ROOT / "static").rglob("*.js"))
    index = ROOT / "index.html"
    return files + ([index] if index.is_file() else [])


def v2_path(path):
    """Mirror static/util.js apiPath(): /api/x maps to /api/v2/x; /api/v2 and /api/host pass through."""
    if path.startswith(("/api/v2/", "/api/host/")):
        return path
    return "/api/v2" + path[len("/api"):]


def live_paths():
    registry._ensure_handlers_loaded()
    paths = set(routes.GET_TABLE) | set(routes.POST_TABLE)
    paths.update(route.path for route in registry.all_routes())
    return paths


def locate(source, pattern):
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        for match in pattern.finditer(line):
            yield f"{source.relative_to(ROOT)}:{number}", match.group(1)


class FrontendEndpointContractTests(unittest.TestCase):
    def test_every_api_call_site_names_a_registered_route(self):
        known = live_paths()
        unresolved = []
        call_sites = 0
        for source in frontend_sources():
            for where, literal in locate(source, API_CALL):
                call_sites += 1
                target = v2_path(literal)
                cut = re.split(r"\?|\$\{", target, maxsplit=1)[0]
                templated = "${" in literal or "?" in literal
                found = any(path.startswith(cut) for path in known) if templated else cut in known
                if not found:
                    unresolved.append(f"{where}: {literal}")
        self.assertGreater(call_sites, 300)
        self.assertEqual(unresolved, [], "\n".join(unresolved[:20]))

    def test_no_frontend_file_names_the_retired_v1_surface(self):
        offenders = [where for source in frontend_sources() for where, _ in locate(source, re.compile(r"(/api/v1)\b"))]
        self.assertEqual(offenders, [])

    def test_direct_requests_are_versioned_or_on_the_host_channel(self):
        offenders = []
        for source in frontend_sources():
            for where, literal in locate(source, DIRECT_FETCH):
                if not literal.startswith(("/api/v2/", "/api/host/")):
                    offenders.append(f"{where}: {literal}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
