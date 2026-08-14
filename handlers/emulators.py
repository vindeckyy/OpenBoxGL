"""EmulatorsHandlers capability handlers. Emulator install, update, open, scan, and definitions.

Method bodies reference DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints
each function's ``__globals__`` at that namespace, so the bodies run
verbatim without circular imports or snapshotting process-global state.
"""

from handlers import rebind_methods


class EmulatorsHandlers:
    def _api_get_api_emulators(self, parsed):
        emulators = emulator_status()
        with PROCESS_LOCK:
            for emulator in emulators:
                emulator["job"] = INSTALLS.get(emulator["app_id"], {})
            install_all = INSTALLS.get("__all__", {})
        self.send_json(200, {"emulators": emulators, "install_all": install_all})
        return

    def _api_get_api_emulators_recommend(self, parsed):
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        self.send_json(200, {"recommendations": recommendations_for_platform(platform)})
        return

    def _api_get_api_emulators_dependencies(self, parsed):
        name = parse_qs(parsed.query).get("name", [""])[0]
        self.send_json(200, detect_dependencies(name))
        return

    def _api_get_api_emulators_definitions(self, parsed):
        self.send_json(200, {"definitions": load_definitions(ROOT / "emulator_defs")})
        return

    def _api_get_api_emulators_scan_configs(self, parsed):
        self.send_json(200, {"configs": list_scan_configs(load_state_view())})
        return

    def _api_post_api_emulators_install(self, payload):
        self.install_emulator(payload)

    def _api_post_api_emulators_install_all(self, payload):
        self.install_all_emulators()

    def _api_post_api_emulators_update(self, payload):
        self.update_one_emulator(payload)

    def _api_post_api_emulators_update_all(self, payload):
        self.update_all_emulators_route()

    def _api_post_api_emulators_open(self, payload):
        self.open_emulator(payload)

    def _api_post_api_emulators_scan(self, payload):
        self.scan_emulator_folder_route(payload)

    def _api_post_api_emulators_scan_configs(self, payload):
        self.save_emulator_scan_config(payload)

    def install_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
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
        def worker():
            try:
                result = install_all_emulators()
                job = {"state": "done", **result}
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                job = {"state": "error", "error": str(error)}
            with PROCESS_LOCK:
                INSTALLS["__all__"] = job

        with PROCESS_LOCK:
            if INSTALLS.get("__all__", {}).get("state") == "installing":
                self.send_json(200, {"state": "installing"})
                return
            INSTALLS["__all__"] = {"state": "installing"}
        JOB_MANAGER.submit("emulator-install-all", worker)
        self.send_json(202, {"state": "installing"})

    def open_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
        self.send_json(200, launch_emulator(app_id))

    def scan_emulator_folder_route(self, payload):
        folder = str(payload.get("folder", "")).strip()
        imported = scan_emulator_folder(folder)
        added, found = merge_imported_games(imported, lambda game: ("path", str(game.get("path", ""))))
        clear_file_probe_cache()
        self.send_json(200, {"added": added, "found": found})

    def save_emulator_scan_config(self, payload):
        folder = str(payload.get("folder", "")).strip()
        emulator_id = str(payload.get("emulator_id", "")).strip()
        auto_update = bool(payload.get("auto_update", False))
        def mutate(state):
            return save_scan_config(state, folder, emulator_id, auto_update=auto_update)
        _, entry = transact_state(mutate)
        self.send_json(200, {"config": entry})

    def update_one_emulator(self, payload):
        app_id = str(payload.get("app_id", ""))
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


rebind_methods(EmulatorsHandlers)
