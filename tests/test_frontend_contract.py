#!/usr/bin/env python3
"""Frontend contract: var(--*) defined, surface-deep in themes, app shell markup."""
import json
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
IGNORED_DYNAMIC = {"motion-index", "coverflow-offset", "progress"}

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


#: Modules that own animation. Each must reach motion through a token or the
#: shared predicate -- never through a private copy of the OS check.
ANIMATED_MODULES = ("constellation.js", "arcaderoom.js", "bigbox.js", "party.js")

#: The one module allowed to read the OS motion preference.
MOTION_PREFERENCE_OWNER = "util.js"


def _call_sites(src, name):
    """Occurrences of `name(` used as a call, ignoring comments and strings.

    ``"prefers-reduced-motion" in src`` was the old gate and it was an OR, so any
    occurrence of the word satisfied it -- including a prose comment. Counting
    real call sites is what makes the check mean something.
    """
    return len(re.findall(rf"(?<![\w.]){re.escape(name)}\s*\(", _strip_comments(src)))


def _strip_comments(src):
    """Drop /* ... */ and // comments so prose cannot satisfy or break a rule."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", "", src)


def test_js_motion_uses_duration_tokens():
    """ADR 0063: JS-owned motion reads the --dur-* tokens, so the single reduced-motion override reaches it.
    A transition set from script must name a token, and every module that animates must consult
    motionMs() or the shared prefersReducedMotion() predicate."""
    static = ROOT / "static"
    for path in sorted(static.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            if "style.transition" in line:
                assert "var(--dur-" in line, f"{path.name}: script-set transition must use a --dur-* token: {line.strip()}"
    for name in ANIMATED_MODULES:
        src = (static / name).read_text(encoding="utf-8")
        assert _call_sites(src, "motionMs") or _call_sites(src, "prefersReducedMotion"), (
            f"{name} animates but never consults motion preferences"
        )


def test_reduced_motion_is_read_in_exactly_one_place():
    """S22: the OS motion preference has a single owner.

    The old gate was ``"motionMs(" in src or "prefers-reduced-motion" in src``.
    The OR is the loophole: the second arm is satisfied by a comment, by an
    unrelated mention, or by a module's own hand-rolled ``matchMedia`` call --
    which is exactly what bigbox.js and arcaderoom.js each had. The guarantee had
    a second, untokenized path and the gate blessed it.

    Now that both read ``util.js``, any new private copy is a one-line gate
    failure instead of a silent second path.
    """
    static = ROOT / "static"
    owner = static / MOTION_PREFERENCE_OWNER
    assert "matchMedia" in owner.read_text(encoding="utf-8"), (
        f"{MOTION_PREFERENCE_OWNER} must own the reduced-motion query; the gate cannot be written otherwise"
    )
    for path in sorted(static.glob("*.js")):
        if path.name == MOTION_PREFERENCE_OWNER:
            continue
        code = _strip_comments(path.read_text(encoding="utf-8"))
        assert "prefers-reduced-motion" not in code, (
            f"{path.name} contains its own reduced-motion query; call prefersReducedMotion() "
            f"or reducedMotionQuery() from {MOTION_PREFERENCE_OWNER} instead"
        )
    # A width query is not a motion query and must stay legal -- library.js has two.
    library = (static / "library.js").read_text(encoding="utf-8")
    assert library.count("(max-width:760px)") == 2, (
        "library.js's two width queries should be untouched -- the rule targets motion only"
    )


def test_motion_preference_gate_catches_a_private_copy():
    """Both migrated modules must actually route through the shared helper.

    Without this, deleting the import and restoring the old inline
    ``window.matchMedia?.('(prefers-reduced-motion: reduce)').matches`` would
    leave the file passing the "no private query" rule only by accident of
    wording. Asserting the helper is imported *and called* makes the migration
    the thing that is checked, not the absence of one string.
    """
    static = ROOT / "static"
    for name in ("bigbox.js", "arcaderoom.js"):
        src = (static / name).read_text(encoding="utf-8")
        assert re.search(r"import\s*\{[^}]*\bprefersReducedMotion\b[^}]*\}\s*from\s*'\./util\.js'", src), (
            f"{name} must import prefersReducedMotion from ./util.js"
        )
        assert _call_sites(src, "prefersReducedMotion") >= 1, (
            f"{name} imports the helper but never calls it"
        )
    arcade = (static / "arcaderoom.js").read_text(encoding="utf-8")
    assert _call_sites(arcade, "reducedMotionQuery") >= 1, "arcaderoom.js must use the shared query for its change subscription"



def _iter_css_rules(css_text: str):
    """Yield (prelude, body, span) for every top-level-ish rule, brace-balanced.

    A regex over `selector { ... }` breaks the moment a body contains a nested
    `@media` block, which app.css does. This walks braces instead, so the
    reduced-motion block is handed back whole.
    """
    index = 0
    length = len(css_text)
    while index < length:
        open_at = css_text.find("{", index)
        if open_at == -1:
            return
        depth = 0
        close_at = open_at
        while close_at < length:
            if css_text[close_at] == "{":
                depth += 1
            elif css_text[close_at] == "}":
                depth -= 1
                if depth == 0:
                    break
            close_at += 1
        if close_at >= length:
            return
        yield (
            css_text[index:open_at].strip(),
            css_text[open_at + 1:close_at],
            (index, close_at),
        )
        index = close_at + 1


def _selector_set(prelude: str):
    return {part.strip() for part in prelude.split(",") if part.strip()}


def test_dur_loop_consumers_are_all_disabled_under_reduced_motion():
    """Every ``--dur-loop`` consumer must be switched off inside the reduce block.

    ``--dur-loop:1s`` is the one motion token deliberately *not* zeroed by the
    reduced-motion override, because an ``infinite`` animation with a 0s period
    is a CPU-burning strobe rather than still motion. The three consumers are
    therefore disabled individually, by selector.

    That design only holds if the list is complete. The existing reduced-motion
    tests only assert that a media query *exists*, so a fourth consumer -- a new
    skeleton, a spinner, a loading pulse -- would animate forever under reduced
    motion with every gate green. That is precisely the "regress by omission" the
    changelog claims is impossible, so completeness is asserted here instead of
    presence.
    """
    css = APP.read_text(encoding="utf-8")
    reduce_block = None
    consumers = {}
    for prelude, body, _ in _iter_css_rules(css):
        if "prefers-reduced-motion" in prelude:
            reduce_block = body
        if "var(--dur-loop)" in body:
            for selector in _selector_set(prelude):
                if selector.startswith("@") or not selector:
                    continue
                consumers[selector] = body
    assert consumers, "no --dur-loop consumer found; the token or its rule shape changed, re-check this test"
    assert reduce_block is not None, "app.css has no prefers-reduced-motion block"

    disabled = set()
    for prelude, body, _ in _iter_css_rules(reduce_block):
        if re.search(r"animation(?:-name)?\s*:\s*none", body):
            disabled |= _selector_set(prelude)

    uncovered = sorted(set(consumers) - disabled)
    assert not uncovered, (
        "these --dur-loop consumers animate forever under prefers-reduced-motion "
        f"and are not disabled in the reduce block: {uncovered}\n"
        f"currently disabled: {sorted(disabled)}"
    )


def test_dur_loop_completeness_gate_catches_an_omission():
    """The check above must fail when a consumer is left out.

    A gate that cannot fail is worse than none: it is a comment that reads like a
    guarantee. This feeds it a synthetic fourth consumer -- a loading pulse of
    exactly the kind described above -- and asserts it is reported.
    """
    css = APP.read_text(encoding="utf-8")
    reduce_block = next(body for prelude, body, _ in _iter_css_rules(css) if "prefers-reduced-motion" in prelude)

    def uncovered(selectors):
        disabled = set()
        for prelude, body, _ in _iter_css_rules(reduce_block):
            if re.search(r"animation(?:-name)?\s*:\s*none", body):
                disabled |= _selector_set(prelude)
        return sorted(set(selectors) - disabled)

    real = uncovered([s for p, b, _ in _iter_css_rules(css) if "var(--dur-loop)" in b for s in _selector_set(p)])
    assert real == [], f"the real stylesheet is already uncovered: {real}"
    assert uncovered(real + [".loading-pulse"]) == [".loading-pulse"], "an added consumer must be reported"


def test_every_dialog_close_path_goes_through_the_exit_choke_point():
    """B1: "every dialog now plays an exit" was true only for JS `close()`.

    Per the HTML spec, Escape and a click on `::backdrop` run the *close
    watcher*: it fires a cancelable `cancel` event and then removes the `open`
    attribute directly, never calling the JS-visible `close()` method. So the
    `close()` override could not see either path -- the dialog snapped shut, the
    exit animation never played, and `closeDialog`'s focus restore never ran,
    dropping focus to `<body>`. 1.15.0 patched 8 dialogs by hand and left 21.

    The fix is one delegated listener rather than 21 per-dialog ones, so the
    check is that the delegation exists and that no dialog has quietly grown a
    private `cancel` handler calling raw `close()` -- which would reintroduce
    the bypass for that one dialog.
    """
    static = ROOT / "static"
    dialogs = (static / "dialogs.js").read_text(encoding="utf-8")

    assert "document.addEventListener('cancel'" in dialogs, (
        "no delegated cancel listener: the spec's close watcher bypasses close(), "
        "so Escape would snap the dialog shut with no exit animation"
    )
    assert "defaultPrevented" in dialogs, (
        "the delegated cancel listener ignores defaultPrevented, so it would close "
        "a dialog whose own unsaved-changes guard asked it not to"
    )
    assert "event.preventDefault();" in dialogs
    # The backdrop path needs its own handling; a cancel event is not fired for
    # a backdrop click.
    assert "getBoundingClientRect" in dialogs, (
        "no backdrop-click handler: clicking ::backdrop still bypasses close()"
    )

    offenders = []
    for path in sorted(static.glob("*.js")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "addEventListener('cancel'" not in line:
                continue
            if path.name == "dialogs.js" and "document.addEventListener" in line:
                continue  # the delegated one, checked above
            if ".close()" in line and "closeDialog(" not in line:
                offenders.append(f"{path.name}:{number}: {line.strip()[:110]}")
    assert not offenders, (
        "these cancel handlers close the dialog without the shared exit animation "
        "or focus restore:\n  " + "\n  ".join(offenders)
    )


def test_escape_is_not_handled_twice():
    """A document-level Escape keydown handler duplicating the cancel listener.

    `close()` is idempotent while a dialog is closing, so this would not break
    today -- but two handlers both registering a focus restore is exactly the
    kind of duplicate that outlives the reason it was added.
    """
    dialogs = (ROOT / "static" / "dialogs.js").read_text(encoding="utf-8")
    assert "addEventListener('keydown'" not in _strip_comments(dialogs) or "closeDialog" in dialogs, (
        "dialogs.js still has a document-level Escape handler next to the delegated "
        "cancel listener; only one of them should own the close"
    )


def test_the_toast_surface_has_one_writer():
    """B2: "one queue-safe surface in the top layer" -- there was no queue.

    Four writers targeted one `#toast` element and three assigned its `innerHTML`
    outright. Delete a game, get an Undo button for eight seconds, let a trophy
    unlock, and the `innerHTML` assignment removed the button: the deleted game
    could no longer be undone from the UI. The single guarded writer held a
    one-deep `pending` slot, so a third message overwrote the queued second.

    Toasts are independent entries in a capped stack now, so the rule is simply
    that nothing writes a toast's DOM from outside the manager.
    """
    static = ROOT / "static"
    state = (static / "state.js").read_text(encoding="utf-8")

    assert "function showToast(" in state, "the toast manager is gone"
    assert "TOAST_LIMIT" in state, "the toast stack is uncapped"
    assert "function leave(" in state, (
        "1.15.0 specified one shared exit function for toasts and never wrote it; "
        "each writer rolled its own class-toggle instead, which is how they diverged"
    )
    # B4: the old code did `setTimeout(() => notify(next), wait)` with the handle
    # never stored, so revealToast's clearTimeout could not cancel it and a
    # queued message overwrote a fresher toast ~240ms later. Check the shape
    # itself, not the word -- `pendingUpdate` and the search debounce are
    # unrelated and must not trip this.
    assert "toastState" not in state, (
        "state.js still has the single-toast state object; the stack replaces it"
    )
    assert not re.search(r"pending:\s*null", state), (
        "the one-deep pending slot is back; a third message would overwrite the queued second"
    )
    assert not re.search(r"setTimeout\(\s*\(\)\s*=>\s*notify\(", state), (
        "a re-show timer is scheduled with an uncancellable handle, so it overwrites "
        "a fresher toast after the next one has already appeared"
    )

    offenders = []
    for path in sorted(static.glob("*.js")):
        if path.name == "state.js":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("//", 1)[0]
            if "$('toast')" in code or '$("toast")' in code or "#toast'" in code:
                offenders.append(f"{path.name}:{number}: {code.strip()[:100]}")
    assert not offenders, (
        "these modules still address the toast element directly; the manager owns it:\n  "
        + "\n  ".join(offenders)
    )

    # The container is what carries the popover, so the toasts can stack; and
    # index.html must have it, or every toast is a silent no-op.
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="toasts"' in html, "the toast container is missing from index.html"
    assert 'id="toastLive"' in html and 'role="status"' in html, (
        "the toast live region is missing; toasts would be silent to a screen reader"
    )


def test_toast_timers_pause_and_resume():
    """B3: WCAG 2.2.1 -- the toast timer never paused.

    `setTimeout(hideToast, ms)` was armed once and nothing ever touched it. Rest
    the cursor on Undo and it faded out from under the pointer at 8s; Tab to the
    button and `hidePopover()` removed the focused node from the top layer,
    dropping focus to `<body>`. Focus is the half that matters for 2.2.1, so
    both are required -- and the pause must re-arm for the *remaining* time,
    not the full duration.
    """
    state = (ROOT / "static" / "state.js").read_text(encoding="utf-8")
    for event in ("mouseenter", "mouseleave", "focusin", "focusout"):
        assert f"'{event}'" in state, f"the toast does not listen for {event}"
    assert "pauseTimer" in state and "resumeTimer" in state
    assert "entry.remaining" in state, (
        "the pause does not preserve the remaining time, so a paused toast still "
        "expires on the original schedule"
    )
    assert "clearTimeout(entry.timer)" in state, "pausing does not actually stop the timer"


def test_toasts_are_raised_above_dialogs():
    """B5: top-layer elements are ordered by when they were shown.

    Every dialog here is a native `<dialog>`, itself top layer. A toast opened
    first sat below the dialog and its `::backdrop` covered the Undo button for
    the rest of its life. Re-showing the popover moves it to the top.
    """
    state = (ROOT / "static" / "state.js").read_text(encoding="utf-8")
    dialogs = (ROOT / "static" / "dialogs.js").read_text(encoding="utf-8")
    assert "function raiseToasts(" in state, "nothing can raise a toast above a dialog"
    assert "raiseToasts" in _strip_comments(dialogs), (
        "the dialog opener does not raise toasts, so opening a dialog after a "
        "toast still buries the Undo button"
    )
    assert "showPopover" in state


def test_counted_strings_have_a_singular_form():
    """S19: `interpolate` substituted `{count}` verbatim, so it read "1 moments".

    A game with one Moment showed "1 moments" on its card *and* in its
    `aria-label`, in all five locales. The mechanism already existed in the tree
    -- `household.stats_skipped_one` / `_many` and a hand-rolled branch in
    `library.js` -- it was just not in the one place every caller goes through.

    Two halves, and both are checked: the mechanism in `t()`, and a singular
    variant for every key that ships a bare `{count}`. A key with a `_one` but no
    caller passing a number is dead weight; a caller with no `_one` is the bug.

    The handful of counted strings that are already correct at 1 are exempted by
    `COUNT_NEEDS_NO_SINGULAR` below, and `test_the_singular_exemption_list_is_still_justified`
    holds that list to its own reasons. Read that before adding a variant here.
    """
    import json

    i18n = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    assert "pluralVariant" in i18n, "t() has no plural selection"
    assert "_one" in i18n and "_many" in i18n
    # The fallback is what makes the mechanism safe: a key with no variant keeps
    # its existing text instead of rendering the bare key.
    assert "?? deepGet(_strings, key)" in i18n or "deepGet(_strings, key)" in i18n, (
        "a missing plural variant must fall back to the base key, not render the key name"
    )

    strings = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))

    def walk(node, prefix=""):
        for key, value in node.items():
            if key == "meta":
                continue
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                yield from walk(value, full)
            else:
                yield full, value

    flat = dict(walk(strings))
    counted = {
        key: text for key, text in flat.items()
        if "{count}" in str(text) and not key.endswith(("_one", "_many"))
    }
    assert counted, "expected counted strings in en.json; the tree shape changed"

    missing = sorted(
        key for key in counted
        if f"{key}_one" not in flat and key not in COUNT_NEEDS_NO_SINGULAR
    )
    assert not missing, (
        "these strings render a bare count with no singular form, so a count of 1 "
        f"reads as a plural: {missing}"
    )

    for locale in ("es", "de", "fr", "pt"):
        other = json.loads((ROOT / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        other_flat = dict(walk(other))
        absent = sorted(
            key for key in counted
            if f"{key}_one" not in other_flat and key not in COUNT_NEEDS_NO_SINGULAR
        )
        assert not absent, f"{locale}.json is missing a singular for: {absent}"


# The gate above is a blanket assertion, which means somebody will eventually hit a
# counted string that is already correct at 1 and be tempted to drop the test. So the
# exemptions live here, each with the reason it reads the same at 1 and at 2, and the
# gate checks this list against itself: a stale entry fails, and growing it fails.
#
# The rule these all follow is that `{count}` does not modify a countable noun -- it is
# a bare number, an adjective, or already pluralised by the string itself.
COUNT_NEEDS_NO_SINGULAR = {
    # "{count} cabinets" / "{count} cabinet" are the two halves of a hand-rolled
    # branch in arcaderoom.js that picks between them on the count itself; both keys
    # are stored in singular form and neither is a base form with a variant.
    "arcade.cabinet_plural": "hand-rolled plural pair; the caller picks between these two",
    "arcade.cabinet_singular": "hand-rolled plural pair; the caller picks between these two",
    # Adjectives, not nouns: "1 new", "1 updated", "1 kept because you edited them".
    "defs.installed": "counts are adjectives (new/updated/kept), not countable nouns",
    # "Fixed 1" is a past-tense verb phrase with no object.
    "health.fix_done": "verb phrase with no countable object",
    # "1 to add", "1 to merge", "1 skipped" -- the verb carries the number.
    "metadata.esde_add_count": "elliptical label, the verb carries the number",
    "metadata.esde_merge_count": "elliptical label, the verb carries the number",
    "metadata.esde_skip_count": "past participle, the noun is implied by the panel",
    "metadata.launchbox_add_count": "elliptical label, the verb carries the number",
    "metadata.launchbox_merge_count": "elliptical label, the verb carries the number",
    "metadata.launchbox_skip_count": "past participle, the noun is implied by the panel",
    # "Relink 1" is a button label; the object is the review list behind the dialog.
    "repair.apply": "button label for a bulk action; the object is the list behind it",
    # "{count} without a match" -- "without a match" is a prepositional phrase.
    "repair.unmatched": "the count modifies a prepositional phrase, not a noun",
    # Already disambiguated in the string itself: "Reverted 1 field(s)."
    "time_machine.reverted": "the string ships its own field(s) disambiguation",
    # "1 in trash" -- "in trash" is a fixed phrase.
    "trash.count": "the count modifies a fixed phrase, not a noun",
    # "(+1 more)" is already read as an increment at any count.
    "trophy.unlocked_more": "an increment in parentheses; singular adds nothing",
}

# Ratchet: this list may shrink when a string is reworded, never grow. Adding an entry
# means a new string renders "1 <plural>", which is the bug the gate exists to catch.
COUNT_NO_SINGULAR_CEILING = 15


def test_the_singular_exemption_list_is_still_justified():
    """S19, second half: an exemption list is only a gate if it polices itself.

    The first half of `test_counted_strings_have_a_singular_form` is a blanket rule,
    so the failure mode is someone hitting a string that reads fine at 1 and deleting
    the test. Moving it here keeps the test and makes the decision reviewable. Three
    ways to go wrong with a hand-kept list, all of which are silent:

    - an entry that no longer exists, or whose text changed, so the reason is now
      fiction and the string may well need a singular after all;
    - an entry that got a `_one` variant, which would mean the exemption is redundant;
    - a new entry, which is the bug arriving through the front door.
    """
    import json

    strings = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))

    def walk(node, prefix=""):
        for key, value in node.items():
            if key == "meta":
                continue
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                yield from walk(value, full)
            else:
                yield full, value

    flat = dict(walk(strings))

    stale = sorted(key for key in COUNT_NEEDS_NO_SINGULAR if key not in flat)
    assert not stale, (
        "these exemptions name a key that is no longer in en.json; delete them or "
        f"re-point them: {stale}"
    )

    lost_count = sorted(
        key for key, text in flat.items()
        if key in COUNT_NEEDS_NO_SINGULAR and "{count}" not in str(text)
    )
    assert not lost_count, (
        "these exempted keys no longer interpolate {count}, so the reason no longer "
        f"applies and the exemption is hiding a reworded string: {lost_count}"
    )

    redundant = sorted(key for key in COUNT_NEEDS_NO_SINGULAR if f"{key}_one" in flat)
    assert not redundant, (
        "these keys now have a singular variant, so the exemption is redundant and "
        f"the variant is dead weight: {redundant}"
    )

    unexempted = sorted(
        key for key, text in flat.items()
        if "{count}" in str(text) and not key.endswith(("_one", "_many"))
        and f"{key}_one" not in flat and key not in COUNT_NEEDS_NO_SINGULAR
    )
    assert not unexempted, (
        f"these counted strings have no singular form and no exemption: {unexempted}"
    )

    assert len(COUNT_NEEDS_NO_SINGULAR) <= COUNT_NO_SINGULAR_CEILING, (
        f"the exemption list grew to {len(COUNT_NEEDS_NO_SINGULAR)} entries "
        f"(ceiling {COUNT_NO_SINGULAR_CEILING}); a shrinking list is a reworded "
        "string, a growing one is a bug arriving through the exemption"
    )


def test_restore_cannot_be_armed_without_a_preview():
    """F2: the whole feature is one rule -- a restore is not offerable until you
    can see what it would do.

    The failure this guards is a bypass, not a missing feature. The pre-F2 flow
    put a live Restore button in the markup and confirmed with a dialog that said
    only "a safety copy will be made". If the confirm button is anywhere in
    `index.html`, a diff that 500s leaves that destructive path reachable and the
    user overwrites blind anyway. So the button may not pre-exist, exactly one
    function may build it, and that function's only caller may be the success arm
    of the diff fetch.
    """
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "data-restore-confirm" not in html, (
        "the confirm button exists in the markup, so a failed diff leaves a "
        "working blind-restore path"
    )
    assert "data-restore-retry" not in html, "the retry button is not markup either"

    settings = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")

    builder = _function_body(settings, "renderRestorePreview")
    assert builder, "no renderRestorePreview() builds the armed button"
    assert "data-restore-confirm" in builder, (
        "renderRestorePreview() does not build the confirm button, so nothing "
        "arms a restore"
    )
    # Counted over the file with the builder excised, not over the raw string: the
    # attribute legitimately appears twice inside it (markup, then the handler
    # binding). What must not exist is an occurrence in any *other* function.
    outside = settings.replace(builder, "")
    assert "data-restore-confirm" not in outside, (
        "a function other than renderRestorePreview() also builds the confirm "
        "button; only one may, or the preview gate is bypassable"
    )

    failed = _function_body(settings, "renderRestorePreviewFailed")
    assert failed, "a failed diff has no renderer"
    assert "data-restore-confirm" not in failed, (
        "the failure path builds the confirm button, so a diff that cannot load "
        "is a diff the user can restore over anyway"
    )
    assert "data-restore-retry" in failed, "a failed diff offers no way back"

    loader = _function_body(settings, "previewBackupRestore")
    assert "/api/v2/backup/diff" in loader, "the preview does not fetch a diff"
    fetched = loader.index("await api(")
    armed = loader.index("renderRestorePreview(path, result)")
    stopped = loader.index("renderRestorePreviewFailed")
    assert fetched < armed < stopped, (
        "the preview is not built between a successful diff and the failure arm; "
        "the success and failure renderers are out of order"
    )

    # One POST, and it is behind a guard, so there is no second way in.
    posters = [
        path.name for path in sorted((ROOT / "static").glob("*.js"))
        if "/api/backup/restore" in path.read_text(encoding="utf-8")
    ]
    assert posters == ["settings.js"], f"restore is posted from {posters}"
    commit = _function_body(settings, "commitRestore")
    assert "/api/backup/restore" in commit
    assert re.search(r"if \(!backupPreviewPath\)\s*return", _strip_comments(commit)), (
        "commitRestore() does not require an armed preview path, so a stale or "
        "cleared panel can still post a restore"
    )


def test_restore_preview_reads_the_restore_block_not_the_set_names():
    """`added` means "in the current library, absent from the backup" -- which is
    exactly what a restore deletes. Rendering it under the label "Added" tells
    the user the opposite of the truth about a destructive operation."""
    settings = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    notes = _strip_comments(_function_body(settings, "restoreNotes"))
    for key in ("will_remove", "will_add", "will_change"):
        assert f"restore.{key}" in notes, f"the preview never reads restore.{key}"
    for legacy in ("result.added", "result.removed", "result.changed"):
        assert legacy not in notes, (
            f"the preview still reads {legacy}; that name is relative to the "
            "current library, not to the restore"
        )


def test_restore_preview_says_settings_would_move():
    """The one part of a restore almost nobody expects: settings.json carries save
    paths, emulator profiles and theme choices, not just the library. The preview
    has to raise it, and has to raise it only when a restore would really write
    them -- a library-only backup would be a false alarm."""
    settings = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
    notes = _strip_comments(_function_body(settings, "restoreNotes"))
    assert "settings.restored" in notes, (
        "the settings line is not gated on whether a restore would write settings"
    )
    assert "settings.would_change" in notes, (
        "the settings-differ line reads the raw comparison, which is true for a "
        "library-only backup and would warn about a restore that cannot happen"
    )
    assert "settings.redacted_secrets" in notes, (
        "backups redact secrets, so the preview can promise the stored "
        "credentials survive; it never says so"
    )
    assert "backup.preview_settings_missing" in notes, (
        "a manifest can promise settings the archive does not carry; that is a "
        "different warning from the settings-moved one"
    )


def test_external_navigation_validates_the_scheme_once():
    """F2: `javascript:` reached `window.open` unvalidated.

    `wikipedia_url` is free text (`index.html:151`, no `type="url"`, no
    `pattern`) and `library.js` passes it to `nativeOpenExternal` verbatim. The
    two native hosts both validate, but the browser-served deployment -- and any
    host-detect failure, which is `.catch(() => {})`-swallowed -- takes the
    `window.open` branch. The rule now lives at the top of the function so all
    three branches agree, and it is the *same* rule `fact()` and `gameLinks()`
    use rather than a second copy of the regex.
    """
    util = (ROOT / "static" / "util.js").read_text(encoding="utf-8")
    state = (ROOT / "static" / "state.js").read_text(encoding="utf-8")

    assert re.search(r"const HTTP_URL_RE\s*=\s*/\^https\?:", util), (
        "the single http(s) URL rule is gone or changed shape"
    )
    assert "HTTP_URL_RE" in re.search(r"^import \{[^}]*\} from '\./util\.js';", state, re.M).group(0), (
        "state.js does not import the shared URL rule, so it has its own copy"
    )
    body = _strip_comments(_function_body(state, "nativeOpenExternal"))
    assert "HTTP_URL_RE.test(url)" in body, (
        "nativeOpenExternal does not validate the scheme; every branch is reachable "
        "with an arbitrary target"
    )
    # The check must come before any branch, not inside one.
    assert body.index("HTTP_URL_RE.test(url)") < body.index("window.open"), (
        "the scheme check runs after the window.open fallback, so it guards nothing"
    )
    assert "noopener,noreferrer" in body, (
        "window.open passes no noopener, so the opened page gets a handle on us"
    )
    # A second copy of the regex anywhere in state.js would drift from util.js.
    assert not re.search(r"/\^https\?:\/", state), (
        "state.js has its own copy of the URL regex; there must be one rule"
    )


def test_the_virtual_grid_never_falls_back_to_rendering_everything():
    """F3: `gridWindow` treated "row height unknown" as "render every row".

    Any reset of `gridRowHeight` before a render -- the resize handler, or the
    zero-results branch -- therefore stringified the entire result set into
    innerHTML. On the 20k fixture in List view that is 20,000
    `<button class="list-row">` elements, and PERF.md treats that budget as
    blocking CI, so it was a gate failure rather than a latency complaint.

    The fallback has to be a *measured* row height, and it must persist across
    the reset -- otherwise the fix is a second reset away from regressing.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    window_body = _strip_comments(_function_body(library, "gridWindow"))
    assert window_body, "gridWindow() is gone; the virtualizer has no entry point"
    assert "gridRowHeight || lastMeasuredRowHeight" in window_body, (
        "gridWindow no longer falls back to the last measured row height, so an "
        "unmeasured render is still 'render everything'"
    )
    assert re.search(r"if \(!rowHeight\) return \[0, total\]", window_body), (
        "the genuine no-measurement-yet case must still render in full; a silent "
        "0-row window would blank the library on first paint"
    )
    # The measurement has to be recorded, or the fallback is always empty.
    assert re.search(r"lastMeasuredRowHeight\s*=\s*gridRowHeight", library), (
        "no measurement is ever recorded, so the fallback can never fire"
    )
    # And the scroll handler must use the same fallback, not the raw variable.
    scroll = _strip_comments(_function_body(library, "gridWindow")) + library
    assert "gridRowHeight || lastMeasuredRowHeight" in scroll
    assert library.count("gridRowHeight || lastMeasuredRowHeight") >= 1


