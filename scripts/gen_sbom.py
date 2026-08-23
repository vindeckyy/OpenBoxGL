#!/usr/bin/env python3
"""Generate a CycloneDX 1.4 SBOM for an OpenBox release.

The AppImage bundles a full Python stdlib plus the OpenBox runtime modules.
This script inventories the runtime modules from runtime_modules.txt, the
bundled data files, and (optionally) the contents of a built AppDir, so a
release can answer "what is inside this artifact" with zero new runtime
dependencies.

Usage:
  python3 scripts/gen_sbom.py --version 1.5.1 --appdir build/OpenBox.AppDir --out sbom.json
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


def artifact_components(appdir, version):
    root = Path(appdir).expanduser()
    if not root.is_dir():
        raise ValueError(f"AppDir does not exist: {root}")
    components = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            components.append({
                "type": "file",
                "bom-ref": f"openbox-artifact-{hashlib.sha256(relative.encode()).hexdigest()}",
                "name": relative,
                "version": version,
                "properties": [
                    {"name": "openbox:artifact-path", "value": relative},
                    {"name": "openbox:symlink-target", "value": str(path.readlink())},
                ],
            })
        elif path.is_file():
            components.append({
                "type": "file",
                "bom-ref": f"openbox-artifact-{hashlib.sha256(relative.encode()).hexdigest()}",
                "name": relative,
                "version": version,
                "hashes": [{"alg": "SHA-256", "content": _hash(path)}],
                "properties": [
                    {"name": "openbox:artifact-path", "value": relative},
                ],
            })
    return components


def build_sbom(version, include_stdlib=True, appdir=None):
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
    for name in ("index.html", "openbox.svg"):
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
    for static_file in sorted((ROOT / "static").glob("*.js")):
        name = static_file.relative_to(ROOT).as_posix()
        components.append({
            "type": "file",
            "bom-ref": f"openbox-data-{name.replace('.', '-').replace('/', '-')}",
            "name": name,
            "version": version,
            "hashes": [{"alg": "SHA-256", "content": _hash(static_file)}],
        })
    for static_file in sorted((ROOT / "static").glob("*.css")):
        name = static_file.relative_to(ROOT).as_posix()
        components.append({
            "type": "file",
            "bom-ref": f"openbox-data-{name.replace('.', '-').replace('/', '-')}",
            "name": name,
            "version": version,
            "hashes": [{"alg": "SHA-256", "content": _hash(static_file)}],
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

    if appdir is not None:
        components.extend(artifact_components(appdir, version))

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
    try:
        from updates import VERSION as DEFAULT_VERSION
    except ImportError:
        DEFAULT_VERSION = "1.6.0"

    parser = argparse.ArgumentParser(description="Generate a CycloneDX SBOM for OpenBox")
    parser.add_argument("pos_out", nargs="?", default=None, help="output JSON path")
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-stdlib", action="store_true")
    parser.add_argument("--appdir", type=Path, help="include every file and symlink from a built AppDir")
    args = parser.parse_args()
    out_path = args.pos_out or args.out or "sbom.json"
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    sbom = build_sbom(args.version, include_stdlib=not args.no_stdlib, appdir=args.appdir)
    out_file.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(sbom['components'])} components)")


if __name__ == "__main__":
    main()
