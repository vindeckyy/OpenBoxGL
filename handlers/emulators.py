"""EmulatorsHandlers capability handlers. Emulator install, update, open, scan, and definitions."""

import subprocess
from urllib.parse import parse_qs

from emulators import emulator_status, install_all_emulators, install_emulator, launch_emulator, recommendations_for_platform, update_all_emulators, update_emulator
from parity_emulator_defs import list_scan_configs, load_definitions, load_registry, save_scan_config, scan_folder as scan_emulator_folder
from parity_import import detect_dependencies
from routes.registry import route
from webapp_state import INSTALLS, JOB_MANAGER, PROCESS_LOCK, ROOT, clear_file_probe_cache, load_state_view, merge_imported_games, transact_state


class EmulatorsHandlers:
    @route("GET", "/api/emulators")
    def _api_get_api_emulators(self, parsed):
        emulators = emulator_status()
        with PROCESS_LOCK:
            for emulator in emulators:
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
                result = update_all_emulators()
                job = {"state": "done", **result}
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__update_all__"] = job

        JOB_MANAGER.submit("emulator-update-all", worker)
        self.send_json(202, {"state": "updating"})


