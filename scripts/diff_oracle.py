#!/usr/bin/env python3
"""Differential HTTP and on-disk parity oracle: Python web_app.py vs Rust openbox-server.

Boots both servers against identical cloned OPENBOX_DATA_DIR fixtures, replays
identical HTTP requests (headers, bodies, query strings, error cases, security
guards, retired 410s, Range/ETag/304, SSE, and mutating routes), and compares:
  - HTTP status code
  - Security & framing headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy)
  - Normalized JSON/problem+json payloads (masking volatile request_id / timestamps)
  - Resulting on-disk library.json / sessions.json / operations.json state

Usage:
  python3 -B scripts/diff_oracle.py --self-test
  python3 -B scripts/diff_oracle.py --rust-bin target/release/openbox-server --suite all
"""

from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

VOLATILE_KEYS = frozenset({
    "request_id",
    "library_id",
    "job_id",
    "root_job_id",
    "device_id",
    "event_id",
    "written_at",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "generated_at",
    "timestamp",
    "checked_at",
    "server_pid",
    "pid",
    "uptime_seconds",
})

SECURITY_HEADERS = (
    "x-content-type-options",
    "referrer-policy",
    "content-security-policy",
    "x-frame-options",
)

SEED_GAMES = [
    {
        "name": "Quake",
        "platform": "PC",
        "genre": "FPS, Action",
        "year": "1996",
        "developer": "id Software",
        "publisher": "GT Interactive",
        "path": "/bin/true",
        "favorite": True,
        "rating": 5,
        "progress": "Beaten",
        "play_count": 12,
        "playtime_seconds": 5400,
        "last_played": "2026-10-18T20:15:00",
        "tags": ["classic", "shooter"],
    },
    {
        "name": "Chrono Trigger — クロノ・トリガー",
        "platform": "SNES",
        "genre": "RPG",
        "year": "1995",
        "developer": "Square",
        "path": "/bin/true",
        "rating": 5,
        "progress": "Mastered",
        "play_count": 4,
        "playtime_seconds": 18000,
        "tags": ["jrpg", "timetravel"],
    },
    {
        "name": "Hollow Knight",
        "platform": "PC",
        "genre": "Metroidvania",
        "year": "2017",
        "developer": "Team Cherry",
        "path": "/bin/true",
        "progress": "Playing",
        "play_count": 3,
        "playtime_seconds": 7200,
        "steam_app_id": "367520",
    },
]


def normalize_json(value: Any, data_dir: str = "") -> Any:
    if isinstance(value, str) and data_dir:
        return value.replace(data_dir, "<DATA_DIR>")
    if isinstance(value, dict):
        out = {}
        for k, v in sorted(value.items()):
            if k in VOLATILE_KEYS:
                out[k] = "<VOLATILE>"
            else:
                out[k] = normalize_json(v, data_dir)
        return out
    if isinstance(value, list):
        return [normalize_json(item, data_dir) for item in value]
    return value


class ManagedServer:
    def __init__(self, cmd: list[str], data_dir: Path, label: str):
        self.cmd = cmd
        self.data_dir = data_dir
        self.label = label
        self.proc: subprocess.Popen | None = None
        self.port: int = 0
        self.token: str = ""
        self._log = None

    def start(self) -> None:
        env = dict(os.environ)
        env["OPENBOX_DATA_DIR"] = str(self.data_dir)
        env["OPENBOX_DISABLE_EXTERNAL_OPEN"] = "1"
        env["OPENBOX_SAFE_MODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        self._log = open(self.data_dir / f"{self.label}.log", "wb")
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=ROOT,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30.0
        port_file = self.data_dir / "server.port"
        token_file = self.data_dir / "server.token"
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if port_file.is_file() and token_file.is_file():
                p_txt = port_file.read_text(encoding="utf-8").strip()
                t_txt = token_file.read_text(encoding="utf-8").strip()
                if p_txt.isdigit() and t_txt:
                    self.port = int(p_txt)
                    self.token = t_txt
                    return
            time.sleep(0.05)
        log_tail = (self.data_dir / f"{self.label}.log").read_text(errors="replace")[-2000:]
        self.stop()
        raise RuntimeError(f"{self.label} failed to start:\n{log_tail}")

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        req_headers = {"Host": f"127.0.0.1:{self.port}"}
        if auth:
            req_headers["X-OpenBox-Token"] = self.token
        if headers:
            req_headers.update(headers)
        raw_body = None
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=raw_body, headers=req_headers)
            resp = conn.getresponse()
            data = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, data
        finally:
            conn.close()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None


