#!/usr/bin/env python3
"""Source-level regressions for frontend modules that have no JS test runner.

Node is not part of the toolchain, so these read static/*.js and pin the
shape of each fix: insights momentum sign, constellation hover tooltip,
Wrapped year picker, party-mode Enter handling, and moments auto-capture.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def _src(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _braced_body(source, start):
    """Return the text between the first `{` at/after `start` and its match."""
    open_at = source.index("{", start)
    depth = 0
    for index in range(open_at, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[open_at + 1:index]
    raise AssertionError("unbalanced braces")


def test_insights_negative_momentum_formats_magnitude():
    src = _src("insights.js")
    line = next(row for row in src.splitlines() if "const deltaLabel" in row)
    # formatHours(negative) hits the "< 1 hour" branch and prints minutes.
    assert "`${formatHours(delta)}" not in line, "negative delta must not go through formatHours unsigned"
    assert "-${formatHours(-delta)}" in line


def test_constellation_hover_updates_tooltip():
    src = _src("constellation.js")
    start = src.index("window.addEventListener('mousemove'")
    body = _braced_body(src, src.index("=>", start))
    after_hover = body[body.index("hovered = n;"):]
    assert "lastMouse = { x: e.clientX, y: e.clientY }" in after_hover, "hover must move the tooltip anchor"
    assert "draw()" in after_hover, "hover must repaint so the tooltip shows after the layout settles"


def test_wrapped_year_select_is_populated():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<select id="wrappedYear">(.*?)</select>', html, re.DOTALL)
    assert match is not None
    src = _src("wrapped.js")
    if "<option" not in match.group(1):
        assert "new Option(" in src, "wrapped.js must fill the empty #wrappedYear select"
        open_body = _braced_body(src, src.index("function openWrapped"))
        assert "syncYearSelect(year)" in open_body


def test_party_enter_on_secondary_buttons_is_not_hijacked():
    src = _src("party.js")
    body = _braced_body(src, src.index("function partyKeydown"))
    # Exactly one Enter path runs primaryAction: the non-button one.
    assert body.count("primaryAction()") == 1, body
    guard = body.index("event.key === 'Enter' && tag !== 'BUTTON'")
    assert body.index("primaryAction()") > guard


def _server_progress_labels():
    tree = ast.parse((ROOT / "handlers" / "moments.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "labels" for t in node.targets):
            return {ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(node.value.keys, node.value.values, strict=True)}
    raise AssertionError("labels map not found in handlers/moments.py")


def test_moments_progress_trigger_understands_labels():
    # Labels are scored by pure.js::progressScore, which moments.js imports. tests/js/pure.test.mjs
    # pins the values; this pins that the table matches the server's labels.
    src = _src("moments.js")
    assert "Number(game?.progress" not in src, "Number('Beaten') is NaN; progress labels must be scored"
    assert "progressScore(" in src and "from './pure.js'" in src, "moments.js must score progress through pure.js"
    match = re.search(r"const PROGRESS_LABEL_SCORES = \{([^}]*)\}", _src("pure.js"))
    assert match, "pure.js must hold the progress label table"
    client = {key: float(value) for key, value in re.findall(r"(\w+):\s*([\d.]+)", match.group(1))}
    assert client == _server_progress_labels(), (client, _server_progress_labels())


def test_moments_autocapture_skips_newly_added_games():
    src = _src("moments.js")
    start = src.index("document.addEventListener('app:state-refreshed'")
    body = _braced_body(src, src.index("=>", start))
    skip = body.index("if (!previousSnapshots.has(gameId)) continue;")
    assert skip < body.index("autoMomentTrigger("), "new games must be skipped before diffing"


if __name__ == "__main__":
    tests = sorted((name, obj) for name, obj in list(globals().items()) if name.startswith("test_") and callable(obj))
    failures = 0
    for name, test in tests:
        try:
            test()
            print(f"PASS {name}")
        except AssertionError as error:
            print(f"FAIL {name}: {error}")
            failures += 1
    if failures:
        print(f"SOME FAIL ({failures} of {len(tests)})")
        raise SystemExit(1)
    print(f"ALL PASS ({len(tests)} tests)")
