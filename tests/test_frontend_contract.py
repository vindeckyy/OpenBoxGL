#!/usr/bin/env python3
"""Frontend contract: var(--*) defined, surface-deep in themes, app shell markup."""
import re
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "static" / "app.css"
INDEX = ROOT / "index.html"
THEMES = sorted((ROOT / "themes").glob("*.css"))

TOOL_GROUPS = {
    "library": [
        "metadataButton", "mediaButton", "healthButton", "constellationButton", "masteryButton", "bulkButton", "tagsButton",
        "playlistsButton", "backupButton", "historyButton", "timeMachineButton", "achievementsButton",
        "saveFilterButton", "savePresetButton",
    ],
    "sources": [
        "storefrontButton", "emulatorsButton", "steamButton", "heroicButton",
        "lutrisButton", "arcadeButton", "arcadeRoomButton", "householdButton", "discoveryButton",
    ],
    "personalize": ["themesButton", "pluginsButton", "settingsButton", "whatsNewButton", "fullscreenButton"],
    "automation": ["webhooksButton", "notificationsButton"],
}

DIALOGS = ROOT / "static" / "dialogs.js"
APP_JS = ROOT / "static" / "app.js"
STATE_JS = ROOT / "static" / "state.js"
EVENTS_JS = ROOT / "static" / "events.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
ACTIVITY_JS = ROOT / "static" / "activity.js"
MEDIA_JS = ROOT / "static" / "media.js"
LIBRARY_JS = ROOT / "static" / "library.js"
UTIL_JS = ROOT / "static" / "util.js"
SETTINGS_JS = ROOT / "static" / "settings.js"
BIGBOX_JS = ROOT / "static" / "bigbox.js"
PICKER_JS = ROOT / "static" / "picker.js"
INSIGHTS_JS = ROOT / "static" / "insights.js"
CONSTELLATION_JS = ROOT / "static" / "constellation.js"

GAME_DIALOG_PATH_FIELDS = [
    "path", "cover", "background", "video", "music", "video_snap", "video_theme",
    "video_trailer", "video_recording", "clear_logo", "fanart", "banner", "icon",
    "box_back", "box_spine", "box_3d", "title_screen", "cart_front", "cart_back",
    "disc", "advertisement", "manual", "screenshots", "documents", "save_paths",
    "applications", "versions",
]

ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)
VAR_DEF_RE = re.compile(r"--([\w-]+)\s*:")
VAR_USE_RE = re.compile(r"var\(--([\w-]+)")
IGNORED_DYNAMIC = {"motion-index", "coverflow-offset"}

def parse_root_vars(css_text: str):
    m = ROOT_BLOCK_RE.search(css_text)
    if not m:
        return set()
    return set(VAR_DEF_RE.findall(m.group(0)))

def find_vars_used_outside_root(css_text: str):
    without = ROOT_BLOCK_RE.sub("", css_text)
    return set(v for v in VAR_USE_RE.findall(without) if v not in IGNORED_DYNAMIC)

def test_app_vars_defined():
    css = APP.read_text()
    defs = parse_root_vars(css)
    used = find_vars_used_outside_root(css)
    missing = sorted(used - defs)
    assert not missing, f"app.css uses vars not defined in :root: {missing}\n defined: {sorted(defs)}"


def test_bigbox_video_snap_css():
    """Video snap class exists and is hidden under reduced-motion."""
    css = APP.read_text()
    assert ".bigbox-video-snap" in css, "bigbox-video-snap CSS class missing"
    assert "prefers-reduced-motion" in css, "reduced-motion media query missing"


def test_bigbox_video_snap_js():
    """bigbox.js has scheduleVideoSnap and clearVideoSnap functions."""
    js = (ROOT / "static" / "bigbox.js").read_text()
    assert "function scheduleVideoSnap" in js, "scheduleVideoSnap missing"
    assert "function clearVideoSnap" in js, "clearVideoSnap missing"
    assert "prefers-reduced-motion" not in js or "_reducedMotion" in js, "reduced-motion check missing"

def test_themes_surface_deep():
    missing_themes = []
    for p in THEMES:
        css = p.read_text()
        defs = parse_root_vars(css)
        if "surface-deep" not in defs:
            missing_themes.append(p.name)
    assert not missing_themes, f"themes missing --surface-deep: {missing_themes}"

