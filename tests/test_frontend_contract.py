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
SESSIONS_JS = ROOT / "static" / "sessions.js"
LIBRARY_JS = ROOT / "static" / "library.js"
BIGBOX_JS = ROOT / "static" / "bigbox.js"
NAVIGATION_JS = ROOT / "static" / "navigation.js"
ARCADEROOM_JS = ROOT / "static" / "arcaderoom.js"

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
    css = APP.read_text(encoding="utf-8")
    defs = parse_root_vars(css)
    used = find_vars_used_outside_root(css)
    missing = sorted(used - defs)
    assert not missing, f"app.css uses vars not defined in :root: {missing}\n defined: {sorted(defs)}"


def test_bigbox_video_snap_css():
    """Video snap class exists and is hidden under reduced-motion."""
    css = APP.read_text(encoding="utf-8")
    assert ".bigbox-video-snap" in css, "bigbox-video-snap CSS class missing"
    assert "prefers-reduced-motion" in css, "reduced-motion media query missing"


def test_bigbox_video_snap_js():
    """bigbox.js has scheduleVideoSnap and clearVideoSnap functions."""
    js = (ROOT / "static" / "bigbox.js").read_text(encoding="utf-8")
    assert "function scheduleVideoSnap" in js, "scheduleVideoSnap missing"
    assert "function clearVideoSnap" in js, "clearVideoSnap missing"
    assert "prefers-reduced-motion" not in js or "_reducedMotion" in js, "reduced-motion check missing"

def test_themes_surface_deep():
    missing_themes = []
    for p in THEMES:
        css = p.read_text(encoding="utf-8")
        defs = parse_root_vars(css)
        if "surface-deep" not in defs:
            missing_themes.append(p.name)
    assert not missing_themes, f"themes missing --surface-deep: {missing_themes}"

def test_themes_vars_defined():
    app_defs = parse_root_vars(APP.read_text(encoding="utf-8"))
    for p in THEMES:
        css = p.read_text(encoding="utf-8")
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
    html = INDEX.read_text(encoding="utf-8")
    groups = _tool_menu_groups(html)
    for key, expected in TOOL_GROUPS.items():
        assert key in groups, f"missing data-tool-group={key!r}"
        assert groups[key] == expected, f"{key} group ids {groups[key]!r} != {expected!r}"

def test_time_machine_ui_surface():
    html = INDEX.read_text(encoding="utf-8")
    js = (ROOT / "static" / "timemachine.js").read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    assert 'id="timeMachineDialog"' in html
    assert 'id="timeMachineButton"' in html
    assert "openTimeMachine" in app
    assert "/api/v2/library/time-machine/events" in js
    assert "/api/v2/library/time-machine/as-of" in js
    assert "/api/v2/library/time-machine/revert" in js
    assert "confirmAction" in js

def test_clip_deeplink_ui_surface():
    app = APP_JS.read_text(encoding="utf-8")
    clips = (ROOT / "static" / "clips.js").read_text(encoding="utf-8")
    assert "import { openClip } from './clips.js';" in app
    assert "['clip', params => openClip(params.get('id'))]" in app
    assert "function openClip(clipId)" in clips
    assert "app:show-game" in clips
    assert "momentsTab" in clips

def test_deeplink_dispatch_uses_explicit_allowlist():
    app = APP_JS.read_text(encoding="utf-8")
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
    html = INDEX.read_text(encoding="utf-8")
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
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="addShelfButton"' in html
    assert 'name="entry_type"' in html
    assert 'value="shelf"' in html
    dialogs = (ROOT / "static" / "dialogs.js").read_text(encoding="utf-8")
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    assert "manual-entry/update" in app
    assert "manual-entry/convert" in dialogs
    assert "Set up launch" in library
    assert "game.manual_entry" in library

