"""ExtensionsHandlers capability handlers. Plugins, themes, playlists, filter presets, and webhooks."""

import email.utils
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from automation import DEFAULT_ATTEMPTS, DEFAULT_TIMEOUT, EVENT_TYPES, MAX_WEBHOOKS, test_ping, validate_webhook
from openbox import DATA, load_state
from plugin_catalog import download_plugin_package, fetch_plugin_catalog
from plugins import install_plugin, list_plugins, remove_plugin, set_plugin_enabled
from stock_themes import ensure_stock_themes
from webapp_state import PLUGIN_EPOCH, ROOT, game_from_payload, load_state_view, public_webhook_configs, transact_state, webhook_configs


class ExtensionsHandlers:
    def _api_get_api_theme_css(self, parsed):
        name = parse_qs(parsed.query).get("name", [""])[0]
        theme = DATA.parent / "themes" / f"{Path(name).stem}.css"
        if not name or not theme.is_file() or theme.stem != name:
            self.send_bytes(200, b"", "text/css; charset=utf-8")
            return
        theme_bytes = theme.read_bytes()
        etag = f'"{theme.stat().st_mtime_ns:x}-{len(theme_bytes):x}"'
        self.send_bytes(
            200, theme_bytes, "text/css; charset=utf-8",
            cache_control="public, max-age=0, must-revalidate",
            etag=etag,
            last_modified=email.utils.formatdate(theme.stat().st_mtime, usegmt=True),
        )
        return

    def _api_get_api_themes(self, parsed):
        ensure_stock_themes(DATA.parent / "themes", ROOT)
        themes = sorted(path.stem for path in (DATA.parent / "themes").glob("*.css"))
        settings = load_state_view().get("settings", {})
        platform = parse_qs(parsed.query).get("platform", [""])[0]
        mappings = settings.get("theme_by_platform", {})
        self.send_json(200, {
            "themes":themes,
            "selected":mappings.get(platform, settings.get("theme", "")) if platform else settings.get("theme", ""),
            "global":settings.get("theme", ""),
            "mappings":mappings,
        })
        return

    def _api_get_api_plugins(self, parsed):
        self.send_json(200, {"plugins":list_plugins(DATA.parent / "plugins")})
        return

    def _api_get_api_plugins_catalog(self, parsed):
        self.send_json(200, {"catalog": fetch_plugin_catalog()})
        return

    def _api_get_api_webhooks(self, parsed):
        state = load_state_view()
        self.send_json(200, {"webhooks": public_webhook_configs(state), "events": list(EVENT_TYPES), "attempts": int(state.get("settings", {}).get("webhook_attempts") or DEFAULT_ATTEMPTS), "timeout": int(state.get("settings", {}).get("webhook_timeout") or DEFAULT_TIMEOUT)})
        return

    def _api_post_api_webhooks(self, payload):
        self.save_webhooks(payload)

    def _api_post_api_webhooks_test(self, payload):
        self.test_webhook(payload)

    def _api_post_api_plugins_catalog_install(self, payload):
        self.install_catalog_plugin(payload)

    def _api_post_api_themes_open_folder(self, payload):
        self.open_themes_folder()

    def _api_post_api_plugins_install(self, payload):
        self.install_plugin(payload)

    def _api_post_api_plugins_toggle(self, payload):
        self.toggle_plugin(payload)

    def _api_post_api_plugins_remove(self, payload):
        self.remove_plugin(payload)

    def _api_post_api_themes_select(self, payload):
        self.select_theme(payload)

    def _api_post_api_themes_import(self, payload):
        self.import_theme(payload)

    def _api_post_api_playlists(self, payload):
        self.save_playlist(payload)

    def _api_post_api_playlists_delete(self, payload):
        self.delete_playlist(payload)

    def save_webhooks(self, payload):
        configs = payload.get("webhooks", payload.get("configs", []))
        if not isinstance(configs, list) or len(configs) > MAX_WEBHOOKS:
            raise ValueError(f"Webhooks must be a list of at most {MAX_WEBHOOKS} entries.")
        clean = []
        for raw in configs:
            if not isinstance(raw, dict):
                raise ValueError("Webhook configuration must be an object.")
            config = dict(raw)
            config["id"] = str(config.get("id") or f"wh-{secrets.token_hex(8)}")
            config["url"] = str(config.get("url") or "").strip()
            config["events"] = list(config.get("events") or [])
            config["enabled"] = bool(config.get("enabled", True))
            config["attempts"] = int(config.get("attempts") or DEFAULT_ATTEMPTS)
            config["timeout"] = int(config.get("timeout") or DEFAULT_TIMEOUT)
            if not config.get("secret") and raw.get("secret_set"):
                existing = next((item for item in webhook_configs() if item.get("id") == config["id"]), {})
                config["secret"] = str(existing.get("secret") or "")
            validate_webhook(config, openbox_port=self.server.server_port)
            clean.append(config)
        def mutate(state):
            settings = state.setdefault("settings", {})
            settings["webhooks"] = clean
            settings["webhook_attempts"] = int(payload.get("attempts") or DEFAULT_ATTEMPTS)
            settings["webhook_timeout"] = int(payload.get("timeout") or DEFAULT_TIMEOUT)
        transact_state(mutate)
        self.send_json(200, {"webhooks": public_webhook_configs(), "events": list(EVENT_TYPES)})

    def test_webhook(self, payload):
        config = dict(payload.get("webhook") or payload)
        result = test_ping(config, openbox_port=self.server.server_port)
        self.send_json(200, result)

    def install_plugin(self, payload):
        manifest = install_plugin(str(payload.get("path", "")), DATA.parent / "plugins")
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"plugin":manifest})

    def toggle_plugin(self, payload):
        enabled = set_plugin_enabled(
            DATA.parent / "plugins",
            str(payload.get("id", "")),
            bool(payload.get("enabled")),
        )
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"enabled":enabled})

    def remove_plugin(self, payload):
        plugin_id = remove_plugin(DATA.parent / "plugins", str(payload.get("id", "")))
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"removed":plugin_id})

    def select_theme(self, payload):
        name = str(payload.get("name", "")).strip()
        platform = str(payload.get("platform", "")).strip()
        if name and not (DATA.parent / "themes" / f"{Path(name).stem}.css").is_file():
            raise FileNotFoundError("Theme not found.")
        def mutate(state):
            settings = state.setdefault("settings", {})
            if platform:
                mappings = settings.setdefault("theme_by_platform", {})
                if name:
                    mappings[platform] = name
                else:
                    mappings.pop(platform, None)
            else:
                settings["theme"] = name
        transact_state(mutate)
        self.send_json(200, {"selected":name, "platform":platform})

    def import_theme(self, payload):
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file() or source.suffix.lower() != ".css":
            raise ValueError("Theme path must point to a CSS file.")
        destination = DATA.parent / "themes" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        self.send_json(200, {"theme": destination.stem})

    def save_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        rules = payload.get("rules", {})
        if not name or not isinstance(rules, dict):
            raise ValueError("Playlist name and rules are required.")
        playlist_type = str(payload.get("type", "filter")).strip().casefold()
        if playlist_type not in {"filter", "manual"}:
            raise ValueError("Playlist type must be filter or manual.")
        state = load_state()
        clean = {
            key: str(rules.get(key, "")).strip()
            for key in ("platform", "view", "query", "platform_category", "esrb", "progress", "genre", "developer", "publisher", "installed", "hidden", "favorite")
            if str(rules.get(key, "")).strip()
        }
        members = payload.get("members", payload.get("ids", []))
        if not isinstance(members, list) or len(members) > 100000:
            raise ValueError("Playlist members must be a list.")
        member_ids = []
        for value in members:
            game = game_from_payload(state, {"game_id": value}) if str(value).startswith("game-") else game_from_payload(state, {"id": value})
            stable_id = str(game.get("game_id") or "")
            if stable_id and stable_id not in member_ids:
                member_ids.append(stable_id)
        parent = str(payload.get("parent", "")).strip()
        notes = str(payload.get("notes", "")).strip()
        def mutate(state):
            playlists = state.setdefault("playlists", [])
            existing = next((item for item in playlists if item.get("name") == name), None)
            if existing:
                existing["rules"] = clean
                existing["type"] = playlist_type
                existing["members"] = member_ids if playlist_type == "manual" else []
                existing["parent"] = parent
                existing["notes"] = notes
            else:
                playlists.append({"name": name, "type": playlist_type, "rules": clean, "members": member_ids if playlist_type == "manual" else [], "parent": parent, "notes": notes})
        transact_state(mutate)
        self.send_json(200, {"saved": name})

    def delete_playlist(self, payload):
        name = str(payload.get("name", "")).strip()
        def mutate(state):
            state["playlists"] = [item for item in state.get("playlists", []) if item.get("name") != name]
        transact_state(mutate)
        self.send_json(200, {"deleted": name})

    def install_catalog_plugin(self, payload):
        catalog = fetch_plugin_catalog()
        plugin_id = str(payload.get("id", "")).strip()
        entry = next((item for item in catalog if item.get("id") == plugin_id), None)
        if not entry:
            raise ValueError("Unknown catalog plugin.")
        if entry.get("local_only"):
            raise ValueError("This catalog entry is documentation-only. Install local plugin packages manually.")
        with tempfile.TemporaryDirectory(dir=DATA.parent) as temporary:
            archive = download_plugin_package(entry, temporary)
            manifest = install_plugin(archive, DATA.parent / "plugins")
        self.send_json(200, {"plugin": manifest})

    def open_themes_folder(self):
        folder = DATA.parent / "themes"
        ensure_stock_themes(folder, ROOT)
        folder.mkdir(parents=True, exist_ok=True)
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("xdg-open is required to open folders.")
        subprocess.Popen([opener, str(folder)])
        self.send_json(200, {"path": str(folder)})