def test_themes_vars_defined():
    app_defs = parse_root_vars(APP.read_text())
    for p in THEMES:
        css = p.read_text()
        defs = parse_root_vars(css) | app_defs
        used = find_vars_used_outside_root(css)
        missing = sorted(used - defs)
        assert not missing, f"{p.name} uses undefined vars: {missing}"

def _tool_menu_groups(html: str):
    tool_menu = re.search(r'id="toolMenu"[^>]*>(.*?)</div>\s*</div>\s*</nav>', html, re.DOTALL)
    assert tool_menu, "missing #toolMenu"
    groups = {}
    for block in re.finditer(
        r'<div[^>]*data-tool-group="([^"]+)"[^>]*>(.*?)</div>',
        tool_menu.group(1),
        re.DOTALL,
    ):
        key = block.group(1)
        ids = re.findall(r'\bid="(\w+)"', block.group(2))
        groups[key] = ids
    return groups

def test_tool_menu_group_membership():
    html = INDEX.read_text()
    groups = _tool_menu_groups(html)
    for key, expected in TOOL_GROUPS.items():
        assert key in groups, f"missing data-tool-group={key!r}"
        assert groups[key] == expected, f"{key} group ids {groups[key]!r} != {expected!r}"

def test_time_machine_ui_surface():
    html = INDEX.read_text()
    js = (ROOT / "static" / "timemachine.js").read_text()
    app = APP_JS.read_text()
    assert 'id="timeMachineDialog"' in html
    assert 'id="timeMachineButton"' in html
    assert "openTimeMachine" in app
    assert "/api/v2/library/time-machine/events" in js
    assert "/api/v2/library/time-machine/as-of" in js
    assert "/api/v2/library/time-machine/revert" in js
    assert "confirmAction" in js

def test_clip_deeplink_ui_surface():
    app = APP_JS.read_text()
    clips = (ROOT / "static" / "clips.js").read_text()
    assert "import { openClip } from './clips.js';" in app
    assert "['clip', params => openClip(params.get('id'))]" in app
    assert "function openClip(clipId)" in clips
    assert "app:show-game" in clips
    assert "momentsTab" in clips

def test_deeplink_dispatch_uses_explicit_allowlist():
    app = APP_JS.read_text()
    actions = re.search(
        r"const DEEPLINK_ACTIONS = new Map\(\[(.*?)\n    \]\);",
        app,
        re.DOTALL,
    )
    assert actions, "deeplink actions must use an explicit Map"
    action_names = set(re.findall(r"^\s*\['([^']+)',", actions.group(1), re.MULTILINE))
    assert action_names == {"bigbox", "settings", "showgame", "recap", "moment", "clip"}

    dispatch = _function_body(app, "dispatchDeeplink")
    assert "DEEPLINK_ACTIONS.get(params.get('deeplink'))" in dispatch
    assert "DEEPLINK_ACTIONS[" not in dispatch
    assert "if (typeof action === 'function') action(params);" in dispatch

def test_game_dialog_path_browse_hosts():
    html = INDEX.read_text()
    game_dialog = re.search(r'id="gameDialog"[^>]*>(.*?)</dialog>', html, re.DOTALL)
    assert game_dialog, "missing #gameDialog"
    body = game_dialog.group(1)
    missing = []
    for name in GAME_DIALOG_PATH_FIELDS:
        field = re.search(
            rf'name="{re.escape(name)}"[^>]*>(?:[^<]*</(?:input|textarea)>)?',
            body,
        )
        if not field:
            missing.append(name)
            continue
        window = body[max(0, field.start() - 400):field.end() + 200]
        if not re.search(r'class="[^"]*path-browse[^"]*"[^>]*data-browse-for="' + re.escape(name) + r'"', window):
            missing.append(name)
    assert not missing, f"#gameDialog path fields missing .path-browse host: {missing}"


def test_shelf_entry_editor_and_actions():
    html = INDEX.read_text()
    assert 'id="addShelfButton"' in html
    assert 'name="entry_type"' in html
    assert 'value="shelf"' in html
    dialogs = (ROOT / "static" / "dialogs.js").read_text()
    app = (ROOT / "static" / "app.js").read_text()
    library = (ROOT / "static" / "library.js").read_text()
    assert "manual-entry/update" in app
    assert "manual-entry/convert" in dialogs
    assert "Set up launch" in library
    assert "game.manual_entry" in library

