"""EmulatorsHandlers capability handlers. Emulator install, update, open, scan, and definitions."""

import subprocess
from urllib.parse import parse_qs

from emulators import emulator_status, install_all_emulators, install_emulator, launch_emulator, recommendations_for_platform, update_all_emulators, update_emulator
from pkg.parity.parity_emulator_defs import EMULATORS, list_scan_configs, load_definitions, load_registry, save_scan_config, scan_folder as scan_emulator_folder
from pkg.parity.parity_import import detect_dependencies
from pkg.parity.parity_emulator_settings import (
    SETTINGS_SCHEMAS,
    canonical_emulator_id,
    effective_values,
    is_managed,
    normalize_values,
    plugin_effective_values,
    plugin_emulator_sections,
    plugin_section_key,
    version_probe,
)
from pkg.parity.parity_plugin_storefront import plugin_manifest
from plugins import validate_plugin_settings
from routes.registry import route
from webapp_state import DATA, INSTALLS, JOB_MANAGER, PROCESS_LOCK, ROOT, clear_file_probe_cache, load_state_view, merge_imported_games, transact_state

_EMULATOR_APP_IDS = {"mame": "org.mamedev.MAME", "retroarch": "org.libretro.RetroArch"}


class EmulatorsHandlers:
    @route("GET", "/api/v2/emulators/settings")
    def _api_get_api_v2_emulators_settings(self, parsed):
        state = load_state_view()
        entries = []
        for emulator_id, schema in SETTINGS_SCHEMAS.items():
            app_id = _EMULATOR_APP_IDS.get(emulator_id, "")
            stored = (state.get("emulator_settings") or {}).get(emulator_id, {})
            definition = EMULATORS.get(app_id, {})
            probe = version_probe(app_id, definition) if app_id else {}
            entries.append({
                "emulator_id": emulator_id,
                "app_id": app_id,
                "label": schema["label"],
                "managed": is_managed(state, emulator_id),
                "schema": schema,
                "global": effective_values(state, emulator_id, ""),
                "platforms": stored.get("platforms", {}) if isinstance(stored.get("platforms"), dict) else {},
                "skip_update_check": app_id in {
                    str(item) for item in (state.get("settings", {}).get("emulator_update_checks", []) or [])
                },
                "version": probe,
            })
        self.send_json(200, {
            "emulators": entries,
            "plugins": [
                {
                    **section,
                    "effective_global": plugin_effective_values(section, ""),
                }
                for section in plugin_emulator_sections(state)
            ],
        })
        return

    @route("POST", "/api/v2/emulators/settings")
    def _api_post_api_v2_emulators_settings(self, payload):
        if payload.get("plugin_id"):
            self.save_plugin_emulator_settings(payload)
            return
        emulator_id = canonical_emulator_id(payload.get("emulator_id", ""))
        platform = str(payload.get("platform") or "").strip()
        if len(platform) > 80:
            raise ValueError("Platform must be 80 characters or fewer.")
        values = payload.get("values", {})
        clean = normalize_values(emulator_id, values)
        managed = payload.get("managed")
        reset = bool(payload.get("reset"))
        skip_update_check = payload.get("skip_update_check")
        app_id = _EMULATOR_APP_IDS.get(emulator_id, "")

        def mutate(state):
            entry = state.setdefault("emulator_settings", {}).setdefault(emulator_id, {})
            if managed is not None:
                entry["managed"] = bool(managed)
            if skip_update_check is not None and app_id:
                checks = state.setdefault("settings", {}).setdefault("emulator_update_checks", [])
                if not isinstance(checks, list):
                    checks = state["settings"]["emulator_update_checks"] = []
                if bool(skip_update_check):
                    if app_id not in checks:
                        checks.append(app_id)
                else:
                    state["settings"]["emulator_update_checks"] = [item for item in checks if item != app_id]
            if reset:
                if platform:
                    entry.setdefault("platforms", {}).pop(platform, None)
                else:
                    entry.pop("platforms", None)
            elif platform:
                entry.setdefault("platforms", {})[platform] = clean
            else:
                entry["global"] = clean
            return {
                "emulator_id": emulator_id,
                "managed": is_managed(state, emulator_id),
                "global": effective_values(state, emulator_id, ""),
                "platforms": entry.get("platforms", {}),
            }

        _, result = transact_state(mutate)
        self.send_json(200, result)
        return

    def save_plugin_emulator_settings(self, payload):
        plugin_id = str(payload.get("plugin_id") or "").strip()
        emulator_id = str(payload.get("emulator_id") or "").strip()
        platform = str(payload.get("platform") or "").strip()
        if len(platform) > 80:
            raise ValueError("Platform must be 80 characters or fewer.")
        manifest = plugin_manifest(DATA.parent / "plugins", plugin_id)
        declarations = (manifest.get("capabilities") or {}).get("emulator_settings") or []
        declaration = next(
            (item for item in declarations if str(item.get("emulator_id")) == emulator_id),
            None,
        )
        if declaration is None:
            raise ValueError("Plugin does not declare this emulator.")
        scopes = declaration.get("scopes") or []
        if platform and "platform" not in scopes:
            raise ValueError("This plugin does not support per-platform settings.")
        if not platform and "global" not in scopes:
            raise ValueError("This plugin only supports per-platform settings.")
        schema = declaration.get("schema") or {}
        values = payload.get("values") or {}
        if not isinstance(values, dict):
            raise ValueError("values must be an object.")
        clean = validate_plugin_settings(schema, values)
        key = plugin_section_key(plugin_id, emulator_id)
        managed = payload.get("managed")
        reset = bool(payload.get("reset"))

        def mutate(state):
            entry = state.setdefault("emulator_settings", {}).setdefault(key, {})
            if managed is not None:
                entry["managed"] = bool(managed)
            if reset:
                if platform:
                    entry.setdefault("platforms", {}).pop(platform, None)
                else:
                    entry.pop("platforms", None)
                    entry.pop("global", None)
            elif platform:
                entry.setdefault("platforms", {})[platform] = clean
            else:
                entry["global"] = clean
            return {
                "plugin_id": plugin_id,
                "emulator_id": emulator_id,
                "key": key,
                "managed": bool(entry.get("managed")),
                "global": entry.get("global", {}),
                "platforms": entry.get("platforms", {}),
            }

        _, result = transact_state(mutate)
        self.send_json(200, result)
        return

    @route("GET", "/api/emulators")
    def _api_get_api_emulators(self, parsed):
        emulators = emulator_status()
        state = load_state_view()
        skip_checks = {
            str(item).strip()
            for item in (state.get("settings", {}).get("emulator_update_checks", []) or [])
            if str(item).strip()
        }
        for emulator in emulators:
            app_id = emulator.get("app_id", "")
            emulator["update_checked"] = app_id not in skip_checks
            emulator["skip_update_check"] = app_id in skip_checks
            emulator.setdefault("version", "")
            emulator.setdefault("latest_version", "")
            emulator.setdefault("update_available", False)
            if emulator.get("installed") and app_id not in skip_checks:
                try:
                    definition = EMULATORS.get(app_id, {})
                    probe = version_probe(app_id, {
                        "app_id": app_id,
                        "native": definition.get("native", ""),
                    })
                    emulator.update({
                        "version": probe.get("version", ""),
                        "latest_version": probe.get("latest_version", ""),
                        "update_available": bool(probe.get("update_available")),
                    })
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            with PROCESS_LOCK:
                app_id = emulator.get("app_id", "")
                job = INSTALLS.get(f"update:{app_id}") or INSTALLS.get(app_id, {})
                emulator["job"] = job
        install_all = INSTALLS.get("__all__", {})
        update_all = INSTALLS.get("__update_all__", {})
        self.send_json(200, {"emulators": emulators, "install_all": install_all, "update_all": update_all})
        return

    @route("GET", "/api/emulators/recommend")
    def _api_get_api_emulators_recommend(self, parsed):
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        self.send_json(200, {"recommendations": recommendations_for_platform(platform)})
        return

    @route("GET", "/api/emulators/dependencies")
    def _api_get_api_emulators_dependencies(self, parsed):
        name = parse_qs(parsed.query).get("name", [""])[0]
        self.send_json(200, detect_dependencies(name))
        return

    @route("GET", "/api/emulators/definitions")
    def _api_get_api_emulators_definitions(self, parsed):
        self.send_json(200, {"definitions": load_definitions(ROOT / "emulator_defs")})
        return

    @route("GET", "/api/emulators/scan-configs")
    def _api_get_api_emulators_scan_configs(self, parsed):
        self.send_json(200, {"configs": list_scan_configs(load_state_view())})
        return

    @route("GET", "/api/v2/emulators/registry")
    def _api_get_api_v2_emulators_registry(self, parsed):
        raw_qs = getattr(parsed, "query", "") or ""
        # Mock objects in tests provide a Mock for query; treat non-str as empty
        if not isinstance(raw_qs, str):
            raw_qs = str(raw_qs) if isinstance(raw_qs, bytes) else ""
        qs = parse_qs(raw_qs)
        health = qs.get("health", ["0"])[0]
        want_health = str(health).lower() in {"1", "true", "yes", "on"}
        if want_health:
            # Validate startup_args tokens as part of health pass; adapters with invalid tokens are still returned but health flags reflect.
            try:
                from pkg.parity.launch_tokens import validate_startup_args  # noqa: F401
            except Exception:
                pass
            payload = load_registry(ROOT / "emulator_defs", health=True)
        else:
            payload = load_registry(ROOT / "emulator_defs")
        self.send_json(200, payload)
        return

    @route("POST", "/api/emulators/install")
    def _api_post_api_emulators_install(self, payload):
        self.install_emulator(payload)

    @route("POST", "/api/emulators/install-all")
    def _api_post_api_emulators_install_all(self, payload):
        self.install_all_emulators()

    @route("POST", "/api/emulators/update")
    def _api_post_api_emulators_update(self, payload):
        self.update_one_emulator(payload)

    @route("POST", "/api/emulators/update-all")
    def _api_post_api_emulators_update_all(self, payload):
        self.update_all_emulators_route()

    @route("POST", "/api/emulators/open")
    def _api_post_api_emulators_open(self, payload):
        self.open_emulator(payload)

    @route("POST", "/api/emulators/scan")
    def _api_post_api_emulators_scan(self, payload):
        self.scan_emulator_folder_route(payload)

    @route("POST", "/api/emulators/scan-configs")
    def _api_post_api_emulators_scan_configs(self, payload):
        self.save_emulator_scan_config(payload)

    def install_emulator(self, payload):
        app_id = str(payload.get("app_id", "")).strip()
        if not app_id:
            raise ValueError("app_id is required")
        with PROCESS_LOCK:
            if INSTALLS.get(app_id, {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS[app_id] = {"state": "installing"}

        def worker():
            try:
                profiles = install_emulator(app_id)
                def mutate(state):
                    for platform, command in profiles.items():
                        state["profiles"].setdefault(platform, command)
                    canonical = next((key for key, value in _EMULATOR_APP_IDS.items() if value == app_id), "")
                    if canonical:
                        state.setdefault("emulator_settings", {}).setdefault(canonical, {})["managed"] = True
                transact_state(mutate)
                job = {"state": "done", "profiles": profiles}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[app_id] = job

        JOB_MANAGER.submit(f"emulator-install:{app_id}", worker)
        self.send_json(202, {"state": "installing"})

    def install_all_emulators(self):
        with PROCESS_LOCK:
            if INSTALLS.get("__all__", {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS["__all__"] = {"state": "installing"}

        def worker():
            try:
                result = install_all_emulators()
                installed_names = set(result.get("installed") or [])
                installed_ids = {
                    canonical for app_id, canonical in _EMULATOR_APP_IDS.items()
                    if EMULATORS.get(app_id, {}).get("name") in installed_names
                }
                if installed_ids:
                    def mutate(state):
                        for canonical in installed_ids:
                            state.setdefault("emulator_settings", {}).setdefault(canonical, {})["managed"] = True
                    transact_state(mutate)
                job = {"state": "done", **result}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__all__"] = job

        JOB_MANAGER.submit("emulator-install-all", worker)
        self.send_json(202, {"state": "installing"})

    def open_emulator(self, payload):
        app_id = str(payload.get("app_id", "")).strip()
        if not app_id:
            raise ValueError("app_id is required")
        try:
            self.send_json(200, launch_emulator(app_id))
        except (OSError, ValueError) as e:
            from api_errors import BadRequest
            raise BadRequest(str(e)) from None

    def scan_emulator_folder_route(self, payload):
        folder = str(payload.get("folder", "")).strip()
        if not folder:
            from api_errors import BadRequest
            raise BadRequest("folder required")
        emulator_id = str(payload.get("emulator_id", "")).strip() or None
        imported = scan_emulator_folder(folder, emulator_id=emulator_id)
        added, found = merge_imported_games(imported, lambda game: ("path", str(game.get("path", ""))))
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

    def save_emulator_scan_config(self, payload):
        folder = str(payload.get("folder", "")).strip()
        emulator_id = str(payload.get("emulator_id", "")).strip()
        if not folder or not emulator_id:
            from api_errors import BadRequest
            raise BadRequest("folder and emulator_id required")
        auto_update = bool(payload.get("auto_update", False))
        def mutate(state):
            return save_scan_config(state, folder, emulator_id, auto_update=auto_update)
        _, entry = transact_state(mutate)
        self.send_json(200, {"config": entry})

    def update_one_emulator(self, payload):
        app_id = str(payload.get("app_id", "")).strip()
        if not app_id:
            raise ValueError("app_id is required")
        skip = load_state_view().get("settings", {}).get("emulator_update_checks", []) or []
        if app_id in {str(item) for item in skip}:
            self.send_json(200, {"state": "skipped", "reason": "Update checks are disabled for this emulator."})
            return
        with PROCESS_LOCK:
            key = f"update:{app_id}"
            if INSTALLS.get(key, {}).get("state") == "updating":
                self.send_json(200, {"state": "updating"})
                return
            INSTALLS[key] = {"state": "updating"}

        def worker():
            try:
                result = update_emulator(app_id)
                job = {"state": "done", **result}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS[f"update:{app_id}"] = job

        JOB_MANAGER.submit(key, worker)
        self.send_json(202, {"state": "updating"})

    def update_all_emulators_route(self):
        with PROCESS_LOCK:
            if INSTALLS.get("__update_all__", {}).get("state") == "updating":
                self.send_json(200, {"state": "updating"})
                return
            INSTALLS["__update_all__"] = {"state": "updating"}

        def worker():
            try:
                skip = load_state_view().get("settings", {}).get("emulator_update_checks", []) or []
                result = update_all_emulators(skip=skip)
                job = {"state": "done", **result}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__update_all__"] = job

        JOB_MANAGER.submit("emulator-update-all", worker)
        self.send_json(202, {"state": "updating"})