def test_statistics_sync_status_uses_current_label():
    text = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    assert "const cloudLabel = AppState.appSettings.cloud_sync_beta ? 'Statistics sync (beta)' : 'Statistics sync';" in text
    assert "const cloudBeta = AppState.appSettings.cloud_sync_beta ? ' (beta)' : ' (beta)';" not in text
    assert "Cloud sync (beta)" not in text

def test_f05_dialogs_no_window_prompt():
    text = DIALOGS.read_text(encoding="utf-8")
    assert "window.prompt" not in text
    assert "window.confirm" not in text
    assert "promptInput" in text
    assert "confirmAction" in text
    assert "bindContextMenuA11y" in text

def test_f05_app_js_context_menu_a11y():
    text = APP_JS.read_text(encoding="utf-8")
    assert "bindContextMenuA11y" in text
    assert "addEventListener('contextmenu'" not in text
    assert "prompt(" not in text
    assert "confirm(" not in text
    assert re.search(r"from '\./dialogs\.js'", text), "app.js must import from dialogs.js"
    if "promptInput(" in text:
        assert re.search(r"import\s*\{[^}]*\bpromptInput\b", text), (
            "app.js uses promptInput but does not import it from dialogs.js"
        )

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

def _function_body_loose(source: str, name: str):
    """Body extractor for functions whose parameter list contains parens
    (e.g. destructured defaults), which the strict regex cannot match."""
    m = re.search(rf"function {re.escape(name)}\b", source)
    assert m, f"missing function {name}"
    i = m.end()
    while i < len(source) and source[i] != "(":
        i += 1
    depth = 0
    while i < len(source):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    brace = source.find("{", i)
    depth = 1
    j = brace + 1
    while j < len(source) and depth:
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
        j += 1
    return source[brace + 1:j - 1]

def _mousedown_handler_body(source: str):
    m = re.search(r"document\.addEventListener\('mousedown', event =>", source)
    assert m, "missing document mousedown listener"
    brace = source.find("{", m.end())
    depth = 1
    i = brace + 1
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[brace + 1:i - 1]


# ── 1.13.1 P0 regression pins (§2.1 of docs/NEXT_UPDATE_PLAN-1.13.1.md) ──

def test_a11y_dialog_hosts_are_not_hidden():
    # Row 1 (probable commit a461edd): lazy utility dialogs were rendered
    # inside a `hidden` wrapper and opened invisibly. Pin: the lazy hosts
    # never hide their wrapper.
    dialogs = DIALOGS.read_text(encoding="utf-8")
    assert "wrap.hidden = true" not in dialogs, "lazy dialog host hides its wrapper"
    assert 'wrap.setAttribute("hidden"' not in dialogs
    assert "wrap.setAttribute('hidden'" not in dialogs

def test_click_outside_closes_only_topmost():
    # Row 2 (a461edd): click-outside used to close *every* open dialog.
    # Pin: the handler selects the topmost dialog (.at(-1)) and there is no
    # forEach close-all loop over open dialogs.
    dialogs = DIALOGS.read_text(encoding="utf-8")
    body = _mousedown_handler_body(dialogs)
    assert ".at(-1)" in body, "click-outside must target the topmost dialog"
    assert not re.search(
        r"querySelectorAll\('dialog\[open\]'\)\.forEach\([^)]*\.close\(\)", body, re.DOTALL
    ), "click-outside must not close-all via forEach"

def test_click_outside_routes_through_closeDialog():
    # G-D2: the click-outside path called topDialog.close() directly,
    # bypassing the focus-restoration contract in closeDialog().
    dialogs = DIALOGS.read_text(encoding="utf-8")
    body = _mousedown_handler_body(dialogs)
    assert "closeDialog(topDialog)" in body, "click-outside must route through closeDialog()"
    assert "topDialog.close()" not in body, "click-outside must not call .close() directly"