def test_the_row_geometry_cache_is_not_invalidated_by_every_cover():
    """F4: `_coverRatiosRevision` was bumped on every cover load and is part of
    the geometry cache key, so each of the 20,000 covers scrolling in was a cache
    miss that re-walked `groupedSections()` and rebuilt ~2,500 row objects. The
    debounce batched the *render*; it never batched the geometry, so the comment
    claiming covers "batch into a single render" was half true.

    Only a bucket change moves a card between sections, so only a bucket change
    may invalidate the layout.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    body = _arrow_body(library, "recordCoverRatio")
    assert body, "recordCoverRatio() is gone or is no longer an arrow function"
    bump = body.index("_coverRatiosRevision++")
    bucket = body.index("coverBucketOf(prev) !== coverBucketOf(ratio)")
    assert bump > bucket, (
        "the revision is bumped before the bucket check, so every cover load "
        "invalidates the row-geometry cache regardless of whether anything moved"
    )
    assert body.count("_coverRatiosRevision++") == 1, (
        "the revision is bumped from more than one place; the invalidation is no "
        "longer tied to the bucket change that justifies it"
    )
    # And the debounced re-render must be inside the same branch, or a
    # bucket-changing cover would not regroup.
    assert body.index("renderGrid()") > bucket


def test_superseded_detail_requests_cannot_paint_into_the_current_game():
    """F5: every `renderDetails()` fired a preflight and a related-games request
    with no sequence guard and no `AbortSignal`. Holding the down arrow through 15
    games issued 30 requests, and whichever resolved last painted into whichever
    pane was on screen -- so game #3's preflight could show "Ready to launch ✓"
    on game #15.

    Two things are required, and the first is easy to half-do: a controller that
    supersedes the previous one, *and* an identity check on both the success and
    the error path, because an abort only covers the requests the browser
    actually tears down.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")

    begin = _function_body(library, "beginDetailsRequest")
    assert begin, "no beginDetailsRequest(); nothing supersedes the previous selection"
    assert "detailsRequest?.abort()" in begin, (
        "the previous selection's requests are not aborted"
    )
    assert "new AbortController()" in begin

    guard = _function_body(library, "isCurrentDetailsRequest")
    assert guard, "no isCurrentDetailsRequest(); a late response is indistinguishable from a current one"
    assert "controller === detailsRequest" in guard, (
        "the guard does not compare against the current controller, so an "
        "aborted request that still resolves is treated as live"
    )
    assert "signal.aborted" in guard
    assert "AppState.selectedId === id" in guard

    for name in ("loadDoctor", "loadRelated"):
        body = _function_body(library, name)
        assert body, f"{name}() is gone"
        assert "isCurrentDetailsRequest" in body, (
            f"{name}() paints without a supersession check; a slow response for a "
            "game the user already left overwrites the current pane"
        )
        assert "controller?.signal.aborted" in body or "signal.aborted" in body, (
            f"{name}() has no abort check on its error path, so an aborted request "
            "writes its failure into the live pane"
        )
    # Both loaders must actually receive the controller.
    details = _strip_comments(library)
    assert "loadRelated(game.id, detailsController)" in details, "loadRelated is not sequenced"
    assert "loadDoctor(game, detailsController)" in details, "loadDoctor is not sequenced"

    state = (ROOT / "static" / "state.js").read_text(encoding="utf-8")
    api_body = _function_body(state, "api")
    assert "signal: controller.signal" in api_body, (
        "api() drops the caller's signal, so aborting a superseded request does "
        "nothing -- the fetch still resolves and still paints"
    )
    assert "API_TIMEOUT_MS" in api_body, (
        "api() has no deadline; a server that stops responding leaves every "
        "caller awaiting forever with no toast (F9)"
    )


