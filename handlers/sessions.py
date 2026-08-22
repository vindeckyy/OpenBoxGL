"""SessionHandlers capability handlers. Launch, session lifecycle, running/history, shutdown, recovery, and Big Box."""

import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

from openbox import STATE_STORE, load_state, recover_state as recover_library_state
from routes.registry import route
from webapp_state import EVENT_SEQUENCE, PROCESS_LOCK, RUNNING, SESSION_EVENTS, STATE_LOCK, bump_media_epoch, control_game_session, game_from_payload, load_state_view, start_game


class SessionHandlers:
    @route("GET", "/api/running")
    def _api_get_api_running(self, parsed):
        try:
            after = int(parse_qs(parsed.query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        state = load_state_view()
        abandoned = [s for s in state.get('active_sessions', []) if s.get('status') == 'abandoned']
        with PROCESS_LOCK:
            payload = {
                "running": list(RUNNING.values()),
                "abandoned": abandoned,
                "events": [event for event in SESSION_EVENTS if event["id"] > after],
                "last_event": EVENT_SEQUENCE,
            }
        self.send_json(200, payload)
        return

    @route("GET", "/api/history")
    def _api_get_api_history(self, parsed):
        try:
            limit = min(500, max(1, int(parse_qs(parsed.query).get("limit", ["100"])[0])))
        except ValueError:
            limit = 100
        state_view = load_state_view()
        history = list(reversed(state_view.get("history", [])[-limit:]))
        self.send_json(200, {"history": history, "enabled": state_view.get("settings", {}).get("track_session_history", True)})
        return

    @route("POST", "/api/launch")
    def _api_post_api_launch(self, payload):
        self.launch(payload)

    @route("POST", "/api/session/control")
    def _api_post_api_session_control(self, payload):
        self.control_session(payload)

    @route("POST", "/api/session/cleanup")
    def _api_post_api_session_cleanup(self, payload):
        launch_id = payload.get("launch_id")
        def mutate(state):
            state["active_sessions"] = [s for s in state.get("active_sessions", []) if s.get("launch_id") != launch_id]
        from openbox import update_state
        update_state(mutate)
        self.send_json(200, {"ok": True})

    @route("POST", "/api/bigbox/mode")
    def _api_post_api_bigbox_mode(self, payload):
        self.bigbox_mode_switch(payload)

    @route("POST", "/api/state/recover")
    def _api_post_api_state_recover(self, payload):
        self.recover_state(payload)

    @route("POST", "/api/shutdown")
    def _api_post_api_shutdown(self, payload):
        self.shutdown(payload)

    @route("POST", "/api/extra/launch")
    def _api_post_api_extra_launch(self, payload):
        self.launch_extra(payload)

    def launch(self, payload):
        if payload.get("id") is None and not payload.get("game_id"):
            raise ValueError("Game id is required.")
        try:
            legacy_id = int(payload["id"]) if payload.get("id") is not None else int(payload.get("legacy_id", 0))
        except (KeyError, TypeError, ValueError):
            from api_errors import BadRequest
            raise BadRequest("missing or invalid game id") from None
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
                games_list = load_state().get("games", [])
                return self.send_json(200, {
                    "dry_run": True,
                    "backup_available": STATE_STORE.backup_path.is_file(),
                    "snapshots": STATE_STORE.snapshots(),
                    "games": len(games_list) if isinstance(games_list, list) else 0,
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
            return self.send_json(200, {"ok": True, "entering": False})
        key = "bigbox_shutdown_commands"
        for command in load_state().get("settings", {}).get(key, []):
            try:
                args = shlex.split(str(command))
                args[0] = str(Path(args[0]).expanduser())
                subprocess.Popen(args, start_new_session=True)
            except (OSError, ValueError, IndexError):
                import logging
                logging.getLogger(__name__).debug('bigbox_mode_switch command failed', exc_info=True)
        self.send_json(200, {"ok": True, "entering": True})

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


