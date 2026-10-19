"""ExtensionsHandlers capability handlers. Plugins, themes, playlists, filter presets, and webhooks."""

import email.utils
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs

from automation import DEFAULT_ATTEMPTS, DEFAULT_TIMEOUT, EVENT_TYPES, MAX_WEBHOOKS, test_ping, validate_webhook
from backend_io import atomic_write_text
from openbox import DATA, load_state
from pkg.platform_compat import contained_path, open_path
from plugins import (
    MAX_LIBRARY_ENTRIES,
    get_plugin_settings,
    get_plugin_settings_public,
    install_plugin,
    list_plugins,
    load_plugin_state,
    save_plugin_state,
    plugin_commands,
    plugin_credentials_summary,
    plugin_data_values,
    plugin_trust_status,
    read_manifest,
    remove_plugin,
    run_plugin_hook,
    sandbox_status,
    set_plugin_credentials,
    set_plugin_data,
    set_plugin_enabled,
    set_plugin_permissions,
    set_plugin_settings,
    set_plugin_trust,
)
from pkg.parity.parity_plugin_stats import plugin_stats_summaries, refresh_plugin_stats
from plugin_catalog import download_plugin_package, fetch_plugin_catalog
from routes.registry import route

from stock_themes import ensure_stock_themes
from webapp_state import PLUGIN_EPOCH, ROOT, game_from_payload, load_state_view, public_webhook_configs, transact_state, webhook_configs

# The native windows paint their background before the page loads. The app writes the active
# theme's page background here; both hosts read it at launch (native_host.c, native_host_win.c).
NATIVE_BACKGROUND_FILE = "native-background"
DEFAULT_NATIVE_BACKGROUND = "#11100e"
_THEME_BACKGROUND = re.compile(r"--bg\s*:\s*(#[0-9a-fA-F]{6})\b")
_STOCK_THEMES = Path(__file__).resolve().parent.parent / "themes"
_THEME_AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav"}
_THEME_ASSET_LIMIT = 20 * 1024 * 1024
_THEME_SOUND_KEYS = {"move", "select", "back"}


def _remember_plugin_source(root: Path, plugin_id: str, source) -> None:
    """Record the local package used for an install so Repair can re-extract it."""
    path = Path(str(source)).expanduser().resolve()
    record = {"path": str(path)}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        record["sha256"] = digest.hexdigest()
    state = load_plugin_state(root)
    sources = state.get("local_sources")
    if not isinstance(sources, dict):
        sources = {}
    sources[plugin_id] = record
    state["local_sources"] = sources
    save_plugin_state(root, state)


def _forget_plugin_source(root: Path, plugin_id: str) -> None:
    state = load_plugin_state(root)
    sources = state.get("local_sources")
    if isinstance(sources, dict) and plugin_id in sources:
        sources.pop(plugin_id, None)
        save_plugin_state(root, state)


def _installed_local_package(root: Path, plugin_id: str):
    """Return the verified recorded local package, or raise ValueError."""
    state = load_plugin_state(root)
    sources = state.get("local_sources")
    record = sources.get(plugin_id) if isinstance(sources, dict) else None
    if not isinstance(record, dict):
        raise ValueError("This plugin has no catalog entry or recorded local package.")
    path = Path(str(record.get("path") or "")).expanduser()
    if not str(path) or not path.exists():
        raise ValueError("The recorded local plugin package is no longer available.")
    expected = str(record.get("sha256") or "")
    if expected and path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("The recorded local plugin package changed; reinstall it manually.")
    return path


def _theme_folder(name: str) -> Path | None:
    clean = Path(str(name or "")).stem
    if not clean or clean != name:
        return None
    for folder in (DATA.parent / "themes", _STOCK_THEMES):
        if (folder / f"{clean}.css").is_file():
            return folder
    return None