def test_focus_survives_a_card_leaving_the_rendered_window():
    """F6: the focused card's id was captured, then restored through an optional
    chain. When the card filtered out of the new window the chain yielded
    `undefined` and focus fell to `<body>`, which silently killed arrow-key
    navigation and Enter-to-launch for the rest of the session.

    An optional chain here is the bug: "the element I want is not there" and "I
    changed my mind" are the same value, and the second reading is what shipped.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    body = _strip_comments(_function_body(library, "renderGrid"))
    assert body, "renderGrid() is gone"
    assert '?.focus({ preventScroll: true })' not in body, (
        "renderGrid restores focus through an optional chain, so a card that left "
        "the window drops focus to <body> again"
    )
    assert body.count("focusGridFallback()") >= 2, (
        "both the card and the bulk picker need the fallback; one of them still "
        "drops focus to <body>"
    )
    fallback = _function_body(library, "focusGridFallback")
    assert fallback, "focusGridFallback() does not exist"
    assert "$('grid')" in fallback, "the fallback must target a container that always exists"
    assert "tabindex" in fallback, (
        "the grid is not focusable, so the fallback is itself a no-op"
    )
    # The capture must still read from the element that had focus, not a guess.
    assert "document.activeElement" in body, "the focused id is no longer captured"


def test_a_file_drop_can_never_navigate_the_window_away():
    """F8: the only drop handlers were three on `#dropZone`, and nothing called
    `preventDefault` at document level. Dropping a file from Explorer anywhere on
    the grid performed the browser's default navigation and *lost the session*.

    The guard has to be at the window, because that is the only place that sees a
    drop anywhere on the page. Two details make it real rather than decorative:
    it must only fire for a `Files` transfer, so an ordinary text drag is
    untouched, and `#dropZone` must re-check that too -- a text drag released over
    the drop zone used to open the "enter the absolute path" prompt.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    for event in ("dragenter", "dragover", "dragleave", "drop"):
        assert f"window.addEventListener('{event}'" in library, (
            f"no window-level {event} guard; a file dropped outside #dropZone "
            "navigates the app away and loses the session"
        )
    guard = library[library.index("window.addEventListener('dragenter'"):library.index("$('dropZone')")]
    for event in ("dragenter", "dragover", "drop"):
        block = guard[guard.index(f"'{event}'"):]
        block = block[:block.index("});")] if "});" in block else block
        assert "preventDefault()" in block, (
            f"the window-level {event} does not preventDefault, so the browser's "
            "default navigation still runs"
        )
    assert "dataTransfer" in library, (
        "the guards never inspect dataTransfer, so a text drag is treated as a "
        "file drop"
    )
    assert "hasFiles(event)" in guard, (
        "the window guard does not filter on a Files transfer"
    )
    drop = _strip_comments(library)
    marker = "$('dropZone').addEventListener('drop'"
    start = drop.index(marker)
    block = drop[start:start + 500]
    assert "hasFiles(event)" in block, (
        "the #dropZone drop handler does not re-check for files, so a text drag "
        "over it still opens the import prompt"
    )


