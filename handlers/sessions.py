"""SessionHandlers capability handlers. Launch, session lifecycle, running/history, shutdown, recovery, and Big Box.

Method bodies reference DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints
each function's ``__globals__`` at that namespace, so the bodies run
verbatim without circular imports or snapshotting process-global state.
"""

from handlers import rebind_methods


class SessionHandlers:
    def _api_get_api_running(self, parsed):
        try:
            after = int(parse_qs(parsed.query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        with PROCESS_LOCK:
            payload = {
                "running": list(RUNNING.values()),
                "events": [event for event in SESSION_EVENTS if event["id"] > after],
                "last_event": EVENT_SEQUENCE,
            }
        self.send_json(200, payload)
        return

    def _api_get_api_history(self, parsed):
        try:
            limit = min(500, max(1, int(parse_qs(parsed.query).get("limit", ["100"])[0])))
        except ValueError:
            limit = 100
        state_view = load_state_view()
        history = list(reversed(state_view.get("history", [])[-limit:]))
        self.send_json(200, {"history": history, "enabled": state_view.get("settings", {}).get("track_session_history", True)})
        return

    def _api_post_api_launch(self, payload):
        self.launch(payload)

    def _api_post_api_session_control(self, payload):
        self.control_session(payload)

    def _api_post_api_bigbox_mode(self, payload):
        self.bigbox_mode_switch(payload)

    def _api_post_api_state_recover(self, payload):
        self.recover_state(payload)

    def _api_post_api_shutdown(self, payload):
        self.shutdown(payload)

    def _api_post_api_extra_launch(self, payload):
        self.launch_extra(payload)

    def launch(self, payload):
        if payload.get("id") is None and not payload.get("game_id"):
            raise ValueError("Game id is required.")
        legacy_id = int(payload["id"]) if payload.get("id") is not None else int(payload.get("legacy_id", 0))
        stable_game_id = str(payload.get("game_id") or "").strip()
        if stable_game_id:
            state = load_state()
            game = game_from_payload(state, payload)
            legacy_id = state["games"].index(game)
        self.send_json(200, {"ok": True, **start_game(legacy_id, stable_game_id=stable_game_id)})

    def control_session(self, payload):
        launch_id = str(payload.get("launch_id", ""))
        action = str(payload.get("action", ""))
        self.send_json(200, control_game_session(launch_id, action))

    def recover_state(self, payload=None):
        payload = payload or {}
        if payload.get("dry_run"):
            with STATE_LOCK:
                return self.send_json(200, {
                    "dry_run": True,
                    "backup_available": STATE_STORE.backup_path.is_file(),
                    "snapshots": STATE_STORE.snapshots(),
                    "games": load_state().get("games", []) and len(load_state().get("games", [])),
                })
        if payload.get("snapshot"):
            state = STATE_STORE.restore_snapshot(str(payload["snapshot"]))
            bump_media_epoch()
            return self.send_json(200, {"ok": True, "games": len(state.get("games", [])), "snapshot": str(payload["snapshot"])})
        state = recover_library_state()
        self.send_json(200, {"ok": True, "games": len(state.get("games", []))})

    def shutdown(self, payload):
        force = bool(payload.get("force"))
        stopped = []
        with PROCESS_LOCK:
            launch_ids = list(RUNNING.keys())
        for launch_id in launch_ids:
            try:
                control_game_session(launch_id, "kill" if force else "stop")
                stopped.append(launch_id)
            except ValueError:
                pass
        return self.send_json(200, {"stopped": len(stopped), "forced": force})

    def bigbox_mode_switch(self, payload):
        if not payload.get("entering"):
            return
        key = "bigbox_shutdown_commands"
        for command in load_state().get("settings", {}).get(key, []):
            try:
                args = shlex.split(str(command))
                args[0] = str(Path(args[0]).expanduser())
                subprocess.Popen(args, start_new_session=True)
            except (OSError, ValueError, IndexError):
                pass
        self.send_json(200, {"ok": True})

    def launch_extra(self, payload):
        state = load_state()
        game = game_from_payload(state, payload)
        kind = payload.get("kind")
        if kind not in {"applications", "versions", "documents"}:
            raise ValueError("Unknown extra type.")
        extra = game.get(kind, [])[int(payload["index"])]
        path = Path(extra["path"])
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if kind == "documents":
            opener = shutil.which("xdg-open")
            if not opener:
                raise FileNotFoundError("xdg-open is required to open documents.")
            args = [opener, str(path)]
        elif extra.get("command"):
            args = [part.replace("{path}", str(path)) for part in shlex.split(extra["command"])]
        else:
            args = [str(path)]
        subprocess.Popen(args, cwd=str(path.parent))
        self.send_json(200, {"ok": True})


rebind_methods(SessionHandlers)
