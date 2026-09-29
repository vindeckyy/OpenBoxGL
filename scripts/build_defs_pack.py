#!/usr/bin/env python3
"""Build the emulator-definition pack the update channel downloads (ADR 0061).

Produces, in --out:
  community-defs.tar.gz   every emulator_defs/*.yaml, byte-deterministic
  index.json              {"version": ..., "notes": ...} for the channel's cheap update check

This script never touches a signing key. Signing is a maintainer step that needs the private
half of openbox-release.pub; the printed command shows exactly what to run, then upload the three
files (archive, archive.sig, index.json) to the release the channel reads from
(parity_emulator_defs_update.PACK_BASE).
"""
import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build_archive(defs_dir: Path) -> bytes:
    """A gzip tarball that is identical for identical inputs: sorted names, zero mtime/uid/gid."""
    files = sorted(defs_dir.glob("*.yaml"))
    if not files:
        raise SystemExit(f"no *.yaml definitions in {defs_dir}")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(path.name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="pack version string, e.g. 2026.10.1")
    parser.add_argument("--notes", default="", help="short human-readable note shown before install")
    parser.add_argument("--defs", default=str(ROOT / "emulator_defs"))
    parser.add_argument("--out", default=str(ROOT / "build" / "defs-pack"))
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    archive = build_archive(Path(args.defs))
    (out / "community-defs.tar.gz").write_bytes(archive)
    (out / "index.json").write_text(
        json.dumps({"version": args.version, "notes": args.notes[:500]}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"built {out / 'community-defs.tar.gz'} sha256={hashlib.sha256(archive).hexdigest()}")
    print("sign it (maintainer, needs the private release key):")
    print(f"  python3 scripts/sign_release.py --key <private-key> {out / 'community-defs.tar.gz'}")
    print("then upload community-defs.tar.gz, community-defs.tar.gz.sig and index.json to the release PACK_BASE points at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
