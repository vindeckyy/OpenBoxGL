#!/usr/bin/env python3
"""Reproducible-build check (plan W5-06).

Builds the macOS bundle twice, from a clean output each time, and compares the two archives:
their SHA-256, their member lists, and the SHA-256 of every member. The report lists which
members differ, because "the zip hashes differ" is only useful with the reason.

    python3 -B scripts/check_reproducible.py --out-dir /tmp/openbox-repro

The build is not reproducible by default: the bundle embeds build timestamps and the archive
records file modification times, so the zip hash is expected to move between builds while the
payload stays identical. The report says which of the two it is.

Exit status is 0 when the run produced a report. Use --require-identical to fail when the
payload differs; that is a gate, and the release does not use it as one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_macos_app.sh"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(output: Path, script: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    done = subprocess.run(
        ["bash", str(script), str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0 or not output.exists():
        sys.stderr.write(done.stdout[-2000:])
        sys.stderr.write(done.stderr[-2000:])
        raise SystemExit(f"the build failed with exit code {done.returncode}")


def compare(first: Path, second: Path) -> dict:
    with zipfile.ZipFile(first) as left, zipfile.ZipFile(second) as right:
        left_members = {name: left.read(name) for name in left.namelist() if not name.endswith("/")}
        right_members = {name: right.read(name) for name in right.namelist() if not name.endswith("/")}
    only_first = sorted(set(left_members) - set(right_members))
    only_second = sorted(set(right_members) - set(left_members))
    differing = sorted(
        name for name in set(left_members) & set(right_members)
        if sha256_bytes(left_members[name]) != sha256_bytes(right_members[name])
    )
    return {
        "first": {"path": str(first), "sha256": sha256_file(first), "size": first.stat().st_size},
        "second": {"path": str(second), "sha256": sha256_file(second), "size": second.stat().st_size},
        "archive_identical": sha256_file(first) == sha256_file(second),
        "payload_identical": not (only_first or only_second or differing),
        "members_only_in_first": only_first,
        "members_only_in_second": only_second,
        "members_with_different_content": differing,
        "member_count": len(set(left_members) | set(right_members)),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="/tmp/openbox-repro")
    parser.add_argument("--build-script", default=str(BUILD_SCRIPT))
    parser.add_argument("--report", default="")
    parser.add_argument("--require-identical", action="store_true")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    script = Path(args.build_script)
    first = out_dir / "build-1.zip"
    second = out_dir / "build-2.zip"
    build(first, script)
    build(second, script)
    report = compare(first, second)
    report["build_script"] = str(script)
    text = json.dumps(report, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    if args.require_identical and not report["payload_identical"]:
        print("FAIL: the two builds differ in payload")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