def test_lazy_dialogs_carry_focus_wiring():
    # G-D1: lazy hosts (a11y dialogs, trophy case) missed the close-event
    # focus-restoration wiring that static dialogs get at module load.
    dialogs = DIALOGS.read_text(encoding="utf-8")
    assert "function wireDialogFocus(dialog)" in dialogs, "missing shared wireDialogFocus helper"
    assert "document.querySelectorAll('dialog').forEach(wireDialogFocus)" in dialogs
    for fn in ("ensureA11yDialogHosts", "ensureTrophyCaseHost"):
        body = _function_body(dialogs, fn)
        assert body, f"missing {fn}"
        assert "wireDialogFocus" in body, f"{fn} does not apply the shared focus wiring"

def test_checkbox_radio_width_exclusion():
    # Row 3: `input { width:100% }` detached checkboxes/radios from labels.
    # Pin: the exclusion rule exists and sits after the full-width rule.
    css = APP.read_text(encoding="utf-8")
    full_width = re.search(r"input,select,textarea\s*\{[^}]*width:\s*100%", css)
    assert full_width, "missing full-width input rule"
    exclusion = 'input[type="checkbox"],input[type="radio"] { width:auto; }'
    assert exclusion in css, "missing checkbox/radio width exclusion"
    assert css.find(exclusion) > full_width.start(), "exclusion must come after the full-width rule"

def test_confirm_panel_has_no_preview_hash():
    # Row 4h (a461edd): the raw preview id hash was rendered on Confirm.
    # Pin: renderConfirm shows the human-readable revision only.
    setup = (ROOT / "static" / "setup.js").read_text(encoding="utf-8")
    body = _function_body(setup, "renderConfirm")
    assert body, "missing renderConfirm"
    assert "state.previewId" not in body, "renderConfirm still renders the raw preview id"
    assert "Preview revision" in body

def test_setup_dismissed_write_and_gate_sites():
    # Row 5 (b583d5b): the wizard must not re-open after a user-initiated
    # dismissal. Pin the three write sites and the two auto-open gates.
    setup = (ROOT / "static" / "setup.js").read_text(encoding="utf-8")
    dialogs = DIALOGS.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    assert "AppState.setupDismissed = true" in _function_body(setup, "saveAndClose")
    assert "dialog.addEventListener('cancel', () => { AppState.setupDismissed = true; });" in setup
    assert "AppState.setupDismissed = true" in _mousedown_handler_body(dialogs)
    assert "AppState.setupDismissed = true" in app
    gate = "!AppState.appSettings.welcome_completed && !AppState.games.length && !AppState.setupDismissed"
    for name in ("library.js", "settings.js"):
        src = (ROOT / "static" / name).read_text(encoding="utf-8")
        assert gate in src, f"{name} lost the setupDismissed auto-open gate"

def test_setup_wizard_single_open_path():
    # §2.3: every wizard open must route through openSetupCenter() (the
    # single entry point that re-renders first); raw showModal() calls show
    # stale panels.
    for name in ("app.js", "library.js", "settings.js", "bigbox.js"):
        src = (ROOT / "static" / name).read_text(encoding="utf-8")
        assert "$('setupCenter').showModal()" not in src, (
            f"{name} still opens the wizard via raw showModal()"
        )
        assert re.search(
            r"import\s*\{[^}]*\bopenSetupCenter\b[^}]*\}\s*from\s*'\./setup\.js'", src
        ), f"{name} must import openSetupCenter from setup.js"

def test_open_setup_center_rerenders_before_show():
    # §2.3 acceptance: openSetupCenter() always resets the step and
    # re-renders before showing, so opening twice shows fresh state even
    # when the dialog is already open.
    setup = (ROOT / "static" / "setup.js").read_text(encoding="utf-8")
    body = _function_body_loose(setup, "openSetupCenter")
    assert "state.step = step" in body
    assert "renderPanel()" in body
    assert body.find("renderPanel()") < body.find("showModal()"), (
        "openSetupCenter must render before showing"
    )