def test_statistics_sync_status_uses_current_label():
    text = (ROOT / "static" / "settings.js").read_text()
    assert "const cloudLabel = AppState.appSettings.cloud_sync_beta ? 'Statistics sync (beta)' : 'Statistics sync';" in text
    assert "const cloudBeta = AppState.appSettings.cloud_sync_beta ? ' (beta)' : ' (beta)';" not in text
    assert "Cloud sync (beta)" not in text

def test_bulk_edit_chunks_large_selections():
    # MAX_BODY is 64 KB; an unchunked shift-range selection fails at ~10k ids.
    text = APP_JS.read_text()
    assert "BULK_EDIT_CHUNK" in text
    assert "ids: ids.slice(start, start + BULK_EDIT_CHUNK)" in text
    assert "ids:[...selectedIds],changes}" not in text

def test_f05_dialogs_no_window_prompt():
    text = DIALOGS.read_text()
    assert "window.prompt" not in text
    assert "window.confirm" not in text
    assert "promptInput" in text
    assert "confirmAction" in text
    assert "bindContextMenuA11y" in text

def test_f05_app_js_context_menu_a11y():
    text = APP_JS.read_text()
    assert "bindContextMenuA11y" in text
    assert "addEventListener('contextmenu'" not in text
    assert "prompt(" not in text
    assert "confirm(" not in text
    assert re.search(r"from '\./dialogs\.js'", text), "app.js must import from dialogs.js"
    if "promptInput(" in text:
        assert re.search(r"import\s*\{[^}]*\bpromptInput\b", text), (
            "app.js uses promptInput but does not import it from dialogs.js"
        )

def test_shared_sse_stream_single_construction_site():
    """P1-3/P1-4: one shared EventSource with reconnect, fan-out, unload close."""
    constructors = [
        js.name for js in sorted((ROOT / "static").glob("*.js"))
        if "new EventSource" in js.read_text()
    ]
    assert constructors == ["events.js"], f"EventSource constructed in {constructors}"
    events = EVENTS_JS.read_text()
    assert "onServerEvent" in events, "events.js must expose onServerEvent"
    assert "closeEventStream" in events, "events.js must expose closeEventStream"
    assert "beforeunload" in events and "pagehide" in events, "events.js must close on unload"
    assert "MAX_BACKOFF_MS" in events and "setTimeout" in events, "events.js must back off reconnects"
    for name, kinds in (
        ("sessions.js", ["session.started", "session.stopped", "session.state", "job.finished", "state.changed", "session.recap"]),
        ("activity.js", ["job.queued", "job.progress", "job.cancelling", "job.finished", "job.interrupted"]),
    ):
        text = (ROOT / "static" / name).read_text()
        assert "new EventSource" not in text, f"{name} still constructs its own EventSource"
        assert "from './events.js'" in text, f"{name} does not import the shared stream"
        for kind in kinds:
            assert kind in text, f"{name} no longer handles {kind}"


def test_details_layout_responsive():
    """P1-26: the detail layout branches on the same breakpoint as app.css."""
    lib = LIBRARY_JS.read_text()
    body = _function_body(lib, "applyDetailsLayout")
    assert "max-width:1100px" in body, "detail layout must branch at the media-query breakpoint"
    assert "150px 1fr 0" in body, "compact collapsed columns must match the media query"
    assert "190px minmax(520px,1fr)" in body, "desktop columns must be preserved"
    assert "190px minmax(520px,1fr) ${width}px" in body, "desktop track must follow the dragged width"
    css = APP.read_text()
    media_block = css[css.index("@media(max-width:1100px)"):css.index("@media(max-width:1100px)") + 200]
    assert "grid-template-columns:150px 1fr 340px" in media_block, "responsive workspace columns missing"


def test_platform_details_stale_guard():
    """P1-27: a late platform-documents response must not overwrite the pane."""
    body = _function_body(LIBRARY_JS.read_text(), "renderPlatformDetails")
    assert "platformDetailsRequest" in body, "missing request token"
    assert "AppState.selectedId !== null" in body, "missing selection guard"
    assert "AppState.platform !== platformName" in body, "missing platform guard"


def test_constellation_error_retry():
    """P1-28: failure hides the spinner and offers retry."""
    text = CONSTELLATION_JS.read_text()
    assert "showConstellationError" in text, "missing error renderer"
    assert "constellationRetry" in text, "missing retry button"
    assert "constellation.load_failed" in text, "missing localized failure string"
    assert "$('constellationLoading').hidden = true" in text, "spinner must be hidden on failure"


