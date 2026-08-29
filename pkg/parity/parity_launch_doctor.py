"""Launch Doctor: validate launch readiness without spawning."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from api_errors import BadRequest, GameNotFound
from pkg.parity.parity_emulator_defs import find_adapter, resolve_launch
from pkg.parity.parity_import import detect_dependencies
from pkg.state.launch import game_from_payload

PRECEDENCE_NUMBERS = {
    "game_launch": 1,
    "game_adapter": 2,
    "platform_profile": 3,
    "registry_adapter": 4,
    "direct_exe": 5,
}

ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}
REMEDIATION_CHOOSE_ADAPTER = {"id": "choose_adapter", "label": "Choose a different adapter"}
REMEDIATION_INSTALL_FLATPAK = {"id": "install_flatpak", "label": "Install Flatpak emulator"}
REMEDIATION_INSTALL_NATIVE = {"id": "install_native", "label": "Install native emulator"}
REMEDIATION_KEEP_CUSTOM = {"id": "keep_custom", "label": "Keep custom launch command"}
REMEDIATION_SET_PATH = {"id": "set_path", "label": "Set game path"}
REMEDIATION_IMPORT_INCOMPLETE = {"id": "import_incomplete", "label": "Finish import setup"}


def _fix(kind, label, payload):
    return {"kind": kind, "label": label, "payload": dict(payload or {})}


def _fix_flatpak(app_id, native_exe=None):
    return _fix("flatpak_install", "Install Flatpak emulator", {"app_id": str(app_id or ""), "native_exe": str(native_exe or "")})


def _fix_reveal(path, name=None):
    return _fix("reveal_bios_path", "Show BIOS folder", {"path": str(path or ""), "name": str(name or "")})


def _fix_pick_core(adapter_id=None, core=None, platforms=None, candidates=None, extension=None):
    payload = {}
    if adapter_id:
        payload["adapter_id"] = str(adapter_id)
    if core:
        payload["core"] = str(core)
    if platforms:
        payload["platforms"] = list(platforms)
    if candidates:
        payload["candidates"] = list(candidates)
    if extension:
        payload["extension"] = str(extension)
    return _fix("pick_core", "Choose emulator / core", payload)


def _fix_explain(invalid_tokens=None, template=None):
    payload = {}
    if invalid_tokens:
        payload["invalid_tokens"] = list(invalid_tokens)
    if template:
        payload["template"] = str(template)
    # also include known tokens hint
    payload.setdefault("invalid_tokens", [])
    return _fix("explain_token", "Unknown token — check {path} etc", payload)


def _check(code, severity, message, remediations=None, fix_action=None):
    entry = {
        "code": code,
        "severity": severity,
        "message": message,
        "remediations": list(remediations or []),
    }
    # Every blocking (error) check must have fix_action per F4 acceptance.
    # Provide supplied fix_action or synthesize a generic one so the gate cannot miss.
    if fix_action is not None:
        entry["fix_action"] = fix_action
    elif severity == "error":
        # fallback generic explain_token to satisfy actionable gate
        entry["fix_action"] = _fix_explain(template=message)
    else:
        # warnings also get a fix_action for UI consistency if caller wants; synthesize reveal for BIOS etc?
        # Keep warnings without mandatory fix_action but add if code suggests BIOS.
        if code in ("BIOS_MISSING", "FIRMWARE_MISSING"):
            entry["fix_action"] = _fix_reveal("")
        elif code == "PLATFORM_UNKNOWN":
            entry["fix_action"] = _fix_pick_core(platforms=["NES", "SNES", "PlayStation", "GameCube"])
    return entry


def _empty_resolved():
    return {
        "emulator_id": None,
        "adapter_id": None,
        "argv_preview": [],
        "cwd": None,
        "precedence": 5,
    }


def _parse_identity(payload):
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be a JSON object.")
    game_id = payload.get("game_id")
    candidate = payload.get("candidate")
    has_game = bool(str(game_id or "").strip())
    has_candidate = isinstance(candidate, dict) and bool(candidate)
    if has_game == has_candidate:
        raise BadRequest("Provide exactly one of game_id or candidate.")
    if has_candidate:
        required = ("candidate_id", "preview_id", "path", "platform", "emulator_id", "adapter_id", "archive_member")
        missing = [key for key in required if key not in candidate]
        if missing:
            raise BadRequest(f"Candidate is missing required fields: {', '.join(missing)}.")
    return game_id if has_game else None, candidate if has_candidate else None


def _preview_path(data_dir, preview_id):
    return Path(data_dir) / "previews" / f"{preview_id}.json"


def validate_preview(preview_id, data_dir):
    preview_id = str(preview_id or "").strip()
    if not preview_id:
        raise BadRequest("preview_id is required for candidate identities.", code="PREVIEW_NOT_FOUND")
    path = _preview_path(data_dir, preview_id)
    if not path.is_file():
        raise BadRequest("Preview not found.", code="PREVIEW_NOT_FOUND")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BadRequest("Preview not found.", code="PREVIEW_NOT_FOUND") from None
    expires_at = str(payload.get("expires_at") or "").strip()
    if expires_at:
        try:
            expired = datetime.fromisoformat(expires_at) < datetime.now()
        except ValueError:
            expired = False
        if expired:
            raise BadRequest("Preview has expired.", code="PREVIEW_EXPIRED")
    return payload


def _game_from_identity(game_id, candidate, *, state, data_dir):
    if game_id:
        try:
            return game_from_payload(state, {"game_id": game_id}), game_id, None
        except (IndexError, ValueError, KeyError) as error:
            raise GameNotFound("Game not found") from error
    validate_preview(candidate["preview_id"], data_dir)
    game = {
        "name": Path(candidate["path"]).stem or "Candidate",
        "path": str(candidate["path"]),
        "platform": candidate.get("platform"),
        "emulator_id": candidate.get("emulator_id"),
        "emulator_adapter_id": candidate.get("adapter_id"),
        "archive_member": candidate.get("archive_member"),
    }
    return game, None, str(candidate["candidate_id"])


def _flatpak_installed(app_id, which, run):
    flatpak = which("flatpak") if which else shutil.which("flatpak")
    if not flatpak or not app_id:
        return False
    return run(
        [flatpak, "info", app_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _flatpak_fs_allowed(app_id, rom_path, which, run):
    flatpak = which("flatpak") if which else shutil.which("flatpak")
    if not flatpak or not app_id:
        return True
    result = run(
        [flatpak, "info", "--show-permissions", app_id],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return True
    permissions = result.stdout
    if "filesystem=host" in permissions or "filesystem=home" in permissions:
        return True
    resolved = Path(rom_path).expanduser().resolve()
    home = Path.home().resolve()
    if home == resolved or home in resolved.parents:
        return "filesystem=home" in permissions
    for line in permissions.splitlines():
        if not line.strip().startswith("filesystem="):
            continue
        grant = line.split("=", 1)[1].strip()
        if grant.endswith(":ro") or grant.endswith(":rw"):
            grant = grant.rsplit(":", 1)[0]
        try:
            if resolved == Path(grant).resolve() or Path(grant).resolve() in resolved.parents:
                return True
        except OSError:
            continue
    return False


def _retroarch_core_missing(adapter):
    startup_args = adapter.get("startup_args") or []
    for index, arg in enumerate(startup_args):
        if arg != "-L" or index + 1 >= len(startup_args):
            continue
        core = startup_args[index + 1]
        if core.startswith("/") and not Path(core).is_file():
            return core
    return None


def _archive_checks(path_value, archive_member):
    checks = []
    path = Path(path_value)
    if path.suffix.lower() not in ARCHIVE_SUFFIXES:
        return checks
    if not path.is_file():
        return checks
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except (OSError, zipfile.BadZipFile):
            checks.append(_check(
                "ARCHIVE_INVALID",
                "error",
                "Archive could not be read.",
                [REMEDIATION_SET_PATH],
                _fix_reveal(str(path_value)),
            ))
            return checks
        if archive_member:
            if archive_member not in names:
                checks.append(_check(
                    "ARCHIVE_MEMBER_INVALID",
                    "error",
                    "Archive member was not found.",
                    [REMEDIATION_SET_PATH],
                    _fix_reveal(str(path_value)),
                ))
        elif not any(name for name in names if not name.endswith("/")):
            checks.append(_check(
                "ARCHIVE_INVALID",
                "error",
                "Archive does not contain launchable files.",
                [REMEDIATION_SET_PATH],
                _fix_reveal(str(path_value)),
            ))
    return checks


def _warning_gaps(game):
    checks = []
    if not game.get("save_paths"):
        checks.append(_check("SAVE_GAP", "warning", "No save paths configured.", [REMEDIATION_IMPORT_INCOMPLETE]))
    media = game.get("cover") or game.get("screenshots")
    if not media:
        checks.append(_check("MEDIA_GAP", "warning", "No cover art or screenshots configured.", [REMEDIATION_IMPORT_INCOMPLETE]))
    if not game.get("documents"):
        checks.append(_check("DOCUMENT_GAP", "warning", "No documents configured.", [REMEDIATION_IMPORT_INCOMPLETE]))
    return checks


def _detect_invalid_tokens_in_command(cmd):
    # find {token} occurrences
    if not cmd or "{" not in cmd:
        return []
    try:
        from pkg.parity.launch_tokens import find_invalid_tokens
        return find_invalid_tokens(cmd)
    except Exception:
        return []


def _ambiguous_platform_check(game, adapter, active_adapter, launch_command, which):
    """Return AMBIGUOUS_PLATFORM check if extension is shared and needs picker."""
    path_value = str(game.get("path", "") or "").strip()
    if not path_value:
        return None
    ext = Path(path_value).suffix.lower().lstrip(".")
    if not ext:
        return None
    # Only for known emulator extensions; check if multiple adapters share ext
    try:
        from pkg.parity.parity_emulator_defs import candidates_for_extension
        candidates = candidates_for_extension(ext)
    except Exception:
        return None
    if len(candidates) <= 1:
        return None
    # If an adapter is already selected (explicit game adapter), not ambiguous
    if adapter is not None:
        return None
    # If a launch command explicitly provides a launch, not ambiguous (user override)
    if launch_command:
        return None
    # If we have an active_adapter (detected installed emulator for platform), then not ambiguous
    if active_adapter is not None:
        return None
    platform = str(game.get("platform", "") or "").strip()
    # candidate platforms
    cand_platforms = sorted({c.get("platform") for c in candidates if c.get("platform")})
    # If platform is already one of the candidates, we consider it resolved — not ambiguous
    if platform and platform in cand_platforms:
        return None
    # Generic platform or empty means ambiguous: need picker
    # Also treat "Disc image" as ambiguous for iso
    if platform and platform not in cand_platforms:
        # unknown platform mapping for this ext -> ambiguous
        pass
    # Build fix_action payload with platform chips
    # For iso, candidates are e.g., GameCube, PlayStation, PlayStation 2, PSP etc
    return _check(
        "AMBIGUOUS_PLATFORM",
        "error",
        "Platform is ambiguous for '.{}' — choose a platform.".format(ext),
        [REMEDIATION_CHOOSE_ADAPTER],
        _fix("pick_core", "Choose platform", {"extension": f".{ext}", "platforms": cand_platforms, "candidates": [c.get("adapter_id") for c in candidates]}),
    )


def _emulator_required_check(game, adapter, active_adapter, launch_command):
    """Return EMULATOR_REQUIRED if no emulator can handle this ROM."""
    if adapter is not None or active_adapter is not None:
        return None
    if launch_command:
        return None
    path_value = str(game.get("path", "") or "").strip()
    if not path_value:
        return None
    # .sh scripts can run directly, not requiring emulator
    if Path(path_value).suffix.lower() == ".sh":
        return None
    ext = Path(path_value).suffix.lower().lstrip(".")
    if not ext:
        return None
    try:
        from pkg.parity.parity_emulator_defs import candidates_for_extension
        candidates = candidates_for_extension(ext)
    except Exception:
        candidates = []
    if not candidates:
        return None
    # If we reach here, we have known extension with no adapter available -> need emulator
    cand_platforms = sorted({c.get("platform") for c in candidates if c.get("platform")})
    return _check(
        "EMULATOR_REQUIRED",
        "error",
        "No emulator is configured for this platform/extension.",
        [REMEDIATION_CHOOSE_ADAPTER],
        _fix_pick_core(platforms=cand_platforms, candidates=[c.get("adapter_id") for c in candidates], extension=f".{ext}"),
    )


def build_resolved(game, profiles, *, data_dir="", which=None):
    which = which or shutil.which
    try:
        resolved = resolve_launch(game, profiles, which=which, data_dir=data_dir)
    except (ValueError, FileNotFoundError):
        return _empty_resolved()
    adapter = find_adapter(game.get("emulator_adapter_id", ""), game.get("emulator_id", ""))
    precedence = PRECEDENCE_NUMBERS.get(resolved.get("precedence"), 5)
    return {
        "emulator_id": (adapter or {}).get("emulator_id") or game.get("emulator_id"),
        "adapter_id": (adapter or {}).get("adapter_id") or game.get("emulator_adapter_id"),
        "argv_preview": list(resolved.get("args") or []),
        "cwd": resolved.get("cwd"),
        "precedence": precedence,
    }


def run_preflight_checks(game, profiles, data_dir, *, which=None, run=None):
    which = which or shutil.which
    run = run or subprocess.run
    checks = []
    path_value = str(game.get("path", "") or "").strip()
    path = Path(path_value) if path_value else None

    if not path_value:
        checks.append(_check("PATH_MISSING", "error", "Game path is missing.", [REMEDIATION_SET_PATH], _fix_reveal("")))
        return checks
    if not path.exists():
        checks.append(_check("PATH_MISSING", "error", "Game path does not exist.", [REMEDIATION_SET_PATH], _fix_reveal(path_value)))
        return checks
    if not path.is_file():
        checks.append(_check("PATH_WRONG_TYPE", "error", "Game path is not a file.", [REMEDIATION_SET_PATH], _fix_reveal(path_value)))
        return checks

    checks.extend(_archive_checks(path_value, game.get("archive_member")))

    platform = str(game.get("platform", "") or "").strip()
    if not platform:
        checks.append(_check("PLATFORM_UNKNOWN", "warning", "Platform is unknown.", [REMEDIATION_IMPORT_INCOMPLETE], _fix_pick_core(platforms=["NES","SNES","PlayStation","GameCube","Wii","Arcade"])))

    adapter_id = str(game.get("emulator_adapter_id", "") or "").strip()
    emulator_id = str(game.get("emulator_id", "") or "").strip()
    adapter = find_adapter(adapter_id, emulator_id) if (adapter_id or emulator_id) else None
    if adapter_id and adapter is None:
        # try to provide candidates for picker
        try:
            from pkg.parity.parity_emulator_defs import _registry
            by_plat = _registry()["by_platform"].get(platform, []) if platform else []
            cand_ids = [c["adapter_id"] for c in by_plat[:5]]
        except Exception:
            cand_ids = []
        checks.append(_check(
            "ADAPTER_UNKNOWN",
            "error",
            "Selected adapter is not in the registry.",
            [REMEDIATION_CHOOSE_ADAPTER],
            _fix_pick_core(adapter_id=adapter_id, candidates=cand_ids),
        ))
    elif emulator_id and adapter is None and not str(game.get("launch", "") or "").strip():
        try:
            from pkg.parity.parity_emulator_defs import _registry
            by_emu = _registry()["by_emulator_id"].get(emulator_id, []) if emulator_id else []
            cand_ids = [c["adapter_id"] for c in by_emu[:5]]
        except Exception:
            cand_ids = []
        checks.append(_check(
            "EMULATOR_UNKNOWN",
            "error",
            "Selected emulator is not in the registry.",
            [REMEDIATION_CHOOSE_ADAPTER],
            _fix_pick_core(adapter_id=emulator_id, candidates=cand_ids),
        ))

    launch_command = str(game.get("launch", "") or "").strip()
    if launch_command and ("{" in launch_command and "}" in launch_command):
        invalid = _detect_invalid_tokens_in_command(launch_command)
        if invalid:
            checks.append(_check("TEMPLATE_INVALID", "error", "Launch template contains unknown tokens.", [REMEDIATION_KEEP_CUSTOM], _fix_explain(invalid_tokens=invalid, template=launch_command)))
        else:
            try:
                resolve_launch(game, profiles, which=which, data_dir=data_dir)
            except ValueError:
                checks.append(_check("TEMPLATE_INVALID", "error", "Launch template is invalid.", [REMEDIATION_KEEP_CUSTOM], _fix_explain(template=launch_command)))
        # extra validate startup_args style tokens
        try:
            from pkg.parity.launch_tokens import validate_tokens
            v = validate_tokens(launch_command)
            if v and not any(c["code"] == "TEMPLATE_INVALID" for c in checks):
                checks.append(_check("TEMPLATE_INVALID", "error", "Launch template contains unknown tokens.", [REMEDIATION_KEEP_CUSTOM], _fix("explain_token", v["label"], v["payload"])))
        except Exception:
            pass

    active_adapter = adapter
    if active_adapter is None and platform:
        from pkg.parity.parity_emulator_defs import detect_adapter_for_platform

        active_adapter = detect_adapter_for_platform(platform, which=which)

    # Validate startup_args tokens for active adapter if present
    if active_adapter and active_adapter.get("startup_args"):
        try:
            from pkg.parity.launch_tokens import validate_startup_args
            invalid = validate_startup_args(active_adapter.get("startup_args"))
            if invalid:
                checks.append(_check(
                    "TEMPLATE_INVALID",
                    "error",
                    "Emulator startup args contain unknown tokens.",
                    [REMEDIATION_CHOOSE_ADAPTER],
                    _fix_explain(invalid_tokens=invalid, template=" ".join(active_adapter.get("startup_args") or [])),
                ))
        except Exception:
            pass

    install_mode = ""
    if active_adapter:
        native_exe = active_adapter.get("native_exe") or ""
        if native_exe and which(native_exe):
            install_mode = "native"
        elif _flatpak_installed(active_adapter.get("flatpak_app_id"), which, run):
            install_mode = "flatpak"
        elif native_exe or active_adapter.get("flatpak_app_id"):
            remediations = [REMEDIATION_INSTALL_FLATPAK] if active_adapter.get("flatpak_app_id") else [REMEDIATION_INSTALL_NATIVE]
            code = "FLATPAK_NOT_INSTALLED" if active_adapter.get("flatpak_app_id") and not native_exe else "NATIVE_EXE_MISSING"
            if active_adapter.get("flatpak_app_id") and native_exe:
                code = "NATIVE_EXE_MISSING"
            app_id = active_adapter.get("flatpak_app_id") or ""
            checks.append(_check(code, "error", "Emulator is not installed.", remediations, _fix_flatpak(app_id, native_exe)))

        if install_mode == "flatpak":
            app_id = active_adapter.get("flatpak_app_id")
            if app_id and not _flatpak_fs_allowed(app_id, path_value, which, run):
                checks.append(_check(
                    "FLATPAK_FS_DENIED",
                    "error",
                    "Flatpak emulator cannot access the game path.",
                    [REMEDIATION_INSTALL_FLATPAK],
                    _fix_flatpak(app_id),
                ))

        missing_core = _retroarch_core_missing(active_adapter)
        if missing_core:
            checks.append(_check(
                "RETROARCH_CORE_MISSING",
                "error",
                "RetroArch core is missing.",
                [REMEDIATION_CHOOSE_ADAPTER, REMEDIATION_INSTALL_NATIVE],
                _fix_pick_core(core=missing_core, adapter_id=active_adapter.get("adapter_id")),
            ))

        emulator_name = active_adapter.get("label", "").split(" (")[0]
        bios_platforms = {
            "PlayStation", "PSX", "PS2", "PlayStation 2", "PS3", "PlayStation 3",
            "Sega Saturn", "Saturn", "Dreamcast",
        }
        if platform in bios_platforms:
            deps = detect_dependencies(emulator_name)
            if deps.get("missing"):
                # Use first missing path for reveal
                first_missing = deps["missing"][0] if deps["missing"] else {}
                missing_path = first_missing.get("path", "") if isinstance(first_missing, dict) else ""
                checks.append(_check(
                    "BIOS_MISSING",
                    "warning",
                    "Required BIOS files are missing.",
                    [REMEDIATION_IMPORT_INCOMPLETE],
                    _fix_reveal(missing_path, first_missing.get("name", "BIOS") if isinstance(first_missing, dict) else "BIOS"),
                ))
                firmware_labels = [item["name"] for item in deps["missing"] if "firmware" in item["name"].lower()]
                if firmware_labels:
                    checks.append(_check(
                        "FIRMWARE_MISSING",
                        "warning",
                        "Required firmware files are missing.",
                        [REMEDIATION_IMPORT_INCOMPLETE],
                        _fix_reveal(missing_path, "Firmware"),
                    ))

    # Ambiguous platform and emulator required checks — must be after active_adapter resolution
    # to provide actionable picker chips for .iso and missing emulator cases.
    amb = _ambiguous_platform_check(game, adapter, active_adapter, launch_command, which)
    if amb:
        checks.append(amb)
    else:
        em_req = _emulator_required_check(game, adapter, active_adapter, launch_command)
        if em_req:
            checks.append(em_req)

    try:
        resolved = resolve_launch(game, profiles, which=which, data_dir=data_dir)
    except (ValueError, FileNotFoundError) as error:
        message = str(error)
        if "path" in message.lower():
            if "exist" in message.lower():
                checks.append(_check("PATH_MISSING", "error", "Game path does not exist.", [REMEDIATION_SET_PATH], _fix_reveal(path_value)))
            else:
                checks.append(_check("ARGV_INVALID", "error", "Launch command could not be built.", [REMEDIATION_CHOOSE_ADAPTER], _fix_explain(template=message)))
        else:
            checks.append(_check("ARGV_INVALID", "error", "Launch command could not be built.", [REMEDIATION_CHOOSE_ADAPTER], _fix_explain(template=message)))
        resolved = None
    else:
        args = resolved.get("args") or []
        if not args or any("{" in str(arg) and "}" in str(arg) for arg in args):
            # find leftover tokens
            leftover = []
            for arg in args:
                if "{" in str(arg) and "}" in str(arg):
                    leftover.extend(_detect_invalid_tokens_in_command(str(arg)))
            # If no invalid tokens found but still braces, treat as leftover template
            if not leftover:
                leftover = ["{unknown}"]
            checks.append(_check("ARGV_INVALID", "error", "Launch argv preview is invalid.", [REMEDIATION_CHOOSE_ADAPTER], _fix_explain(invalid_tokens=leftover, template=" ".join(str(a) for a in args))))
        cwd = resolved.get("cwd")
        if cwd and not Path(cwd).is_dir():
            checks.append(_check("CWD_INVALID", "error", "Working directory is invalid.", [REMEDIATION_SET_PATH], _fix_reveal(cwd)))

    checks.extend(_warning_gaps(game))
    # Ensure every error still has fix_action (fallback already handled in _check)
    return checks


def _derive_status(checks):
    if any(item["severity"] == "error" for item in checks):
        return "blocked"
    if any(item["severity"] == "warning" for item in checks):
        return "warning"
    return "ready"


def _build_result(game, profiles, *, game_id, candidate_id, data_dir, which=None, run=None):
    checks = run_preflight_checks(game, profiles, data_dir, which=which, run=run)
    resolved = build_resolved(game, profiles, data_dir=data_dir, which=which)
    return {
        "status": _derive_status(checks),
        "game_id": game_id,
        "candidate_id": candidate_id,
        "resolved": resolved,
        "checks": checks,
    }


def preflight_single(payload, *, state=None, profiles=None, data_dir="", game=None, which=None, run=None):
    from openbox import DATA, load_state

    state = state if state is not None else load_state()
    profiles = profiles if profiles is not None else state.get("profiles", {})
    data_dir = data_dir or str(DATA.parent)
    if game is not None:
        game_id = str(payload.get("game_id") or "").strip() or None if isinstance(payload, dict) else None
        candidate = payload.get("candidate") if isinstance(payload, dict) else None
        candidate_id = str((candidate or {}).get("candidate_id") or "").strip() or None
    else:
        game_id, candidate = _parse_identity(payload)
        game, game_id, candidate_id = _game_from_identity(game_id, candidate, state=state, data_dir=data_dir)
    return _build_result(
        game,
        profiles,
        game_id=game_id,
        candidate_id=candidate_id,
        data_dir=data_dir,
        which=which,
        run=run,
    )


def preflight_batch(payload, *, state=None, profiles=None, data_dir="", games=None, which=None, run=None):
    from openbox import DATA, load_state

    if not isinstance(payload, dict):
        raise BadRequest("Request body must be a JSON object.")
    items = payload.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        raise BadRequest("items must contain between 1 and 200 identities.")
    state = state if state is not None else load_state()
    profiles = profiles if profiles is not None else state.get("profiles", {})
    data_dir = data_dir or str(DATA.parent)
    results = []
    by_platform_map = {}
    for index, item in enumerate(items):
        if games is not None and index < len(games):
            game = games[index]
            if isinstance(item, dict):
                item_game_id = str(item.get("game_id") or "").strip() or None
                candidate = item.get("candidate")
                candidate_id = str((candidate or {}).get("candidate_id") or "").strip() or None
            else:
                item_game_id = None
                candidate_id = None
            result = _build_result(
                game,
                profiles,
                game_id=item_game_id,
                candidate_id=candidate_id,
                data_dir=data_dir,
                which=which,
                run=run,
            )
            platform = game.get("platform")
        else:
            result = preflight_single(item, state=state, profiles=profiles, data_dir=data_dir, which=which, run=run)
            if isinstance(item, dict) and item.get("candidate"):
                platform = item["candidate"].get("platform")
            elif result.get("game_id"):
                try:
                    platform = game_from_payload(state, {"game_id": result["game_id"]}).get("platform")
                except (IndexError, ValueError, KeyError):
                    platform = None
            else:
                platform = None
        results.append(result)
        bucket = by_platform_map.setdefault(platform, {"platform": platform, "ready": 0, "warning": 0, "blocked": 0})
        bucket[result["status"]] += 1
    totals = {"ready": 0, "warning": 0, "blocked": 0}
    for result in results:
        totals[result["status"]] += 1
    return {
        "totals": totals,
        "by_platform": list(by_platform_map.values()),
        "results": results,
    }