def test_invalid_field_jumps_to_section():
    # Row 8: saving with a required field on a hidden tab failed silently.
    # Pin: the gameForm `invalid` listener unhides the field's section and
    # toggles the matching nav item.
    app = APP_JS.read_text(encoding="utf-8")
    assert "closest?.('.game-editor-section')" in app
    assert "$('gameForm').addEventListener('invalid'" in app
    assert ".game-editor-nav-item" in app

def test_token_session_storage_lifecycle():
    # Row 9 (2b59eb8): the scrubbed ?token= param is persisted in
    # sessionStorage so reloads stay authenticated.
    app = APP_JS.read_text(encoding="utf-8")
    state = STATE_JS.read_text(encoding="utf-8")
    assert "sessionStorage.setItem('openbox.token', token)" in app
    line = next(
        line for line in state.splitlines() if "sessionStorage.getItem('openbox.token')" in line
    )
    assert "get('token')" in line, "?token= must take precedence over sessionStorage"

def test_hidden_sidebar_sections_round_trip():
    # Row 14: free-text setting became a checkbox group. Pin the
    # collect/restore round-trip through the schema key.
    html = INDEX.read_text(encoding="utf-8")
    settings = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    assert 'data-hide-section' in html
    assert "[data-hide-section]:checked" in settings, "collectSettings must read checked boxes"
    assert "hidden_sidebar_sections" in settings
    assert "hiddenSections.has(input.dataset.hideSection)" in settings, (
        "settings restore must re-check stored sections"
    )

def test_editor_prev_next_hidden_while_adding():
    # Row 15: Prev/Next showed while adding a new game (game is undefined).
    dialogs = DIALOGS.read_text(encoding="utf-8")
    body = _function_body(dialogs, "openGameDialog")
    assert "prevBtn.hidden = !game" in body
    assert "nextBtn.hidden = !game" in body

def test_activity_badge_i18n_structure():
    # Row 16 (dd3ec9b + 12706e8): the badge rendered "0", then the i18n
    # pass destroyed it because applyTranslations sets textContent.
    html = INDEX.read_text(encoding="utf-8")
    button = re.search(r'<[^>]*id="activityButton"[^>]*>', html)
    assert button, "missing #activityButton"
    assert "data-i18n-aria-label" in button.group(0)
    assert "data-i18n=" not in button.group(0).replace("data-i18n-aria-label", ""), (
        "#activityButton must not carry data-i18n (it would wipe child nodes)"
    )
    count = re.search(r'<[^>]*id="activityCount"[^>]*>', html)
    assert count and "hidden" in count.group(0), "#activityCount must start hidden"
    # Structural invariant: no data-i18n element may contain child elements.
    for m in re.finditer(r'<(\w+)[^>]*data-i18n="[^"]*"[^>]*>(.*?)</\1>', html, re.DOTALL):
        inner = m.group(2).strip()
        assert "<" not in inner, (
            f"data-i18n on <{m.group(1)}> wraps child elements — "
            "applyTranslations would destroy them"
        )

def test_media_rare_rows_collapsed_and_labeled():
    # Row 17 (12706e8): 16 rare media rows collapsed behind a toggle that
    # auto-expands when a rare row already holds a value, with a dynamic
    # count label (now i18n-keyed).
    html = INDEX.read_text(encoding="utf-8")
    rows = re.findall(r'class="[^"]*media-rare[^"]*"[^>]*>', html)
    assert len(rows) == 16, f"expected 16 .media-rare rows, found {len(rows)}"
    assert all("hidden" in row for row in rows), "rare media rows must start collapsed"
    dialogs = DIALOGS.read_text(encoding="utf-8")
    body = _function_body(dialogs, "openGameDialog")
    assert "row.hidden = !expandRare" in body, "rare rows must auto-expand when one holds a value"
    assert "t('dialog.media_show_more', {count:" in body, "dynamic label must use the i18n count key"
    assert "t('dialog.media_show_fewer')" in body
    assert 'data-i18n="dialog.media_show_more_initial"' in html, (
        "initial toggle text must be i18n-keyed"
    )

