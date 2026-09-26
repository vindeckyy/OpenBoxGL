"""Signed update channel for emulator definitions (ADR 0060).

24 definitions ship in ``emulator_defs/``, but there was no way to receive a
*newer* one: a user on a fresh Dolphin or PCSX2 build had to hand-edit YAML.
This module is that channel.

Design constraints, all of them learned from the 1.12.1 hardening that shipped
four verifier bugs in one release:

- **Never re-implement verification.** Signature checking is delegated to
  ``updates.verify_artifact``, which already rejects small-order keys, fails
  closed on a malformed signature payload, and normalizes the digest before
  comparison. A second verifier is a second set of bugs.
- **Never partially apply.** The pack is fetched, verified, fully parsed, and
  fully validated *before* anything is written. A pack that fails at step n
  leaves the installed definitions exactly as they were.
- **Bundled definitions are never overwritten.** The pack installs into the
  per-user data directory and the loader prefers that directory, so the shipped
  set stays the always-valid fallback.
- **Local edits win.** A definition already present in the data directory is
  never replaced, so a hand-tuned ``native_exe`` survives an update.
- **A bad signature is a security event**, not a generic failure: it raises
  ``SignatureError`` so the caller can raise a notification.

Dependency-free: stdlib plus the existing release key.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from updates import verify_artifact

# The pack lives beside the release key's repository, pinned to an immutable
# commit exactly as plugin_catalog.py pins its catalog URL. Pinning a commit
# rather than a branch is the point: the bytes cannot change under us.
PACK_BASE = (
    "https://raw.githubusercontent.com/vindeckyy/OpenBoxGL/"
    "566f57e276cd5fffb587675c970bc86f50dfbccb/emulator_defs"
)
PACK_INDEX = f"{PACK_BASE}/index.json"
PACK_ARCHIVE = f"{PACK_BASE}/community-defs.tar.gz"
MAX_PACK_BYTES = 8 * 1024 * 1024
MAX_DEFINITION_BYTES = 256 * 1024
MAX_DEFINITIONS = 400
FETCH_TIMEOUT = 20

# Every definition must carry these to be usable cross-platform. ADR 0048 added
# native_exe_windows and the emulator-defs gate enforces it, so the update
# channel enforces the same list or it could install a definition the gate
# would have rejected.
REQUIRED_KEYS = (
    "emulator_id",
    "adapter_id",
    "label",
    "platform",
    "extensions",
    "executable_patterns",
    "native_exe",
    "native_exe_windows",
    "flatpak_app_id",
    "priority",
    "startup_args",
    "state",
    "schema_version",
    "recommended",
)

_TOPLEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):")


class DefinitionError(ValueError):
    """A pack is malformed or incomplete. Not a security event."""


class SignatureError(DefinitionError):
    """The pack failed Ed25519 verification. Treated as a security event."""


def _repo_root() -> Path:
    candidate = Path(__file__).resolve().parent
    for parent in (candidate, *candidate.parents):
        if (parent / "emulator_defs").is_dir():
            return parent
    return candidate.parent


ROOT = _repo_root()
BUNDLED_DEFS = ROOT / "emulator_defs"
RELEASE_KEY = ROOT / "openbox-release.pub"
STATE_FILE = "emulator_defs_update.json"


def local_defs_dir(data_dir=None) -> Path:
    """Return the per-user definition directory that shadows the bundled set."""
    if data_dir is None:
        override = os.environ.get("OPENBOX_DATA_DIR")
        if override:
            return Path(override) / "emulator_defs"
        from pkg.platform_compat import default_data_dir

        return default_data_dir() / "emulator_defs"
    return Path(data_dir) / "emulator_defs"


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment that is not inside quotes."""
    out, quote = [], None
    for char in line:
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out)


def _fetch(url: str, opener=urlopen) -> bytes:
    request = Request(url, headers={"User-Agent": "OpenBox/1.14.1 definition channel"})
    with opener(request, timeout=FETCH_TIMEOUT) as response:
        payload = response.read(MAX_PACK_BYTES + 1)
    if len(payload) > MAX_PACK_BYTES:
        raise DefinitionError("The definition pack is larger than the allowed size.")
    return payload