def test_bulk_media_watch_cancelled():
    """P1-29: bulk polling stops on dialog close and on job end."""
    text = MEDIA_JS.read_text()
    assert "function stopBulkMediaWatch" in text, "missing stopBulkMediaWatch"
    assert "addEventListener('close', stopBulkMediaWatch)" in text, "close listener missing"
    body = _function_body(text, "watchBulkMedia")
    assert "bulkMediaWatch" in body, "watch generation guard missing"
    assert "setTimeout(watchBulkMedia, 1200)" in body, "running job must keep polling"


def test_bigbox_snap_and_attract_overlays():
    """P1-30: close stops the video snap; attract skips pause/party overlays."""
    text = BIGBOX_JS.read_text()
    close = _function_body(text, "closeBigBox")
    assert "clearVideoSnap()" in close, "closeBigBox must clear the video snap"
    poll = _function_body(text, "pollGamepads")
    assert "$('bigBoxPause').hidden" in poll, "attract mode must skip the pause overlay"
    assert "partyOverlayOpen()" in poll, "attract mode must skip the party overlay"


def test_persistence_failures_surface():
    """P1-31: preference writes, profiles, and EmuMovies failures are visible."""
    lib = LIBRARY_JS.read_text()
    persist = _function_body(lib, "persistListSort")
    assert ".catch(() => {})" not in persist, "persistListSort still swallows failures"
    assert "notifyError(error)" in persist, "persistListSort must surface failures"
    for fn in ("$('grouping').onchange", "$('viewToggleButton').onclick"):
        assert fn in lib
    assert lib.count("notifyError(error)") >= 5
    state = STATE_JS.read_text()
    assert "profilesFetched = false" in state and "notifyError(error)" in state, "ensureProfiles must surface failures"
    settings = SETTINGS_JS.read_text()
    assert "emumoviesFailed" in settings, "EmuMovies failure must be tracked"
    assert "settings.saved_emumovies_failed" in settings, "saved toast must be conditional"


def test_double_submit_guards():
    """P1-32: network-triggering buttons use the setButtonBusy pattern."""
    settings = SETTINGS_JS.read_text()
    for ident in ("browsePluginCatalog", "installPlugin", "importTheme", "createNamedBackup",
                  "checkUpdate", "installUpdate", "syncCloud", "removeSteamGames"):
        assert ident in settings, f"settings.js lost {ident}"
    assert settings.count("setButtonBusy(button") >= 8, "network actions missing busy guards"
    assert "setButtonBusy(button, true, t('common.loading'))" in settings
    app = APP_JS.read_text()
    assert "bindImportButton" in app, "app.js import buttons missing a shared busy guard"
    assert "setButtonBusy(button, true, t('common.loading'))" in app
    picker = PICKER_JS.read_text()
    assert "setButtonBusy(spinButton" in picker, "picker spin missing busy guard"
    lib = LIBRARY_JS.read_text()
    assert "setButtonBusy($('viewToggleButton')" in lib, "view toggle missing busy guard"


def test_search_worker_nulled_on_error():
    """P1-34: a dead worker is dropped and recreated on the next search."""
    text = LIBRARY_JS.read_text()
    idx = text.index("w.onerror")
    assert "_searchWorker = null" in text[idx:idx + 500], "onerror must null the worker handle"


def test_insights_loaded_only_on_success():
    """P1-35: one transient failure must remain retryable."""
    text = INSIGHTS_JS.read_text()
    observer_block = text[text.index("new IntersectionObserver"):text.index("new IntersectionObserver") + 400]
    assert "loaded = true" not in observer_block, "observer marks loaded before the fetch resolves"
    catch_block = text[text.index("loaded = false"):text.index("loaded = false") + 120]
    assert "loaded = false" in catch_block


def test_sessions_poll_quiet_error_and_rendered_i18n():
    """P1-37: one toast per outage; chrome composed with t() at render time."""
    sessions = SESSIONS_JS.read_text()
    assert "sessionPollError" in sessions, "missing quiet-error state"
    assert "t('nav.running')" in sessions, "sessions button must compose a localized label"
    lib = LIBRARY_JS.read_text()
    assert "t('library.all_games')" in lib, "library title must be localized at render time"
    assert "t('library.games_count'" in lib, "library meta must be localized at render time"
    assert "t('library.bulk_selected'" in lib, "bulk meta must be localized at render time"