def test_platforms_hidden_specificity():
    # Row 19: `.platforms { display:grid }` overrode the `hidden` attribute.
    # Pin the higher-specificity override and the applySidebarVisibility
    # wiring that hides [data-sidebar-section] elements.
    css = APP.read_text(encoding="utf-8")
    m = re.search(r"\.platforms\[hidden\][^{]*\{([^}]*)\}", css)
    assert m, "missing .platforms[hidden] override"
    assert "display:none" in m.group(1).replace(" ", ""), (
        ".platforms[hidden] must force display:none"
    )
    state = STATE_JS.read_text(encoding="utf-8")
    assert "[data-sidebar-section]" in state, "applySidebarVisibility must touch [data-sidebar-section]"

def test_tab_switch_does_not_guard():
    # Row 20 (6448ae5): tab switches discard nothing, so the discard prompt
    # on nav items was removed — pin the removal while keeping the real
    # close/cancel guards.
    dialogs = DIALOGS.read_text(encoding="utf-8")
    assert "stopImmediatePropagation" not in dialogs, (
        "nav-item discard wiring must stay removed"
    )
    body = _function_body(dialogs, "bindGameEditorUnsavedGuard")
    assert "$('closeDialog').onclick" in body
    assert "$('cancelDialog').onclick" in body
    assert "addEventListener('cancel'" in body

def test_bigbox_empty_view_strings_keyed():
    # §7.3/§8 Option A: the two hardcoded Big Box empty-view strings are
    # now i18n keys.
    bigbox = (ROOT / "static" / "bigbox.js").read_text(encoding="utf-8")
    assert "t('bigbox.empty_view_setup')" in bigbox
    assert "t('bigbox.empty_view_now_empty')" in bigbox
    assert "Big Box needs games in the current view" not in bigbox
    assert "The current view is now empty" not in bigbox

def test_f05_state_native_fallbacks_no_prompt():
    text = STATE_JS.read_text(encoding="utf-8")
    for name in ("nativePickFolder", "nativePickFile", "nativePrompt", "nativeConfirm"):
        body = _function_body(text, name)
        assert body, f"missing function {name}"
        assert "prompt(" not in body, f"{name} still calls prompt()"

def test_sessions_sse_reconnect_closes_previous():
    # Regression: connectSessionEvents() used a per-call const EventSource and
    # leaked the previous stream on every reconnect. The source must live at
    # module level and be closed before a new one is created.
    text = SESSIONS_JS.read_text(encoding="utf-8")
    assert re.search(r"^    let sessionEventSource = null;", text, re.MULTILINE), (
        "sessions.js must keep the EventSource in a module-level variable"
    )
    body = _function_body(text, "connectSessionEvents")
    assert body, "missing function connectSessionEvents"
    close_at = body.find("sessionEventSource.close()")
    new_at = body.find("new EventSource")
    assert close_at != -1, "connectSessionEvents must close the previous EventSource"
    assert new_at != -1, "connectSessionEvents must create an EventSource"
    assert close_at < new_at, "previous EventSource must be closed before creating a new one"
    assert "registerLifecycleStream(source)" in body, (
        "session EventSource must be registered for pagehide teardown"
    )
    assert re.search(r"from '\./state\.js'", text), "sessions.js must import from state.js"
    assert "registerLifecycleStream" in text and "unregisterLifecycleStream" in text