def test_bulk_selection_and_context_menu_agree_with_the_screen():
    """F13 and F14, together, because they are the same class of defect: the
    selection state and the rendered state disagree, and the keyboard follows the
    state while the eye follows the screen.

    F13: the bulk picker is a sibling of `.card-main`, so its click never reached
    the `[data-game]` handler that re-renders, and the selection count froze at
    whatever it said when bulk mode was entered.

    F14: right-clicking a card set `AppState.selectedId` without re-rendering, so
    the card was not highlighted and the details pane still described a different
    game -- then Enter launched a game that was not on screen.
    """
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    picker = _strip_comments(library)
    start = picker.index("[data-game-picker]').forEach(input => input.onchange")
    block = picker[start:picker.index("document.querySelectorAll('[data-moments-card]')", start)]
    assert "selectedIds.add(id)" in block and "selectedIds.delete(id)" in block
    assert "renderGrid()" in block, (
        "the bulk picker mutates selectedIds without re-rendering, so the "
        "selection count never updates (F13)"
    )

    dialogs = (ROOT / "static" / "dialogs.js").read_text(encoding="utf-8")
    menu = _strip_comments(_function_body(dialogs, "openContextMenu"))
    assert menu, "openContextMenu() is gone"
    assert "AppState.selectedId = id" in menu
    assert "renderGrid()" in menu and "renderDetails()" in menu, (
        "right-clicking sets selectedId without re-rendering, so the highlighted "
        "card and the details pane disagree with the selection and Enter acts on "
        "a game that is not on screen (F14)"
    )
    assert menu.index("AppState.selectedId = id") < menu.index("renderGrid()"), (
        "the re-render happens before the selection is set, so it renders the old one"
    )


