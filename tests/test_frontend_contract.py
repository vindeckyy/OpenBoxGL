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
        "playlistsButton", "backupButton", "historyButton", "achievementsButton",
        "saveFilterButton", "savePresetButton",
    ],
    "sources": [
        "storefrontButton", "emulatorsButton", "steamButton", "heroicButton",
        "lutrisButton", "arcadeButton", "discoveryButton",
    ],
    "personalize": ["themesButton", "pluginsButton", "settingsButton", "fullscreenButton"],
    "automation": ["webhooksButton", "notificationsButton"],
}

DIALOGS = ROOT / "static" / "dialogs.js"
APP_JS = ROOT / "static" / "app.js"
STATE_JS = ROOT / "static" / "state.js"

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

if __name__ == "__main__":
    try:
        test_app_vars_defined()
        print("PASS test_app_vars_defined")
    except AssertionError as e:
        print(f"FAIL test_app_vars_defined: {e}")
    try:
        test_themes_surface_deep()
        print("PASS test_themes_surface_deep")
    except AssertionError as e:
        print(f"FAIL test_themes_surface_deep: {e}")
    try:
        test_themes_vars_defined()
        print("PASS test_themes_vars_defined")
    except AssertionError as e:
        print(f"FAIL test_themes_vars_defined: {e}")
    try:
        test_tool_menu_group_membership()
        print("PASS test_tool_menu_group_membership")
    except AssertionError as e:
        print(f"FAIL test_tool_menu_group_membership: {e}")
    try:
        test_game_dialog_path_browse_hosts()
        print("PASS test_game_dialog_path_browse_hosts")
    except AssertionError as e:
        print(f"FAIL test_game_dialog_path_browse_hosts: {e}")
    try:
        test_f05_dialogs_no_window_prompt()
        print("PASS test_f05_dialogs_no_window_prompt")
    except AssertionError as e:
        print(f"FAIL test_f05_dialogs_no_window_prompt: {e}")
    try:
        test_f05_app_js_context_menu_a11y()
        print("PASS test_f05_app_js_context_menu_a11y")
    except AssertionError as e:
        print(f"FAIL test_f05_app_js_context_menu_a11y: {e}")
    try:
        test_f05_state_native_fallbacks_no_prompt()
        print("PASS test_f05_state_native_fallbacks_no_prompt")
    except AssertionError as e:
        print(f"FAIL test_f05_state_native_fallbacks_no_prompt: {e}")
    try:
        test_app_vars_defined()
        test_themes_surface_deep()
        test_themes_vars_defined()
        test_tool_menu_group_membership()
        test_game_dialog_path_browse_hosts()
        test_f05_dialogs_no_window_prompt()
        test_f05_app_js_context_menu_a11y()
        test_f05_state_native_fallbacks_no_prompt()
        print("ALL PASS")
    except AssertionError:
        print("SOME FAIL")
        raise SystemExit(1) from None