def test_state_pagehide_suppresses_hidden_tab_saves():
    # A hidden tab holds stale AppState: pagehide/visibilitychange must arm a
    # flag that stops state-mutating api() calls from overwriting newer state
    # saved by the visible tab.
    text = STATE_JS.read_text(encoding="utf-8")
    assert "addEventListener('pagehide'" in text, "state.js must listen for pagehide"
    assert "addEventListener('visibilitychange'" in text, "state.js must listen for visibilitychange"
    assert "let pageHidden" in text, "state.js must track the hidden flag"
    assert "closeLifecycleStreams()" in text, "pagehide must close registered SSE streams"
    body = _function_body(text, "api")
    assert body, "missing function api"
    assert "pageHidden" in body, "api() must consult the hidden flag"
    assert "paused while the tab is hidden" in body, "api() must refuse hidden-tab mutations"
    assert "/api/shutdown" in body, "the shutdown POST must stay exempt from suppression"
    assert "isPageHidden" in text and "registerLifecycleStream" in text, (
        "state.js must export the lifecycle helpers"
    )

def test_story_local_day_key_contract():
    # §7.1: mountStoryPanel() grouped events by String(event.at).slice(0,10) —
    # raw string slicing put a 23:30 local (EDT) moment under the following UTC
    # date, apart from its session. Pin: grouping goes through localDayKey(),
    # which parses with full ISO semantics (aware -> convert to local via
    # `new Date`; naive -> parsed as local, the existing convention) and reads
    # LOCAL Y/M/D components — never UTC getters, never raw slicing.
    text = LIBRARY_JS.read_text(encoding="utf-8")
    body = _function_body(text, "localDayKey")
    assert body, "library.js must define localDayKey(isoString)"
    assert "new Date(" in body, "localDayKey must parse with full ISO semantics via new Date()"
    assert "getFullYear()" in body and "getMonth()" in body and "getDate()" in body, (
        "localDayKey must read local calendar components (getFullYear/getMonth/getDate)"
    )
    assert "getUTC" not in body, (
        "localDayKey must not use UTC getters — aware values convert to local, naive values are local"
    )
    assert "slice(0, 10)" not in body, "localDayKey must not slice the raw string"
    assert "'Unknown date'" in body, "localDayKey must keep the 'Unknown date' fallback"
    mount = _function_body(text, "mountStoryPanel")
    assert mount, "missing function mountStoryPanel"
    assert "localDayKey(event.at)" in mount, "mountStoryPanel must group via localDayKey(event.at)"
    assert "slice(0, 10)" not in mount, "mountStoryPanel must not group by raw string slicing anymore"

def test_gamepad_single_loop_invariant():
    # Plan §9 item 3: exactly one gamepad poll loop per surface, all using the
    # same current && !prev edge pattern. Pin the INVARIANT comment in all
    # three surfaces and the single loop owner + edge pattern in each.
    surfaces = {
        "bigbox.js": BIGBOX_JS,
        "navigation.js": NAVIGATION_JS,
        "arcaderoom.js": ARCADEROOM_JS,
    }
    for name, path in surfaces.items():
        text = path.read_text(encoding="utf-8")
        assert "// INVARIANT: exactly one gamepad poll loop per surface" in text, (
            f"{name} must carry the single-loop INVARIANT comment"
        )
        assert re.search(r"const edge = \w+ => current\[\w+\] && !", text), (
            f"{name} must use the current && !prev edge pattern"
        )
    bigbox = BIGBOX_JS.read_text(encoding="utf-8")
    assert bigbox.count("function pollGamepads()") == 1, (
        "bigbox.js must define exactly one pollGamepads loop"
    )
    navigation = NAVIGATION_JS.read_text(encoding="utf-8")
    assert navigation.count("function pollGamepads()") == 1, (
        "navigation.js must define exactly one pollGamepads loop"
    )
    arcade = ARCADEROOM_JS.read_text(encoding="utf-8")
    assert arcade.count("startGamepadPoll() {") == 1, (
        "arcaderoom.js must own exactly one poll loop via startGamepadPoll"
    )

if __name__ == "__main__":
    tests = sorted(
        (name, obj)
        for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj)
    )
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failures += 1
    if failures:
        print(f"SOME FAIL ({failures} of {len(tests)})")
        raise SystemExit(1)
    print(f"ALL PASS ({len(tests)} tests)")