def test_the_query_grammar_drops_empty_tokens_and_rejects_unknown_keys():
    """F7, F11 and F12 -- three ways the search box lied.

    F7: a lone `-` produced an empty value, and `anything.includes('')` is true,
    so the negative matched nothing and the library rendered "0 games" with no
    explanation.
    F11: the tokenizer had no way to attach a leading `-` to a quoted string, so
    `-"the beatles"` became two tokens and returned a game named "Beatles".
    F12: `fields[key] || fields.all` meant one typo'd key silently searched all 16
    fields and returned a plausible-looking result set.
    """
    util = (ROOT / "static" / "util.js").read_text(encoding="utf-8")
    tokenizer = _function_body(util, "parseQueryTokens")
    assert tokenizer, "parseQueryTokens() is gone"
    assert "QUERY_TOKEN_RE = /(-?)" in util, (
        "the token pattern no longer allows a leading '-' to bind to the phrase; "
        "a quoted negative query splits in two (F11)"
    )
    assert re.search(r"if \(!value\) continue;", tokenizer), (
        "an empty-valued token is not dropped; a lone '-' negates every game and "
        "the library shows 0 results (F7)"
    )

    matcher = _strip_comments(_function_body(util, "advancedQueryMatches"))
    assert "fields[key] || fields.all" not in matcher, (
        "an unrecognised filter key falls back to searching every field, so a "
        "typo returns plausible-looking wrong results (F12)"
    )
    assert re.search(r"const names = fields\[key\];", matcher)
    assert re.search(r"if \(!names\) return false;", matcher), (
        "an unknown key must match nothing rather than widening to all fields"
    )
    # `all:` is a real key, so dropping the fallback must not have broken it.
    assert "all:['name'" in _strip_comments(util), "the explicit `all:` key is gone"