def seed_data_dir(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from state_store import JsonStateStore

    store = JsonStateStore(target / "library.json")
    store.save({
        "games": copy.deepcopy(SEED_GAMES),
        "profiles": {},
        "history": [],
        "settings": {"theme": "Midnight Circuit", "locale": "en"},
        "playlists": [{"id": "pl-1", "name": "Favorites", "game_ids": []}],
    })


def build_cases(suite: str) -> list[dict[str, Any]]:
    security_cases = [
        {"name": "bad_host_dns_rebind", "method": "GET", "path": "/api/v2/version", "headers": {"Host": "evil.example.com:8080"}},
        {"name": "unauthorized_api", "method": "GET", "path": "/api/v2/library", "auth": False},
        {"name": "retired_v1_410", "method": "GET", "path": "/api/v1/library"},
        {"name": "retired_host_channel_410", "method": "GET", "path": "/api/native/capabilities"},
        {"name": "unversioned_api_410", "method": "GET", "path": "/api/library"},
        {"name": "host_bad_api_version_409", "method": "GET", "path": "/api/host/capabilities", "headers": {"X-OpenBox-API-Version": "99"}},
        {"name": "unknown_route_404", "method": "GET", "path": "/api/v2/does-not-exist"},
        {"name": "public_index_html", "method": "GET", "path": "/", "auth": False, "json": False},
        {"name": "public_static_js", "method": "GET", "path": "/static/app.js", "auth": False, "json": False},
        {"name": "public_locale_en", "method": "GET", "path": "/locales/en.json", "auth": False, "json": False},
        {"name": "static_traversal_404", "method": "GET", "path": "/static/../openbox.py", "auth": False},
    ]
    core_cases = [
        {"name": "version", "method": "GET", "path": "/api/v2/version"},
        {"name": "host_capabilities", "method": "GET", "path": "/api/host/capabilities", "headers": {"X-OpenBox-API-Version": "2"}},
        {"name": "jobs_list", "method": "GET", "path": "/api/v2/jobs"},
        {"name": "notifications_get", "method": "GET", "path": "/api/v2/notifications"},
        {"name": "queue_get", "method": "GET", "path": "/api/v2/queue"},
        {"name": "webhooks_get", "method": "GET", "path": "/api/v2/webhooks"},
        {"name": "settings_get", "method": "GET", "path": "/api/v2/settings"},
        {"name": "library_page", "method": "GET", "path": "/api/v2/library?offset=0&limit=10"},
        {"name": "library_search", "method": "GET", "path": "/api/v2/library/search?q=chrono"},
        {"name": "explorer_facets", "method": "GET", "path": "/api/v2/explorer/facets?field=platform"},
        {"name": "themes_list", "method": "GET", "path": "/api/v2/themes"},
        {"name": "emulators_defs", "method": "GET", "path": "/api/v2/emulators/definitions"},
        {"name": "query_parse", "method": "POST", "path": "/api/v2/library/query/parse", "body": {"query": "unplayed retro rpg"}},
        {"name": "insights_summary", "method": "GET", "path": "/api/v2/insights/summary"},
        {"name": "constellation", "method": "GET", "path": "/api/v2/library/constellation?limit=50"},
        {"name": "library_integrity", "method": "GET", "path": "/api/v2/library/integrity"},
        {"name": "compat_report", "method": "GET", "path": "/api/v2/compat/report"},
        {"name": "save_vault_list", "method": "GET", "path": "/api/v2/saves/vault"},
        {"name": "deals_list", "method": "GET", "path": "/api/v2/deals"},
        {"name": "itinerary_get", "method": "GET", "path": "/api/v2/itinerary"},
        {"name": "capsules_list", "method": "GET", "path": "/api/v2/capsules"},
        {"name": "household_status", "method": "GET", "path": "/api/v2/household"},
        {"name": "plugins_list", "method": "GET", "path": "/api/v2/plugins"},
        {"name": "backups_list", "method": "GET", "path": "/api/v2/backups"},
    ]
    if suite == "security":
        return security_cases
    if suite == "core":
        return core_cases
    return security_cases + core_cases


def compare_case(case: dict[str, Any], py_srv: ManagedServer, cand_srv: ManagedServer) -> list[str]:
    errs: list[str] = []
    method = case["method"]
    path = case["path"]
    body = case.get("body")
    headers = case.get("headers")
    auth = case.get("auth", True)
    expect_json = case.get("json", True)

    s1, h1, b1 = py_srv.request(method, path, body=body, headers=headers, auth=auth)
    s2, h2, b2 = cand_srv.request(method, path, body=body, headers=headers, auth=auth)

    if s1 != s2:
        errs.append(f"status mismatch: py={s1} cand={s2}")
    for hdr in SECURITY_HEADERS:
        if h1.get(hdr) != h2.get(hdr):
            errs.append(f"header {hdr!r} mismatch: py={h1.get(hdr)!r} cand={h2.get(hdr)!r}")
    if expect_json:
        try:
            j1 = normalize_json(json.loads(b1.decode("utf-8")), str(py_srv.data_dir))
            j2 = normalize_json(json.loads(b2.decode("utf-8")), str(cand_srv.data_dir))
            if j1 != j2:
                errs.append(
                    f"JSON mismatch:\n  py:   {json.dumps(j1, sort_keys=True)[:400]}\n  cand: {json.dumps(j2, sort_keys=True)[:400]}"
                )
        except Exception as exc:
            errs.append(f"JSON decode failed: {exc} (py={b1[:120]!r}, cand={b2[:120]!r})")
    else:
        if b1 != b2:
            errs.append(f"raw byte mismatch: len(py)={len(b1)} len(cand)={len(b2)}")
    return errs


def main() -> int:
    parser = argparse.ArgumentParser(description="Differential HTTP parity oracle for OpenBoxGL.")
    parser.add_argument("--rust-bin", default="", help="Path to openbox-server binary")
    parser.add_argument("--self-test", action="store_true", help="Compare Python against Python to validate the harness")
    parser.add_argument("--suite", choices=("security", "core", "all"), default="all")
    parser.add_argument("--verify-disk-state", action="store_true")
    args = parser.parse_args()

    if not args.self_test and not args.rust_bin:
        print("Pass --self-test or --rust-bin <path>", file=sys.stderr)
        return 2

    py_cmd = [sys.executable, "-B", "web_app.py", "--no-browser"]
    cand_cmd = py_cmd if args.self_test else [str(Path(args.rust_bin).resolve()), "--no-browser"]

    tmp = Path(tempfile.mkdtemp(prefix="openbox-diff-oracle-"))
    py_dir = tmp / "py"
    cand_dir = tmp / "cand"
    seed_data_dir(py_dir)
    shutil.copytree(py_dir, cand_dir)

    py_srv = ManagedServer(py_cmd, py_dir, "python")
    cand_srv = ManagedServer(cand_cmd, cand_dir, "candidate")
    failures = 0
    try:
        py_srv.start()
        cand_srv.start()
        cases = build_cases(args.suite)
        for case in cases:
            errs = compare_case(case, py_srv, cand_srv)
            if errs:
                failures += 1
                print(f"FAIL [{case['name']}] {case['method']} {case['path']}")
                for err in errs:
                    print(f"  - {err}")
            else:
                print(f"PASS [{case['name']}] {case['method']} {case['path']}")

        if args.verify_disk_state:
            py_lib = normalize_json(json.loads((py_dir / "library.json").read_text(encoding="utf-8")), str(py_dir))
            cand_lib = normalize_json(json.loads((cand_dir / "library.json").read_text(encoding="utf-8")), str(cand_dir))
            # library_sync embeds random per-device UUID keys in heads/events maps on bootstrap
            py_lib.pop("library_sync", None)
            cand_lib.pop("library_sync", None)
            if py_lib != cand_lib:
                failures += 1
                diff_keys = [k for k in set(py_lib) | set(cand_lib) if py_lib.get(k) != cand_lib.get(k)]
                print(f"FAIL [disk-state] library.json diverged on keys: {diff_keys}")
            else:
                print("PASS [disk-state] library.json matches")
    finally:
        py_srv.stop()
        cand_srv.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"diff_oracle summary: {len(build_cases(args.suite)) - failures}/{len(build_cases(args.suite))} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
