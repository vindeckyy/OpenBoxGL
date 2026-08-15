#!/usr/bin/env python3
"""Generate a CycloneDX 1.4 SBOM for an OpenBox release.

The AppImage bundles a full Python stdlib plus the OpenBox runtime modules.
This script inventories the runtime modules from runtime_modules.txt, the
bundled data files, and (optionally) the contents of a built AppDir, so a
release can answer "what is inside this artifact" with zero new runtime
dependencies.

Usage:
  python3 scripts/gen_sbom.py --version 1.0.1 --out sbom.json
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

COMPONENT_APP = "openbox-game-launcher"
COMPONENT_STDLIB = "python-stdlib"
COMPONENT_APPIMAGE = "openbox-appimage"


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_modules():
    lines = (ROOT / "runtime_modules.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def build_sbom(version, include_stdlib=True):
    now = datetime.now(timezone.utc).isoformat()
    components = []

    # The OpenBox runtime itself.
    openbox_component = {
        "type": "application",
        "bom-ref": COMPONENT_APP,
        "name": "OpenBox Game Launcher",
        "version": version,
        "licenses": [{"license": {"id": "AGPL-3.0-only"}}],
        "properties": [
            {"name": "openbox:component", "value": "runtime"},
        ],
    }
    components.append(openbox_component)

    # Runtime modules with hashes.
    for module in runtime_modules():
        path = ROOT / module
        if not path.is_file():
            print(f"missing runtime module: {module}", file=sys.stderr)
            continue
        components.append({
            "type": "library",
            "bom-ref": f"openbox-module-{module.replace('.', '-').replace('/', '-')}",
            "name": module,
            "version": version,
            "hashes": [{"alg": "SHA-256", "content": _hash(path)}],
        })

    # Bundled UI and data files.
    for name in ("index.html", "static/app.js", "static/app.css", "openbox.svg"):
        path = ROOT / name
        if not path.is_file():
            continue
        components.append({
            "type": "file",
            "bom-ref": f"openbox-data-{name.replace('.', '-').replace('/', '-')}",
            "name": name,
            "version": version,
            "hashes": [{"alg": "SHA-256", "content": _hash(path)}],
        })

    if include_stdlib:
        # The AppImage ships the CPython stdlib wholesale; record it as one
        # component with its version rather than thousands of file rows.
        stdlib_version = ".".join(map(str, __import__("sys").version_info[:3]))
        components.append({
            "type": "library",
            "bom-ref": COMPONENT_STDLIB,
            "name": "CPython standard library",
            "version": stdlib_version,
            "licenses": [{"license": {"id": "Python-2.0"}}],
            "properties": [
                {"name": "openbox:component", "value": "bundled-stdlib"},
            ],
        })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{__import__('uuid').uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "bom-ref": COMPONENT_APPIMAGE,
                "name": "OpenBox AppImage",
                "version": version,
            },
        },
        "components": components,
    }


def main():
    parser = argparse.ArgumentParser(description="Generate a CycloneDX SBOM for OpenBox")
    parser.add_argument("--version", required=True)
    parser.add_argument("--out", default="sbom.json")
    parser.add_argument("--no-stdlib", action="store_true")
    args = parser.parse_args()
    sbom = build_sbom(args.version, include_stdlib=not args.no_stdlib)
    Path(args.out).write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out} ({len(sbom['components'])} components)")


if __name__ == "__main__":
    main()