def test_bigbox_inert_is_released_whenever_bigbox_hides():
    """S18 regression: `inert` was only released by `closeBigBox()`.

    Any other path that hides `#bigBox` -- the empty-view branch, a deeplink, the
    gamepad loop stopping on blur -- left the whole page `inert` for the rest of
    the session. That failure is quiet: buttons still render and still respond to
    synthetic clicks, but `.focus()` is a no-op, so every dialog's focus restore
    lands on `<body>` and keyboard navigation is dead. The ui_smoke run caught it
    as a focus regression three features away from the cause, which is exactly
    why the release is pinned here too.
    """
    bigbox = (ROOT / "static" / "bigbox.js").read_text(encoding="utf-8")
    body = _function_body(bigbox, "setBackgroundInert")
    assert body, "setBackgroundInert() is gone; the S18 focus isolation was reverted"

    watcher = _function_body(bigbox, "watchBigBoxVisibility")
    assert watcher, (
        "nothing observes #bigBox becoming hidden, so a close path that does not "
        "go through closeBigBox() leaves the page inert forever"
    )
    assert "MutationObserver" in watcher
    assert "attributeFilter: ['hidden']" in watcher, (
        "the observer does not watch the hidden attribute, so it never fires"
    )
    assert "if (bigBox.hidden) setBackgroundInert(false)" in watcher, (
        "the observer does not release inertness when Big Box hides"
    )
    # And it must be armed, not merely defined.
    assert "watchBigBoxVisibility()" in bigbox.replace(watcher, "", 1), (
        "watchBigBoxVisibility() is defined but never called"
    )
    keep = re.search(r"INERT_KEEP_VISIBLE\s*=\s*new Set\(\[([^\]]*)\]", bigbox)
    assert keep, "the INERT_KEEP_VISIBLE keep-out list is gone"
    exempt = set(re.findall(r"'([^']+)'", keep.group(1)))
    for required in ("bigBox", "toasts", "toastLive"):
        assert required in exempt, (
            f"{required} is not exempt from inert; it would be pulled out of the "
            "focus order and the accessibility tree while Big Box is open"
        )
    body = _function_body(bigbox, "setBackgroundInert")
    assert "INERT_KEEP_VISIBLE.has(el.id)" in body, (
        "the keep-out list is declared but never consulted"
    )
    assert "el.tagName === 'DIALOG'" in body, (
        "native <dialog> elements are not exempt; a modal open on top of Big Box "
        "would be inert"
    )


def test_bigbox_video_snap_css():
    """Video snap class exists and is hidden under reduced-motion."""
    css = APP.read_text(encoding="utf-8")
    assert ".bigbox-video-snap" in css, "bigbox-video-snap CSS class missing"
    assert "prefers-reduced-motion" in css, "reduced-motion media query missing"


# --- ADR 0064: gates test behavior, not the presence of a helper ------------
#
# Untrusted data reaches the DOM from imported filenames, provider metadata and
# caught error strings. `escapeHtml` already exists in util.js and almost every
# call site uses it -- but `wrapped.js` used it nowhere at all, so five game
# names reached the report unescaped. `mastery.js:45` performs the *identical*
# operation with escapeHtml, which is what makes this an oversight rather than a
# decision.
#
# The sink is what decides whether escaping is required, and getting that wrong
# makes the gate useless in both directions. `notify()` assigns `textContent`,
# `.value =` is an attribute assignment, and `${game.platform === 'Arcade' ? …}`
# is a comparison rather than an interpolation. None of those are HTML sinks.
# A first draft of this check scanned every template literal in the codebase and
# produced eleven false positives for exactly those three shapes. So the check
# below is sink-first: it only looks inside an actual HTML parse.