def download_and_verify(opener=urlopen, pack_url=None, key_file=None) -> bytes:
    """Fetch the pack and verify it with the release key.

    Returns the verified archive bytes. Raises ``SignatureError`` when the
    signature does not check out, which the caller must surface as a security
    event rather than a routine update failure.
    """
    url = pack_url or PACK_ARCHIVE
    key_path = Path(key_file) if key_file else RELEASE_KEY
    if not key_path.is_file():
        raise SignatureError(f"The release key {key_path.name} is missing.")
    archive = _fetch(url, opener=opener)
    signature = _fetch(f"{url}.sig", opener=opener)

    with tempfile.TemporaryDirectory() as workdir:
        base = Path(workdir)
        artifact_path = base / "pack.tar.gz"
        signature_path = base / "pack.tar.gz.sig"
        artifact_path.write_bytes(archive)
        try:
            signature_path.write_text(signature.decode("utf-8"), encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SignatureError("The pack signature is not valid UTF-8.") from exc
        try:
            # Reuse the hardened verifier rather than reimplementing Ed25519.
            verify_artifact(artifact_path, signature_path, key_path)
        except Exception as exc:
            raise SignatureError(f"The definition pack signature is not valid: {exc}") from exc
    return archive


def read_pack(archive: bytes) -> dict:
    """Return {filename: yaml text} for every definition in a verified pack.

    Only regular files are considered, and absolute paths and ``..`` traversal
    are refused, so a crafted archive cannot reach outside the target directory
    or materialize a symlink.
    """
    import tarfile

    definitions = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar:
            if not member.isfile():
                # Directories, symlinks, hardlinks, and devices are all refused.
                continue
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise DefinitionError(f"The pack contains an unsafe path: {name}")
            if not name.endswith(".yaml"):
                continue
            if len(definitions) >= MAX_DEFINITIONS:
                raise DefinitionError(f"The pack holds more than {MAX_DEFINITIONS} definitions.")
            handle = tar.extractfile(member)
            if handle is None:
                continue
            basename = Path(name).name
            if basename in definitions:
                raise DefinitionError(
                    f"The pack contains two definitions named {basename} ({name})."
                )
            definitions[basename] = handle.read(MAX_DEFINITION_BYTES + 1).decode(
                "utf-8", errors="strict"
            )
    if not definitions:
        raise DefinitionError("The pack contains no definitions.")
    return definitions


def _read_state(data_dir: Path) -> dict:
    path = data_dir / STATE_FILE
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(data_dir: Path, payload: dict) -> None:
    """Persist the install ledger atomically so rollback knows what it owns."""
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / STATE_FILE
    handle, tmp = tempfile.mkstemp(prefix=state_path.name + ".", dir=data_dir)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
        os.replace(tmp, state_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def check(opener=urlopen, data_dir=None) -> dict:
    """Report whether a newer pack exists, without installing anything."""
    target = local_defs_dir(data_dir)
    state = _read_state(target)
    installed = str(state.get("version", ""))
    try:
        raw = _fetch(PACK_INDEX, opener=opener)
        index = json.loads(raw.decode("utf-8"))
    except (URLError, OSError, ValueError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "error": f"Could not reach the definition index: {exc}",
            "installed_version": installed,
        }
    if not isinstance(index, dict):
        # A non-object payload fails closed rather than being coerced.
        return {
            "ok": False,
            "error": "The definition index is not an object.",
            "installed_version": installed,
        }
    latest = str(index.get("version", ""))
    return {
        "ok": True,
        "version": latest,
        "installed_version": installed,
        "update_available": bool(latest) and latest != installed,
        "notes": str(index.get("notes", ""))[:500],
    }


def status(data_dir=None) -> dict:
    """Local definition state: what is installed and what is user-owned."""
    target = local_defs_dir(data_dir)
    state = _read_state(target)
    local_files = sorted(p.name for p in target.glob("*.yaml")) if target.is_dir() else []
    channel_owned = set(state.get("installed", []))
    bundled = sorted(p.name for p in BUNDLED_DEFS.glob("*.yaml")) if BUNDLED_DEFS.is_dir() else []
    return {
        "ok": True,
        "version": str(state.get("version", "")),
        "dir": str(target),
        "installed": sorted(channel_owned),
        "kept_local": list(state.get("kept_local", [])),
        "local_definitions": local_files,
        "bundled_definitions": bundled,
        # Files the user shadowed by hand: local files matching a bundled name
        # that the channel did not install (channel-owned files are not user
        # edits).
        "locally_modified": sorted(set(local_files) & set(bundled) - channel_owned),
    }


def install(opener=urlopen, data_dir=None, key_file=None) -> dict:
    """Fetch, verify, validate, and install the pack. All-or-nothing.

    A user's own definitions are preserved: a filename already in the data
    directory that the channel does not own is never overwritten, because
    that file is a deliberate edit. A file the channel does own *is*
    refreshed, and one this pack no longer ships is removed, so the local
    directory converges on the pack instead of accumulating every version
    ever installed.
    """
    target = local_defs_dir(data_dir)
    archive = download_and_verify(opener=opener, key_file=key_file)
    definitions = read_pack(archive)
    # Validate every definition before writing anything, so a bad pack cannot
    # install half of itself.
    for name, text in sorted(definitions.items()):
        validate_definition(name, text)

    version = _index_version(opener=opener)
    previous = _read_state(target)
    channel_owned = set(previous.get("installed", []))
    kept, installed, updated, removed = [], [], [], []
    target.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(definitions.items()):
        destination = target / name
        if destination.exists():
            if name in channel_owned:
                # The channel owns this file: a newer pack refreshes it.
                # A user-edited copy under a channel-owned name is refreshed
                # too — the rollback ledger already records that this file is
                # not the user's.
                destination.write_text(text, encoding="utf-8")
                updated.append(name)
                continue
            # A user's own definition wins: never clobber a deliberate edit.
            kept.append(name)
            continue
        destination.write_text(text, encoding="utf-8")
        installed.append(name)
    # A pack that no longer ships a definition the channel installed has
    # retracted it. Leaving the file in place would keep it shadowing the
    # bundled set until a full rollback, which also removes everything else the
    # pack installed -- so an update has to honor the retraction itself.
    for name in sorted(channel_owned - set(definitions)):
        stale = target / name
        if stale.is_file():
            stale.unlink()
            removed.append(name)


    from pkg.parity import parity_emulator_defs

    parity_emulator_defs._reset_registry_cache()

    payload = {
        "version": version or previous.get("version", ""),
        # installed is what the channel still owns on disk: new writes and
        # refreshed writes. A retracted file is no longer installed, so it
        # stops shadowing the bundled definition immediately.
        "installed": sorted(installed + updated),
        "kept_local": sorted(kept),
        "previous_version": previous.get("version", ""),
    }
    _write_state(target, payload)
    return {
        "ok": True,
        "version": payload["version"],
        "installed": sorted(installed),
        "updated": sorted(updated),
        "removed": sorted(removed),
        "kept_local": payload["kept_local"],
        "dir": str(target),
    }


def rollback(data_dir=None) -> dict:
    """Remove definitions this channel installed, restoring the bundled set.

    Only files recorded in the install state are touched, so a user's own
    definitions in the same directory are never deleted.
    """
    target = local_defs_dir(data_dir)
    state = _read_state(target)
    removed = []
    for name in state.get("installed", []):
        path = target / str(name)
        if path.is_file():
            path.unlink()
            removed.append(str(name))
    try:
        if target.is_dir() and not any(target.iterdir()):
            shutil.rmtree(target, ignore_errors=True)
    except OSError:
        pass
    _write_state(
        target, {"version": "", "installed": [], "kept_local": state.get("kept_local", [])}
    )

    from pkg.parity import parity_emulator_defs

    parity_emulator_defs._reset_registry_cache()
    return {"ok": True, "removed": sorted(removed), "dir": str(target)}

def _index_version(opener=urlopen) -> str:
    try:
        index = json.loads(_fetch(PACK_INDEX, opener=opener).decode("utf-8"))
    except (URLError, OSError, ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(index, dict):
        return ""
    return str(index.get("version", ""))



def definition_keys(text: str) -> set:
    """Top-level keys of one definition, comments ignored."""
    keys = set()
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip() or line.startswith((" ", "\t", "-")):
            continue
        match = _TOPLEVEL_KEY_RE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def validate_definition(name: str, text: str) -> None:
    """Reject an incomplete definition before it can be installed."""
    if len(text.encode("utf-8")) > MAX_DEFINITION_BYTES:
        raise DefinitionError(f"{name} is larger than {MAX_DEFINITION_BYTES} bytes.")
    missing = [key for key in REQUIRED_KEYS if key not in definition_keys(text)]
    if missing:
        raise DefinitionError(f"{name} is missing required key(s): {', '.join(missing)}")
