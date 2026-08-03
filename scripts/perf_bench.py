#!/usr/bin/env python3
"""Server-side performance benchmark for OpenBox.

Spawns the real web server against a synthetic library (see
perf_gen_library.py) and measures the core request paths:

  * /api/library wall time and payload bytes
  * /api/media wall time for a cover
  * /api/favorite mutation wall time (a full state-save cycle)

Usage:
  python3 -B scripts/perf_bench.py --base-dir /tmp/openbox-perf --sizes 100,1000,5000 \
      --out specs/verifications/perf-results.json
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.perf_gen_library import generate  # noqa: E402


def _start_server(data_dir):
    env = dict(os.environ)
    env["OPENBOX_DATA_DIR"] = str(data_dir)
    env["OPENBOX_SAFE_MODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-B", "web_app.py", "--no-browser"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = None
    line = process.stdout.readline()
    deadline = time.monotonic() + 60
    while line:
        line = line.strip()
        if line.startswith("http://127.0.0.1:"):
            url = line
            break
        if time.monotonic() > deadline:
            break
        line = process.stdout.readline()
    if not url:
        stderr = process.stderr.read()
        process.kill()
        raise RuntimeError(f"server failed to start: {stderr[:2000]}")
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    token = urllib.parse.parse_qs(parsed.query)["token"][0]
    return process, origin, token


def _request(origin, token, path, method="GET", body=None, timeout=120):
    headers = {"X-OpenBox-Token": token}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{origin}{path}", data=data, headers=headers, method=method)
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    elapsed = time.perf_counter() - start
    return elapsed, payload


def _median(times):
    return statistics.median(times)


def benchmark(data_dir):
    process, origin, token = _start_server(data_dir)
    try:
        # Warm-up: let the server populate its probe cache.
        _request(origin, token, "/api/library")

        library_times = []
        library_bytes = None
        for _ in range(3):
            elapsed, payload = _request(origin, token, "/api/library")
            library_times.append(elapsed)
            library_bytes = len(payload)

        media_times = []
        for _ in range(3):
            elapsed, _ = _request(origin, token, "/api/media?id=0&kind=cover")
            media_times.append(elapsed)

        mutation_times = []
        for _ in range(3):
            elapsed, _ = _request(origin, token, "/api/favorite", method="POST", body={"id": 0})
            mutation_times.append(elapsed)

        return {
            "library_ms": round(_median(library_times) * 1000, 1),
            "library_bytes": library_bytes,
            "media_ms": round(_median(media_times) * 1000, 1),
            "favorite_mutation_ms": round(_median(mutation_times) * 1000, 1),
        }
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", default="/tmp/openbox-perf")
    parser.add_argument("--sizes", default="1000", help="comma-separated game counts")
    parser.add_argument("--out", default="specs/verifications/perf-results.json")
    args = parser.parse_args()

    base = Path(args.base_dir)
    results = {}
    for size in [int(item) for item in args.sizes.split(",") if item.strip()]:
        data_dir = base / str(size)
        print(f"generating {size} games in {data_dir} ...", flush=True)
        generate(size, data_dir)
        print(f"benchmarking {size} games ...", flush=True)
        results[str(size)] = benchmark(data_dir)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    print(f"results written to {out_path}")


if __name__ == "__main__":
    main()