def test_safe_storage_and_format_date():
    """P1-38/P1-41: shared storage and date helpers are used in render paths."""
    util = UTIL_JS.read_text()
    assert "const safeStorage" in util, "safeStorage helper missing"
    assert "function formatDate" in util, "formatDate helper missing"
    assert "Intl.DateTimeFormat" in util, "formatDate must use Intl.DateTimeFormat"
    assert "localechange" in util, "formatDate cache must react to localechange"
    lib = LIBRARY_JS.read_text()
    assert "safeStorage.get(DETAILS_COLLAPSED_KEY)" in lib, "layout still reads localStorage unguarded"
    assert "formatDate(game.last_played)" in lib, "details pane must use formatDate"
    sessions = SESSIONS_JS.read_text()
    assert "formatDate(session.started)" in sessions
    recap = (ROOT / "static" / "recap.js").read_text()
    assert "formatDate(payload.started_at)" in recap
    settings = SETTINGS_JS.read_text()
    assert "formatDate(AppState.appSettings.last_cloud_sync)" in settings
    bigbox = BIGBOX_JS.read_text()
    assert "formatDate(session.started)" in bigbox
    insights = INSIGHTS_JS.read_text()
    assert "safeStorage.get(RANGE_KEY)" in insights
    assert "safeStorage.set(RANGE_KEY" in insights
    moments = (ROOT / "static" / "moments.js").read_text()
    assert "formatDate as formatTimestamp" in moments, "moments.js must reuse the shared formatter"
    assert "new Intl.DateTimeFormat" not in moments, "moments.js still has a private formatter"


def test_resize_handler_rAF_debounced():
    """P1-39: resize coalesces renders into one per animation frame."""
    text = LIBRARY_JS.read_text()
    idx = text.index("addEventListener('resize'")
    assert "requestAnimationFrame" in text[idx:idx + 500], "resize handler must rAF-debounce"


def test_named_dialogs_route_through_open_dialog():
    """P1-40: the flagged dialogs use the shared openDialog focus/stack logic."""
    settings = SETTINGS_JS.read_text()
    assert "openDialog($('settingsDialog'))" in settings
    media = MEDIA_JS.read_text()
    assert "openDialog($('mediaManagerDialog'))" in media
    sessions = SESSIONS_JS.read_text()
    assert "openDialog($('sessionsDialog'))" in sessions
    assert "openDialog($('historyDialog'))" in sessions


def test_remove_game_single_confirmation():
    """P1-42: one confirm with an explicit media-deletion checkbox."""
    body = _function_body(LIBRARY_JS.read_text(), "removeGame")
    assert body.count("confirmAction") == 1, "removeGame still chains two confirmations"
    assert "checkboxLabel" in body, "remove confirm must offer the media checkbox"
    assert "delete_media:Boolean(answer.checked)" in body


def test_facet_field_labels_localized():
    """P1-43: explorer tabs render translated labels, raw keys stay for the API."""
    text = STATE_JS.read_text()
    assert "t(`dialog.${name}`)" in text, "facet tab labels are still raw English"
    assert "explorer.no_values" in text, "facet empty state is still raw English"


def test_dialogs_open_dialog_exported():
    text = DIALOGS.read_text()
    assert "function openDialog(" in text and "openDialog," in text


def _function_body(source: str, name: str):
    m = re.search(rf"function {name}\([^)]*\)\s*\{{", source)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start:i - 1]

def test_f05_state_native_fallbacks_no_prompt():
    text = STATE_JS.read_text()
    for name in ("nativePickFolder", "nativePickFile", "nativePrompt", "nativeConfirm"):
        body = _function_body(text, name)
        assert body, f"missing function {name}"
        assert "prompt(" not in body, f"{name} still calls prompt()"


def test_m5_artwork_doctor_panel():
    text = LIBRARY_JS.read_text()
    assert "artworkDoctorDialog" in text, "missing Artwork Doctor panel"
    assert "/api/v2/steamgrid/hygiene/report" in text
    assert "/api/v2/steamgrid/hygiene/fix" in text
    assert "/api/v2/steamgrid/hygiene/undo" in text
    assert "artwork_doctor" in text, "artwork issues must be localized"
    palette = (ROOT / "static" / "palette.js").read_text()
    assert "tools.artwork_doctor" in palette
    css = APP.read_text()
    assert ".artwork-doctor-list" in css and "var(--" in css