def _theme_relative_audio(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 300:
        return ""
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        return ""
    if candidate.suffix.lower() not in _THEME_AUDIO_EXTENSIONS:
        return ""
    return candidate.as_posix()


def _theme_sound_config(folder: Path, name: str) -> dict:
    sidecar = folder / f"{name}.json"
    try:
        if not sidecar.is_file() or sidecar.stat().st_size > 64 * 1024:
            return {"music": "", "sounds": {}}
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"music": "", "sounds": {}}
    if not isinstance(raw, dict):
        return {"music": "", "sounds": {}}
    music = _theme_relative_audio(raw.get("music"))
    sounds = {}
    raw_sounds = raw.get("sounds")
    if isinstance(raw_sounds, dict):
        for key in _THEME_SOUND_KEYS:
            relative = _theme_relative_audio(raw_sounds.get(key))
            if relative:
                sounds[key] = relative
    return {"music": music, "sounds": sounds}


def write_native_background(theme_name="") -> str:
    """Record the page background of the named theme (the built-in default when it has none)."""
    color = DEFAULT_NATIVE_BACKGROUND
    name = Path(str(theme_name or "")).stem
    if name:
        for folder in (DATA.parent / "themes", _STOCK_THEMES):
            path = folder / f"{name}.css"
            if not path.is_file():
                continue
            try:
                match = _THEME_BACKGROUND.search(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                match = None
            if match:
                color = match.group(1).lower()
            break
    atomic_write_text(DATA.parent / NATIVE_BACKGROUND_FILE, color + "\n", mode=0o644)
    return color


class ExtensionsHandlers:
    @route("GET", "/api/theme.css")
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

    @route("GET", "/api/theme-sounds")
    def _api_get_api_theme_sounds(self, parsed):
        name = parse_qs(parsed.query).get("name", [""])[0]
        folder = _theme_folder(name)
        if folder is None:
            self.send_json(200, {"music": "", "sounds": {}})
            return
        self.send_json(200, _theme_sound_config(folder, name))

    @route("GET", "/api/theme-asset")
    def _api_get_api_theme_asset(self, parsed):
        query = parse_qs(parsed.query)
        name = query.get("name", [""])[0]
        relative = query.get("path", [""])[0]
        folder = _theme_folder(name)
        limit = _THEME_ASSET_LIMIT
        if folder is None or Path(relative).suffix.lower() not in _THEME_AUDIO_EXTENSIONS:
            self.send_json(404, {"error": "Theme sound asset not found."})
            return
        try:
            asset = contained_path(folder, relative)
        except ValueError:
            self.send_json(404, {"error": "Theme sound asset not found."})
            return
        try:
            if not asset.is_file() or asset.stat().st_size > limit:
                self.send_json(404, {"error": "Theme sound asset not found."})
                return
        except OSError:
            self.send_json(404, {"error": "Theme sound asset not found."})
            return
        self.send_file(200, asset, mimetypes.guess_type(asset.name)[0], cache_control="private, max-age=3600")
        return

    @route("GET", "/api/themes")
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

    @route("GET", "/api/plugins")
    def _api_get_api_plugins(self, parsed):
        self.send_json(200, {
            "plugins": list_plugins(DATA.parent / "plugins"),
            "api_version": 1,
            "sandbox": sandbox_status(),
        })
        return

    @route("GET", "/api/v2/plugins/commands")
    def _api_get_api_v2_plugins_commands(self, parsed):
        """Palette commands declared by enabled plugins plus host builtins."""
        self.send_json(200, {
            "api_version": 1,
            "sandbox": sandbox_status(),
            "commands": plugin_commands(DATA.parent / "plugins"),
            "epoch": PLUGIN_EPOCH["value"],
        })
        return

    @route("POST", "/api/v2/plugins/command")
    def _api_post_api_v2_plugins_command(self, payload):
        """Run one palette command through the sandboxed plugin runner."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("plugin_id") or "").strip()
        command = str(body.get("command") or "").strip()
        if not plugin_id or not command:
            raise ValueError("plugin_id and command are required.")
        try:
            manifest = read_manifest(DATA.parent / "plugins" / plugin_id)
        except ValueError as error:
            raise ValueError(str(error)) from error
        declared = {entry.get("id") for entry in manifest.get("commands") or []}
        if command not in declared:
            raise ValueError("This plugin does not declare that command.")
        enabled = next((item.get("enabled") for item in list_plugins(DATA.parent / "plugins") if item.get("id") == plugin_id), False)
        if os.environ.get("OPENBOX_SAFE_MODE") or not enabled:
            raise ValueError("Plugin is disabled.")
        state = load_state_view()
        library = [
            {
                "game_id": str(game.get("game_id") or ""),
                "name": str(game.get("name") or ""),
                "platform": str(game.get("platform") or ""),
                "progress": str(game.get("progress") or ""),
                "favorite": bool(game.get("favorite")),
                "playtime_seconds": int(game.get("playtime_seconds") or 0),
            }
            for game in (state.get("games") or [])[:MAX_LIBRARY_ENTRIES]
        ]
        result, error = run_plugin_hook(
            DATA.parent / "plugins", plugin_id, "command",
            {"command": command, "library": library},
        )
        if result is None and error:
            raise ValueError(error)
        notification = result.get("notification") if isinstance(result, dict) else None
        self.send_json(200, {"ok": True, "plugin_id": plugin_id, "command": command, "result": result, "notification": notification})
        return

    @route("GET", "/api/plugins/catalog")
    def _api_get_api_plugins_catalog(self, parsed):
        self.send_json(200, {"catalog": fetch_plugin_catalog()})
        return

    # --- Plugins 2.0 (F1a/F1b/F1c/F1f): trust, permissions, settings, browser.
    # The v1 surface stays frozen; everything new lives under /api/v2.
    # (Static paths: this router has no path parameters, so the plugin id
    # travels in the query string for GET and the JSON body for POST.)

    @route("GET", "/api/v2/plugins/trust")
    def _api_get_api_v2_plugins_trust(self, parsed):
        """Trust status for one plugin (F1a): granted, checksum, match."""
        plugin_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        read_manifest(DATA.parent / "plugins" / plugin_id)  # validates
        self.send_json(200, {"id": plugin_id, **plugin_trust_status(DATA.parent / "plugins", plugin_id)})
        return

    @route("POST", "/api/v2/plugins/trust")
    def _api_post_api_v2_plugins_trust(self, payload):
        """Grant or revoke unsandboxed-execution trust for one plugin (F1a)."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        if "trusted" not in body:
            raise ValueError("trusted is required.")
        trusted = set_plugin_trust(DATA.parent / "plugins", plugin_id, bool(body.get("trusted")))
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"id": plugin_id, "trusted": trusted})
        return

    @route("POST", "/api/v2/plugins/permissions")
    def _api_post_api_v2_plugins_permissions(self, payload):
        """Record a user permission grant for one plugin (F1b)."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        granted = set_plugin_permissions(DATA.parent / "plugins", plugin_id, body.get("permissions") or [])
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"id": plugin_id, "permissions": granted})
        return

    @route("GET", "/api/v2/plugins/settings")
    def _api_get_api_v2_plugins_settings(self, parsed):
        """Settings schema + redacted values for one plugin (F1c/P9.2)."""
        plugin_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        self.send_json(200, {
            "id": plugin_id,
            **get_plugin_settings_public(DATA.parent / "plugins", plugin_id),
            "credentials": plugin_credentials_summary(DATA.parent / "plugins", plugin_id),
        })
        return

    @route("POST", "/api/v2/plugins/settings")
    def _api_post_api_v2_plugins_settings(self, payload):
        """Validate and store per-plugin settings (F1c); blank secrets are kept."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        values = body.get("values") or {}
        if not isinstance(values, dict):
            raise ValueError("values must be an object.")
        public = get_plugin_settings_public(DATA.parent / "plugins", plugin_id)
        current = get_plugin_settings(DATA.parent / "plugins", plugin_id)["values"]
        secret_names = {
            name for name, field in (public["schema"].get("properties") or {}).items()
            if isinstance(field, dict) and field.get("format") == "password"
        }
        clear = body.get("clear_secrets") or []
        if not isinstance(clear, list):
            raise ValueError("clear_secrets must be a list.")
        for name in secret_names:
            value = values.get(name)
            if name in clear:
                values.pop(name, None)
            elif value in (None, "", "••••") and current.get(name):
                values[name] = current[name]
        set_plugin_settings(DATA.parent / "plugins", plugin_id, values)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {
            "id": plugin_id,
            **get_plugin_settings_public(DATA.parent / "plugins", plugin_id),
            "credentials": plugin_credentials_summary(DATA.parent / "plugins", plugin_id),
        })
        return

    @route("GET", "/api/v2/plugins/credentials")
    def _api_get_api_v2_plugins_credentials(self, parsed):
        """Credential key names only; values never leave the host."""
        plugin_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        self.send_json(200, {
            "id": plugin_id,
            "credentials": plugin_credentials_summary(DATA.parent / "plugins", plugin_id),
        })
        return

    @route("GET", "/api/v2/plugins/data")
    def _api_get_api_v2_plugins_data(self, parsed):
        """Read one plugin's data bag (never its credentials)."""
        plugin_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        self.send_json(200, {
            "id": plugin_id,
            "data": plugin_data_values(DATA.parent / "plugins", plugin_id),
        })
        return

    @route("POST", "/api/v2/plugins/data")
    def _api_post_api_v2_plugins_data(self, payload):
        """Shallow-merge a bounded patch into one plugin's data bag."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        values = body.get("values") or {}
        merged = set_plugin_data(DATA.parent / "plugins", plugin_id, values)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"id": plugin_id, "data": merged})
        return

    @route("POST", "/api/v2/plugins/repair")
    def _api_post_api_v2_plugins_repair(self, payload):
        """Reinstall one plugin from its catalog entry without touching its records."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        if not plugin_id:
            raise ValueError("Plugin id is required.")
        root = DATA.parent / "plugins"
        read_manifest(root / plugin_id)  # validates the installed package
        try:
            catalog = fetch_plugin_catalog()
        except (OSError, ValueError):
            catalog = []
        entry = next((item for item in catalog if item.get("id") == plugin_id), None)
        if entry and not entry.get("local_only"):
            with tempfile.TemporaryDirectory(dir=DATA.parent) as temporary:
                archive = download_plugin_package(entry, temporary)
                manifest = install_plugin(archive, root)
            _forget_plugin_source(root, manifest["id"])
        else:
            manifest = install_plugin(_installed_local_package(root, plugin_id), root)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"plugin": manifest, "repaired": True})
        return

    @route("GET", "/api/v2/plugin-stats")
    def _api_get_api_v2_plugin_stats(self, parsed):
        self.send_json(200, {"sources": plugin_stats_summaries(DATA.parent)})
        return

    @route("POST", "/api/v2/plugin-stats/refresh")
    def _api_post_api_v2_plugin_stats_refresh(self, payload):
        body = payload if isinstance(payload, dict) else {}
        plugin_ids = body.get("plugin_ids")
        if plugin_ids is None:
            plugin_id = str(body.get("plugin_id") or "").strip()
            plugin_ids = [plugin_id] if plugin_id else []
        if not isinstance(plugin_ids, list) or len(plugin_ids) > 64:
            raise ValueError("plugin_ids must be a list of at most 64 ids.")
        state = load_state_view()
        results = []
        for plugin_id in plugin_ids:
            plugin_id = str(plugin_id or "").strip()
            if not plugin_id:
                continue
            try:
                results.append(refresh_plugin_stats(plugin_id, state, data_dir=DATA.parent, timeout=15))
            except (OSError, ValueError) as error:
                results.append({"plugin_id": plugin_id, "error": str(error)})
        if results:
            from pkg.state.cache import invalidate_state_cache

            invalidate_state_cache()
        self.send_json(200, {"results": results})
        return

    @route("POST", "/api/v2/plugins/credentials")
    def _api_post_api_v2_plugins_credentials(self, payload):
        """Write capability credentials; values never travel back to the browser."""
        body = payload if isinstance(payload, dict) else {}
        plugin_id = str(body.get("id") or "").strip()
        keys = set_plugin_credentials(
            DATA.parent / "plugins",
            plugin_id,
            body.get("values") or {},
            clear=body.get("clear") or [],
        )
        self.send_json(200, {"id": plugin_id, "credentials": {"keys": keys, "set": {key: True for key in keys}}})
        return

    @route("GET", "/api/v2/plugins/catalog")
    def _api_get_api_v2_plugins_catalog(self, parsed):
        """Community catalog joined with installed state (F1f browser)."""
        installed = {
            manifest["id"]: manifest
            for manifest in list_plugins(DATA.parent / "plugins")
            if manifest.get("valid")
        }
        entries = []
        for entry in fetch_plugin_catalog():
            plugin_id = str(entry.get("id") or "")
            current = installed.get(plugin_id)
            catalog_version = str(entry.get("version") or "")
            entries.append({
                **entry,
                "installed": current is not None,
                "installed_version": str(current.get("version") or "") if current else "",
                "update_available": bool(current) and catalog_version != str(current.get("version") or ""),
            })
        self.send_json(200, {"catalog": entries, "sandbox": sandbox_status()})
        return

    @route("GET", "/api/webhooks")
    def _api_get_api_webhooks(self, parsed):
        state = load_state_view()
        self.send_json(200, {"webhooks": public_webhook_configs(state), "events": list(EVENT_TYPES), "attempts": int(state.get("settings", {}).get("webhook_attempts") or DEFAULT_ATTEMPTS), "timeout": int(state.get("settings", {}).get("webhook_timeout") or DEFAULT_TIMEOUT)})
        return

    @route("POST", "/api/webhooks")
    def _api_post_api_webhooks(self, payload):
        self.save_webhooks(payload)

    @route("POST", "/api/webhooks/test")
    def _api_post_api_webhooks_test(self, payload):
        self.test_webhook(payload)

    @route("POST", "/api/plugins/catalog/install")
    def _api_post_api_plugins_catalog_install(self, payload):
        self.install_catalog_plugin(payload)

    @route("POST", "/api/themes/open-folder")
    def _api_post_api_themes_open_folder(self, payload):
        self.open_themes_folder()

    @route("POST", "/api/plugins/install")
    def _api_post_api_plugins_install(self, payload):
        self.install_plugin(payload)

    @route("POST", "/api/plugins/toggle")
    def _api_post_api_plugins_toggle(self, payload):
        self.toggle_plugin(payload)

    @route("POST", "/api/plugins/remove")
    def _api_post_api_plugins_remove(self, payload):
        self.remove_plugin(payload)

    @route("POST", "/api/themes/select")
    def _api_post_api_themes_select(self, payload):
        self.select_theme(payload)

    @route("POST", "/api/themes/import")
    def _api_post_api_themes_import(self, payload):
        self.import_theme(payload)

    @route("POST", "/api/playlists")
    def _api_post_api_playlists(self, payload):
        self.save_playlist(payload)

    @route("POST", "/api/playlists/delete")
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
        root = DATA.parent / "plugins"
        source = str(payload.get("path", ""))
        manifest = install_plugin(source, root)
        _remember_plugin_source(root, manifest["id"], source)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"plugin":manifest})

    def toggle_plugin(self, payload):
        plugin_id = str(payload.get("id", ""))
        enabled = set_plugin_enabled(
            DATA.parent / "plugins",
            plugin_id,
            bool(payload.get("enabled")),
        )
        if not enabled:
            self._drop_plugin_games(plugin_id)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"enabled":enabled})

    def remove_plugin(self, payload):
        root = DATA.parent / "plugins"
        plugin_id = str(payload.get("id", ""))
        plugin_id = remove_plugin(root, plugin_id)
        _forget_plugin_source(root, plugin_id)
        self._drop_plugin_games(plugin_id)
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"removed":plugin_id})

    def _drop_plugin_games(self, plugin_id):
        """Remove persisted games owned by a disabled/removed plugin.

        The public projection also hides these games, but leaving stale rows in
        state would keep them in backups, search indices, and export data.
        ``transact_state`` emits the usual ``game_removed`` lifecycle event.
        """
        if not plugin_id:
            return 0

        def mutate(state):
            kept = []
            removed = 0
            for game in state.get("games", []):
                if isinstance(game, dict) and str(game.get("plugin_source") or "") == plugin_id:
                    removed += 1
                    continue
                kept.append(game)
            state["games"] = kept
            return removed

        _, removed = transact_state(mutate)
        return removed

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
        if not platform:
            write_native_background(name)
        self.send_json(200, {"selected":name, "platform":platform})

    def import_theme(self, payload):
        source = Path(str(payload.get("path", ""))).expanduser()
        if not source.is_file() or source.suffix.lower() != ".css":
            raise ValueError("Theme path must point to a CSS file.")
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
            raise ValueError("Theme path may not contain symlinks.")
        try:
            size = source.stat().st_size
        except OSError as error:
            raise ValueError("Could not inspect theme file.") from error
        if size > 256 * 1024:
            raise ValueError("Theme file is too large (max 256 KiB).")
        # Basic content check: reject remote @import that would bypass CSP
        try:
            text = source.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError("Theme file is not valid UTF-8 CSS.") from error
        lowered = text.lower()
        if "@import" in lowered and "http" in lowered:
            raise ValueError("Theme CSS may not contain remote @import.")
        if len(text.encode("utf-8")) > 256 * 1024:
            raise ValueError("Theme file is too large (max 256 KiB).")
        destination = DATA.parent / "themes" / source.name
        if destination.is_symlink():
            raise ValueError("Theme destination may not be a symlink.")
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
        from pkg.parity.parity_filter_presets import normalize_rules

        clean = normalize_rules(rules) if playlist_type == "filter" else {}
        state = load_state()
        members = payload.get("members", payload.get("ids", []))
        if not isinstance(members, list) or len(members) > 100000:
            raise ValueError("Playlist members must be a list.")
        member_ids = []
        for value in members:
            if isinstance(value, dict):
                game = game_from_payload(state, value)
            elif isinstance(value, int) and not isinstance(value, bool):
                game = game_from_payload(state, {"id": value})
            elif isinstance(value, str) and value.isdigit():
                try:
                    game = game_from_payload(state, {"game_id": value})
                except IndexError:
                    game = game_from_payload(state, {"id": int(value)})
            else:
                game = game_from_payload(state, {"game_id": str(value)})
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
        from api_errors import NotFound

        name = str(payload.get("name", "")).strip()
        removed = []

        def mutate(state):
            nonlocal removed
            kept = []
            for item in state.get("playlists", []):
                if isinstance(item, dict) and item.get("name") == name:
                    removed.append(item)
                else:
                    kept.append(item)
            state["playlists"] = kept
        transact_state(mutate)
        if not removed:
            raise NotFound(f"Playlist not found: {name or '(blank)'}")
        self.send_json(200, {"deleted": name})

    def install_catalog_plugin(self, payload):
        catalog = fetch_plugin_catalog()
        plugin_id = str(payload.get("id", "")).strip()
        entry = next((item for item in catalog if item.get("id") == plugin_id), None)
        if not entry:
            raise ValueError("Unknown catalog plugin.")
        if entry.get("local_only"):
            raise ValueError("This catalog entry is documentation-only. Install local plugin packages manually.")
        root = DATA.parent / "plugins"
        with tempfile.TemporaryDirectory(dir=DATA.parent) as temporary:
            archive = download_plugin_package(entry, temporary)
            manifest = install_plugin(archive, root)
        _forget_plugin_source(root, manifest["id"])
        PLUGIN_EPOCH["value"] += 1
        self.send_json(200, {"plugin": manifest})

    def open_themes_folder(self):
        folder = DATA.parent / "themes"
        ensure_stock_themes(folder, ROOT)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            open_path(folder)
        except OSError as error:
            raise FileNotFoundError("No application is available to open folders.") from error
        self.send_json(200, {"path": str(folder)})