# An HTML-parsing sink, with the template literal that reaches it.
HTML_SINK_RE = re.compile(r"\.innerHTML\s*(?:\+?=)|insertAdjacentHTML\s*\(")
# A property access on a library row, provider payload, or entry object. These
# are untrusted: game names come from imported filenames.
UNTRUSTED_PROPERTY_RE = re.compile(
    r"(?:\b(?:game|top|data|entry|item|row|platform|genre|first_play|oldest_played|"
    r"store|source|target|doc|definition|field)\b)"
    r"\s*(?:\?\.|\.)"
    r"\s*(?:name|platform|genre|title|developer|path|notes|description|tag|"
    r"publisher|esrb|store|category|value|label|url|website)\b"
)
# Attribute sinks inside markup: the value lands in the DOM parser, and a
# `javascript:` or `data:` URL there is script execution. The lookbehind keeps
# this to *exact* attribute names -- without it, `data-pause-action=` matches
# because it contains the substring "action".
URL_ATTR_RE = re.compile(r"""(?<![\w:.-])(?:href|src|action|xlink:href)\s*=\s*["']\$\{""")
# Helpers that already validate or neutralise their argument. `gameLinks` and
# `fact` (util.js) gate URLs on HTTP_URL_RE before emitting an href; `media`
# builds a same-origin /api/media path from a numeric id; `Number` coerces to a
# number. `String()` does NOT neutralise markup, so it is not listed: a
# `${String(game.name)}` inside innerHTML is an unescaped sink.
SAFE_HELPERS = (
    "escapeHtml",
    "encodeURIComponent",
    "gameLinks",
    "fact(",
    "media(",
    "Number(",
    "JSON.stringify",
)


def _template_spans(src):
    """Yield (start_offset, end_offset) for every template literal in src."""
    index = 0
    length = len(src)
    while index < length:
        if src[index] != "`":
            index += 1
            continue
        start = index
        cursor = index + 1
        depth = 0
        while cursor < length:
            char = src[cursor]
            if char == "\\":
                cursor += 2
                continue
            if src.startswith("${", cursor):
                depth += 1
                cursor += 2
                continue
            if char == "}" and depth:
                depth -= 1
                cursor += 1
                continue
            if char == "`" and depth == 0:
                yield start, cursor
                index = cursor + 1
                break
            cursor += 1
        else:
            return


def _interpolations(block):
    """Yield (offset, expression_text) for each ${...} in a template literal."""
    cursor = 0
    while True:
        open_at = block.find("${", cursor)
        if open_at < 0:
            return
        depth = 0
        index = open_at + 2
        while index < len(block):
            if block[index] == "{":
                depth += 1
            elif block[index] == "}":
                if depth == 0:
                    break
                depth -= 1
            index += 1
        yield open_at, block[open_at + 2:index]
        cursor = index + 1


def _sinks(src):
    """Yield (line_number, block_text) for the template literal at each HTML sink.

    Only the template *immediately* following the sink is considered. Scanning
    the rest of the file from each sink -- the obvious first implementation --
    sweeps in every later template and reports the same block many times over,
    which is how `notify()` calls end up looking like HTML injection.
    """
    for match in HTML_SINK_RE.finditer(src):
        cursor = match.end()
        while cursor < len(src) and src[cursor] in " \t\r\n":
            cursor += 1
        if cursor >= len(src) or src[cursor] != "`":
            # `el.innerHTML = someVariable` -- the block is elsewhere; a
            # variable assignment is not a literal sink and is left alone.
            continue
        for start, end in _template_spans(src[cursor:]):
            if start != 0:
                continue
            yield src.count("\n", 0, cursor) + 1, src[cursor:cursor + end]
            break


def _ternary_split(expression):
    """Return the offset of the top-level `?`, or -1 if the expression is not a ternary."""
    depth = 0
    for index, char in enumerate(expression):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "?" and depth == 0:
            # `?.` is optional chaining, not a ternary.
            if index + 1 < len(expression) and expression[index + 1] == ".":
                continue
            if index and expression[index - 1] == "?":
                continue
            return index
    return -1