def test_m5_setup_checklists_and_try_cards():
    text = (ROOT / "static" / "setup.js").read_text()
    assert "/api/v2/setup/checklists" in text, "setup checklists route missing"
    for token in ("setup.checklists_title", "setup.check_bios", "setup.check_emulator",
                  "setup.check_launch", "setup.check_artwork"):
        assert token in text, f"missing {token}"
    for token in ("setup.try_pick_game", "setup.try_radio", "setup.try_arcade", "setup.try_save_folder"):
        assert token in text, f"missing {token}"
    assert "renderChecklists" in text and "renderFinish" in text


def test_m5_palette_runs_plugin_commands():
    text = (ROOT / "static" / "palette.js").read_text()
    assert "/api/v2/plugins/commands" in text, "palette must load plugin commands"
    assert "/api/v2/plugins/command" in text, "palette must run plugin commands"
    assert "type === 'plugin'" in text


def test_m5_constellation_viewpoints_path_and_export():
    text = CONSTELLATION_JS.read_text()
    assert "bfsPath" in text, "missing BFS path helper"
    assert "toDataURL" in text, "missing PNG export"
    assert "VIEWPOINT_KEY" in text, "missing saved viewpoints"
    assert "pathEdges" in text, "missing path highlighting"
    for token in ("constellation.viewpoint_save", "constellation.path_find", "constellation.export_png"):
        assert token in text, f"missing {token}"
    css = APP.read_text()
    assert ".constellation-tools" in css


def test_m5_auto_moment_offer():
    text = (ROOT / "static" / "moments.js").read_text()
    assert "/api/v2/moments/auto-suggest" in text, "server trigger must be consulted"
    assert "offerMomentCapture" in text
    assert "moments.offer_title" in text


def test_m5_high_contrast_theme_installed():
    names = {path.stem for path in THEMES}
    assert "High Contrast" in names, "sixth stock theme missing"
    text = (ROOT / "themes" / "High Contrast.css").read_text()
    assert ":root {" in text and "--bg:" in text


def test_m5_signed_channel_and_background_update_settings():
    text = SETTINGS_JS.read_text()
    for token in ("/api/v2/emulators/defs/channel", "/api/v2/update/download",
                  "settings.defs_channel_title", "settings.bg_update_title"):
        assert token in text, f"missing {token}"
    assert "emulator_defs_update_enabled" in text
    assert "update_auto_download" in text
    plugin_manager = text[text.index("async function openPlugins"):]
    assert "plugins.invalid" in plugin_manager, "invalid plugins must be surfaced"
    assert "plugins.sandbox_" in plugin_manager



def test_m6_timeline_entries_are_buttons():
    """P7/P8: timeline rows are real buttons with labels and locale re-render."""
    timeline = (ROOT / "static" / "timeline.js").read_text()
    assert '<button type="button" class="timeline-entry"' in timeline, "timeline entries must be buttons"
    assert '<div class="timeline-entry"' not in timeline, "old clickable div still present"
    assert "timeline.open_game" in timeline, "entry button label key missing"
    assert "timeline.cover_label" in timeline, "cover label key missing"
    assert "onLocaleChange" in timeline, "timeline must re-render on locale change"
    css = APP.read_text()
    assert ".timeline-entry" in css


def test_m6_owned_dialogs_route_through_open_dialog():
    """P7: owned raw showModal() opens are routed through openDialog()."""
    for name in ("activity.js", "palette.js", "setup.js", "app.js"):
        text = (ROOT / "static" / name).read_text()
        assert ".showModal()" not in text, f"{name} still calls raw showModal()"
        assert "openDialog" in text, f"{name} must import openDialog"
    activity = ACTIVITY_JS.read_text()
    assert "closeDialog" in activity, "activity drawer close must restore focus"
    palette = (ROOT / "static" / "palette.js").read_text()
    assert "openDialog(paletteDialog" in palette
    setup = (ROOT / "static" / "setup.js").read_text()
    assert "openDialog(dialog" in setup and "closeDialog(dialog)" in setup


def test_m6_on_locale_change_migrations():
    """P8: owned JS surfaces subscribe to the onLocaleChange registry."""
    for name in ("setup.js", "activity.js", "insights.js", "palette.js", "timeline.js"):
        text = (ROOT / "static" / name).read_text()
        assert "onLocaleChange" in text, f"{name} does not use onLocaleChange"
    insights = INSIGHTS_JS.read_text()
    assert "addEventListener('localechange'" not in insights, "insights still binds the raw DOM event"


def test_m6_grid_a11y_hooks():
    """P7: #grid declares grid roles; util.js ships the adoption helpers."""
    html = INDEX.read_text()
    grid = re.search(r'<section class="grid" id="grid"[^>]*>', html)
    assert grid, "missing #grid"
    for attr in ('role="grid"', 'aria-rowcount', 'aria-colcount', 'data-i18n-aria-label="library.grid_label"'):
        assert attr in grid.group(0), f"#grid missing {attr}"
    util = UTIL_JS.read_text()
    assert "function applyGridA11y" in util, "applyGridA11y helper missing"
    assert "function gridCellAttrs" in util, "gridCellAttrs helper missing"
    assert "aria-rowindex" in util and "aria-colindex" in util
    assert "applyGridA11y" in util.split("export {")[-1], "helpers must be exported"
    css = APP.read_text()
    assert '.grid[role="grid"] [role="gridcell"]:focus-visible' in css, "gridcell focus style missing"


def test_m6_activity_live_region():
    """P7: job transitions announce in a dedicated live region."""
    html = INDEX.read_text()
    assert 'id="activityAnnouncer"' in html and 'aria-live="polite"' in html
    activity = ACTIVITY_JS.read_text()
    assert "function announceJobState" in activity
    assert "announce_started" in activity and "announce_done" in activity and "announce_failed" in activity
    assert "activityAnnouncer" in activity


def test_m6_setup_localized():
    """P8: setup.js routes every user-visible string through t()."""
    setup = (ROOT / "static" / "setup.js").read_text()
    assert "import { t, onLocaleChange } from './i18n.js';" in setup
    assert "setup.step_overview" in setup and "setup.save_close" in setup
    assert "cont.textContent = state.step >= 8 ? t('common.done') : t('setup.continue');" in setup
    assert "throw new Error('Add at least one source before scanning.');" not in setup
    assert "labelKey" in setup, "source labels must be locale keys, not baked English"
    for key in ("setup.source_folder", "setup.pick_folder_path", "setup.view_imported", "setup.finish_"):
        assert key in setup, f"missing {key}"


def test_m6_palette_launch_doctor_and_help_index():
    """P6: `>` runs Launch Doctor and `?` searches a generated help index."""
    palette = (ROOT / "static" / "palette.js").read_text()
    assert "tools.launch_doctor" in palette
    assert "event: 'launch-doctor'" in palette
    assert "function helpRows(" in palette
    assert "SHORTCUT_RUNNERS" in palette, "help rows must resolve to real actions"
    app = APP_JS.read_text()
    assert "app:palette-launch-doctor" in app
    assert "openSetupCenter({ step: 5 })" in app
    assert "prefersReducedMotion" in palette


def test_m6_reduced_motion_owned_selectors():
    """P7: a reduced-motion rule covers the owned surfaces."""
    css = APP.read_text()
    blocks = re.findall(r"@media \(prefers-reduced-motion:reduce\) \{(.*?)\n    \}", css, re.DOTALL)
    assert blocks, "no reduced-motion block found"
    combined = "\n".join(blocks)
    for selector in (".setup-step-item", ".palette-row", ".activity-row", ".timeline-entry", ".insight-card"):
        assert selector in combined, f"{selector} not covered by reduced motion"
    assert "animation:none !important" in combined
    assert ".sr-only" in css, "screen-reader-only utility missing"


def test_m6_contributing_locale_docs_and_floors():
    """Docs: locale contribution section and current coverage floors."""
    doc = (ROOT / "docs" / "CONTRIBUTING.md").read_text()
    assert "73.0%" in doc, "stale web_app coverage floor"
    assert "58.0%" not in doc, "old web_app floor still documented"
    assert "make check-ci" in doc, "check-ci target not documented"
    assert "locales/en.json" in doc and "check_i18n.py" in doc, "community locale guide missing"


if __name__ == "__main__":
    tests = sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed.append(name)
            print(f"FAIL {name}: {e}")
    if failed:
        print(f"SOME FAIL: {', '.join(failed)}")
        raise SystemExit(1)
    print(f"ALL PASS ({len(tests)} tests)")