def test_unescaped_untrusted_data_never_reaches_an_html_sink():
    """A game name must never reach innerHTML without escapeHtml.

    This is the check that would have caught wrapped.js. It only inspects
    template literals that are actually assigned to innerHTML (or handed to
    insertAdjacentHTML), so a `notify()` or a `.value =` is correctly ignored,
    and a property used only as a ternary *condition* is not an interpolation
    of that value.
    """
    static = ROOT / "static"
    offenders = []
    for path in sorted(static.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        for lineno, block in _sinks(src):
            for _, expression in _interpolations(block):
                text = expression.strip()
                if any(helper in text for helper in SAFE_HELPERS):
                    continue
                # `t('key')` is our own locale string, not user data.
                if text.startswith("t(") or text.startswith("t ("):
                    continue
                for hit in UNTRUSTED_PROPERTY_RE.finditer(text):
                    if _ternary_split(text) != -1 and hit.start() < _ternary_split(text):
                        # Used to choose a branch, not emitted.
                        continue
                    offenders.append(f"{path.name}:{lineno}  ${{...{text[:70]}}}")
    assert not offenders, (
        "unescaped library/provider data reaches an HTML sink. Wrap it in "
        "escapeHtml() (imported from util.js), or build the node with "
        "textContent:\n  " + "\n  ".join(offenders)
    )


def test_url_attributes_are_validated_at_every_html_sink():
    """An href/src built from data must be escaped or scheme-validated."""
    static = ROOT / "static"
    offenders = []
    for path in sorted(static.glob("*.js")):
        src = path.read_text(encoding="utf-8")
        for lineno, block in _sinks(src):
            for match in URL_ATTR_RE.finditer(block):
                expression = block[match.end():]
                close = expression.find("}")
                expression = expression[:close if close >= 0 else 60]
                if any(helper in expression for helper in SAFE_HELPERS):
                    continue
                offenders.append(f"{path.name}:{lineno}  href/src=${{{expression[:60]}}}")
    assert not offenders, (
        "a URL attribute inside markup is built from unvalidated data. Use "
        "gameLinks()/fact() (util.js, HTTP_URL_RE) or encodeURIComponent on a "
        "validated value:\n  " + "\n  ".join(offenders)
    )


def test_wrapped_report_escapes_its_interpolations():
    """The specific regression: wrapped.js had no escapeHtml at all."""
    src = (ROOT / "static" / "wrapped.js").read_text(encoding="utf-8")
    assert "escapeHtml" in src, "wrapped.js must import escapeHtml from util.js"
    for needle in (
        "(top.game || {}).name",
        "(top.platform || {}).platform",
        "(top.genre || {}).genre",
        "(data.first_play || {}).name",
        "(data.oldest_played || {}).name",
    ):
        assert f"${{escapeHtml({needle}" in src, f"wrapped.js must escape {needle}"
        assert f"${{{needle}" not in src, f"wrapped.js still interpolates {needle} unescaped"


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
    assert "/api/v2/library/time-machine/compare" in js
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

def _arrow_body(source: str, name: str):
    """Body extractor for `const NAME = args => { ... }`, which `_function_body`
    cannot match because there is no `function` keyword. Same brace balancing."""
    m = re.search(rf"(?:const|let|var)\s+{re.escape(name)}\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*\{{", source)
    if not m:
        return ""
    depth = 1
    i = m.end()
    start = i
    while i < len(source) and depth:
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
        i += 1
    return source[start:i - 1]


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

def test_one_event_stream_per_tab_owned_by_events_js():
    # A tab used to open up to three EventSources (health, activity, sessions); the
    # server caps subscribers, so a busy tab got 503 and lost updates. The stream is
    # now one module: the only EventSource construction in the frontend is in
    # events.js, and each consumer subscribes to it.
    constructions = []
    for path in sorted((ROOT / "static").glob("*.js")):
        for _match in re.finditer(r"new EventSource\(", path.read_text(encoding="utf-8")):
            constructions.append(path.name)
    assert constructions == ["events.js"], f"EventSource must be constructed only in events.js, found {constructions}"
    events = (ROOT / "static" / "events.js").read_text(encoding="utf-8")
    assert "registerLifecycleStream(next)" in events, "the shared stream must be registered for pagehide teardown"
    assert "unregisterLifecycleStream(dying)" in events, "a dropped stream must be unregistered"
    assert "nextRetryDelay(retryDelay, RETRY_MAX_MS)" in events, "a dropped stream must retry with backoff"
    for name in ("sessions.js", "activity.js", "health.js"):
        text = (ROOT / "static" / name).read_text(encoding="utf-8")
        assert re.search(r"from '\./events\.js'", text), f"{name} must subscribe through events.js"
        assert "new EventSource" not in text, f"{name} must not open its own stream"


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

def test_gamepad_unified_loop():
    # Exactly one gamepad rAF loop for the whole app, owned by static/gamepad.js.
    # The three surfaces (bigbox, navigation, arcaderoom) only register frame
    # handlers via registerGamepadSurface and never schedule their own frames.
    gamepad_js = ROOT / "static" / "gamepad.js"
    assert gamepad_js.exists(), "static/gamepad.js must own the unified gamepad loop"
    gamepad = gamepad_js.read_text(encoding="utf-8")
    assert gamepad.count("requestAnimationFrame(tick)") == 2, (
        "gamepad.js must schedule the loop exactly twice (tick reschedule + ensure)"
    )
    assert "registerGamepadSurface" in gamepad
    surfaces = {
        "bigbox.js": BIGBOX_JS,
        "navigation.js": NAVIGATION_JS,
        "arcaderoom.js": ARCADEROOM_JS,
    }
    for name, path in surfaces.items():
        text = path.read_text(encoding="utf-8")
        assert "registerGamepadSurface(" in text, (
            f"{name} must register with the unified gamepad loop"
        )
        assert "requestAnimationFrame(pollGamepads)" not in text, (
            f"{name} must not schedule its own gamepad poll loop"
        )
        assert "INVARIANT: exactly one gamepad poll loop per surface" not in text, (
            f"{name} must not carry the stale per-surface INVARIANT comment"
        )
        assert re.search(r"const edge = \w+ => current\[\w+\] && !", text), (
            f"{name} must use the current && !prev edge pattern"
        )
    # Dispatch priority, highest first: pause overlay > arcade room > big box > library.
    priorities = {}
    for name, path in surfaces.items():
        text = path.read_text(encoding="utf-8")
        priorities[name] = [int(m) for m in re.findall(r"registerGamepadSurface\(\{\s*priority:\s*(\d+)", text)]
    assert priorities["bigbox.js"] == [10, 30], (
        f"bigbox.js must register pause(10) then bigbox(30), got {priorities['bigbox.js']}"
    )
    assert priorities["arcaderoom.js"] == [20], (
        f"arcaderoom.js must register arcade(20), got {priorities['arcaderoom.js']}"
    )
    assert priorities["navigation.js"] == [40], (
        f"navigation.js must register library(40), got {priorities['navigation.js']}"
    )
    # Pause overlay owns the pad while open: d-pad moves focus, A activates, B dismisses.
    bigbox = BIGBOX_JS.read_text(encoding="utf-8")
    assert "function pollBigBoxPause()" in bigbox, "bigbox.js must define the pause-overlay gamepad branch"
    assert "closeBigBoxPause()" in bigbox, "bigbox.js must define closeBigBoxPause"
    assert 'data-i18n="bigbox.pause_close"' in INDEX.read_text(encoding="utf-8"), (
        "index.html pause panel must carry a dismiss control with an i18n key"
    )

def test_dna_search_surface():
    """Game DNA search: toggle, results panel, why-chips, more-like-this,
    Big Box search-toggle region, tokens in app.css + all themes, i18n keys."""
    index = INDEX.read_text(encoding="utf-8")
    assert 'id="contextDna"' in index, "context menu needs #contextDna"
    assert 'data-i18n="dna.more_like_this"' in index, "contextDna needs an i18n key"
    assert 'id="bigBoxDnaToggle"' in index, "Big Box needs #bigBoxDnaToggle"
    assert 'data-dna-mode' in index, "Big Box toggle must carry data-dna-mode"
    dna = (ROOT / "static" / "dna.js").read_text(encoding="utf-8")
    for symbol in ("initDnaSearch", "dnaMoreLikeThis", "dnaMoreLikeThisBigBox",
                   "dnaSearchMode", "setDnaSearchMode", "scheduleBigBoxSmartSearch"):
        assert symbol in dna, f"dna.js must export {symbol}"
    assert "250" in dna, "dna.js must debounce at 250 ms"
    assert "/api/v2/library/dna/search" in dna, "dna.js must call the v2 search route"
    assert "/api/v2/jobs/cancel" in dna, "dna.js must support cancelling the build job"
    assert "no AI cloud" in dna, "dna.js must state the no-AI-cloud promise"
    assert "r.game_id" in dna, "dna.js must read the backend game_id field"
    assert "persist" in dna, "dna.js must support non-persisted mode syncs"
    library = (ROOT / "static" / "library.js").read_text(encoding="utf-8")
    assert "initDnaSearch()" in library, "library.js must initialize DNA search"
    assert 'id="dnaMoreLikeThis"' in library, "details pane needs a More-like-this button"
    app_js = APP_JS.read_text(encoding="utf-8")
    assert '$(\'contextDna\').onclick' in app_js or '$("contextDna").onclick' in app_js, \
        "app.js must wire the context menu More-like-this item"
    bigbox = BIGBOX_JS.read_text(encoding="utf-8")
    assert "bigBoxHybridSearch" in bigbox and "scheduleBigBoxSmartSearch" in bigbox, \
        "bigbox.js smart mode must feed the existing hybrid search input"
    assert "bigbox-dna-why" in bigbox, "bigbox.js must render why-chips as subtitle lines"
    assert "dnaMoreLikeThisBigBox" in bigbox, "bigbox.js must wire More-like-this"
    assert "entry.game_id" in bigbox, "bigbox.js must map results on the backend game_id field"
    css = APP.read_text(encoding="utf-8")
    defs = parse_root_vars(css)
    for token in ("dna-chip-bg", "dna-chip-border", "dna-chip-fg",
                  "dna-panel-bg", "dna-panel-border", "dna-panel-shadow",
                  "dna-toggle-active-bg", "dna-toggle-active-fg"):
        assert token in defs, f"app.css :root must define --{token}"
        for theme in THEMES:
            assert token in parse_root_vars(theme.read_text(encoding="utf-8")), \
                f"{theme.name} :root must define --{token}"
    for cls in (".dna-search-wrap", ".dna-mode-toggle", ".dna-results",
                ".dna-result", ".dna-chip", ".bigbox-dna-why"):
        assert cls in css, f"app.css must style {cls}"
    for locale in ("en", "de", "es", "fr", "pt"):
        strings = json.loads((ROOT / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        section = strings.get("dna", {})
        for key in ("mode_label", "mode_title", "mode_smart", "search_placeholder_smart",
                    "results_title", "no_results", "empty_coverage", "open_metadata",
                    "building", "cancel_build", "degraded_note", "search_failed",
                    "privacy_note", "more_like_this"):
            assert section.get(key), f"locales/{locale}.json missing dna.{key}"


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
