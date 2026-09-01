"""Named filter presets with optional Big Box quick-switch slots."""

PRESET_RULE_KEYS = (
    "platform",
    "view",
    "query",
    "esrb",
    "progress",
    "favorite",
    "installed",
    "platform_category",
    "genre",
    "developer",
    "publisher",
    "hidden",
    "has_saves",
    "has_achievements",
    "has_missing_media",
    "has_highscores",
)
EXPLORER_FIELDS = {"genre", "developer", "publisher", "platform", "progress", "esrb"}


def normalize_rules(rules):
    if not isinstance(rules, dict):
        raise ValueError("Preset rules must be an object.")
    clean = {}
    for key in PRESET_RULE_KEYS:
        if key not in rules:
            continue
        value = rules[key]
        if key in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores"):
            if value in (True, False, "true", "false"):
                clean[key] = value is True or value == "true"
            continue
        if key == "installed":
            if value in ("installed", "uninstalled", "all", True, False):
                clean[key] = "installed" if value is True else "uninstalled" if value is False else str(value)
            continue
        text = str(value).strip()
        if text and text != "all":
            clean[key] = text
    if not clean:
        raise ValueError("Preset needs at least one filter rule.")
    return clean


def list_presets(state):
    presets = state.get("filter_presets", [])
    if not isinstance(presets, list):
        return []
    return [item for item in presets if isinstance(item, dict) and item.get("name")]


def save_preset(state, name, rules, bigbox_quick=False):
    name = str(name).strip()
    if not name:
        raise ValueError("Preset name is required.")
    clean_rules = normalize_rules(rules)
    presets = state.setdefault("filter_presets", [])
    if not isinstance(presets, list):
        presets = []
        state["filter_presets"] = presets
    entry = {"name": name, "rules": clean_rules, "bigbox_quick": bool(bigbox_quick)}
    for index, item in enumerate(presets):
        if isinstance(item, dict) and item.get("name") == name:
            presets[index] = entry
            return name
    presets.append(entry)
    return name


def delete_preset(state, name):
    name = str(name).strip()
    presets = state.get("filter_presets", [])
    if not isinstance(presets, list):
        return False
    kept = [item for item in presets if not isinstance(item, dict) or item.get("name") != name]
    if len(kept) == len(presets):
        return False
    state["filter_presets"] = kept
    return True


def bigbox_quick_presets(state, limit=8):
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 8
    return [item for item in list_presets(state) if item.get("bigbox_quick")][:limit]


def _matches_platform(game, rules):
    platform = str(rules.get("platform", "")).strip()
    if platform and platform != "all" and str(game.get("platform", "")) != platform:
        return False
    return True


def _matches_view(game, rules):
    view = str(rules.get("view", "")).strip()
    if view and view != "all":
        if view == "favorites" and not game.get("favorite"):
            return False
        if view == "hidden" and not game.get("hidden"):
            return False
        if view == "installed" and game.get("store_installed") is False:
            return False
        if view == "uninstalled" and game.get("store_installed") is not False:
            return False
    return True


def _matches_esrb_progress(game, rules):
    esrb = str(rules.get("esrb", "")).strip()
    if esrb and str(game.get("esrb", "")) != esrb:
        return False
    progress = str(rules.get("progress", "")).strip()
    if progress and str(game.get("progress", "")) != progress:
        return False
    return True


def _matches_booleans(game, rules):
    if "favorite" in rules and bool(game.get("favorite")) != bool(rules["favorite"]):
        return False
    if "hidden" in rules and bool(game.get("hidden")) != bool(rules["hidden"]):
        return False
    if "has_saves" in rules:
        actual = bool(game.get("save_paths") or game.get("has_saves"))
        if actual != bool(rules["has_saves"]):
            return False
    if "has_achievements" in rules:
        actual = bool(game.get("ra_game_id") or game.get("has_achievements"))
        if actual != bool(rules["has_achievements"]):
            return False
    if "has_missing_media" in rules:
        actual = bool(game.get("has_missing_media") or (not game.get("cover") or not game.get("background")))
        if actual != bool(rules["has_missing_media"]):
            return False
    if "has_highscores" in rules:
        actual = bool(game.get("has_highscores"))
        if actual != bool(rules["has_highscores"]):
            return False
    return True


def _matches_installed_state(game, rules):
    installed = rules.get("installed")
    if installed == "installed" and game.get("store_installed") is False:
        return False
    if installed == "uninstalled" and game.get("store_installed") is not False:
        return False
    return True


def _matches_text_fields(game, rules):
    genre = str(rules.get("genre", "")).strip()
    if genre and genre.casefold() not in str(game.get("genre", "")).casefold():
        return False
    developer = str(rules.get("developer", "")).strip()
    if developer and developer.casefold() not in str(game.get("developer", "")).casefold():
        return False
    publisher = str(rules.get("publisher", "")).strip()
    if publisher and publisher.casefold() not in str(game.get("publisher", "")).casefold():
        return False
    return True


def _matches_query(game, rules):
    query = str(rules.get("query", "")).strip().casefold()
    if query:
        haystack = " ".join(
            str(game.get(field, "")) for field in (
                "name", "sort_title", "platform", "genre", "developer", "publisher", "series", "notes",
            )
        ).casefold()
        if query in haystack:
            return True
        if 2 <= len(query) <= 8 and query.isalnum():
            import re
            name = str(game.get("name", "")).strip()
            if name:
                words = re.findall(r"[A-Za-z0-9]+", name)
                acronym = "".join(w[0] for w in words).casefold()
                if query == acronym or query in acronym:
                    return True
                if words and words[0].casefold() in ("the", "a", "an"):
                    sub_acronym = "".join(w[0] for w in words[1:]).casefold()
                    if query == sub_acronym or query in sub_acronym:
                        return True
        return False
    return True


def game_matches_rules(game, rules):
    if not isinstance(game, dict) or not isinstance(rules, dict):
        return True
    return all(
        helper(game, rules)
        for helper in (
            _matches_platform,
            _matches_view,
            _matches_esrb_progress,
            _matches_booleans,
            _matches_installed_state,
            _matches_text_fields,
            _matches_query,
        )
    )


def filter_games(games, rules, platform_category_fn=None):
    rules = rules if isinstance(rules, dict) else {}
    category = str(rules.get("platform_category", "")).strip()
    result = []
    for game in games:
        if category and category != "all" and platform_category_fn:
            if platform_category_fn(game) != category:
                continue
        if game_matches_rules(game, rules):
            result.append(game)
    return result


def explorer_facets(games, field, limit=40):
    if field not in EXPLORER_FIELDS:
        return []
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 40
    counts = {}
    for game in games:
        if game.get("hidden"):
            continue
        if field == "genre":
            for part in str(game.get("genre", "")).split(","):
                label = part.strip()
                if label:
                    counts[label] = counts.get(label, 0) + 1
        elif field == "developer":
            label = str(game.get("developer", "")).strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        elif field == "publisher":
            label = str(game.get("publisher", "")).strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        elif field == "platform":
            label = str(game.get("platform", "Unspecified")).strip() or "Unspecified"
            counts[label] = counts.get(label, 0) + 1
        elif field == "progress":
            label = str(game.get("progress", "")).strip() or "Unset"
            counts[label] = counts.get(label, 0) + 1
        elif field == "esrb":
            label = str(game.get("esrb", "")).strip() or "Unrated"
            counts[label] = counts.get(label, 0) + 1
    items = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    return [{"value": value, "count": count} for value, count in items[:limit]]


# ---------------------------------------------------------------------------
# Visual chip builder for Smart Collections (1.7.2)
# ---------------------------------------------------------------------------

CHIP_LABELS = {
    "platform": "Platform",
    "genre": "Genre",
    "view": "View",
    "esrb": "ESRB",
    "progress": "Progress",
    "developer": "Developer",
    "publisher": "Publisher",
    "favorite": "Favorite",
    "hidden": "Hidden",
    "installed": "Installed",
    "has_saves": "Has saves",
    "has_achievements": "Has achievements",
    "has_missing_media": "Missing media",
    "has_highscores": "Has high scores",
    "query": "Search",
}


def rules_to_chips(rules):
    """Convert preset rules into visual chip descriptors for the UI.

    Returns a list of {key, label, value, display} dicts.
    """
    if not isinstance(rules, dict):
        return []
    chips = []
    for key, value in rules.items():
        if key not in CHIP_LABELS:
            continue
        label = CHIP_LABELS[key]
        if isinstance(value, bool):
            display = "Yes" if value else "No"
        else:
            display = str(value)
        chips.append({"key": key, "label": label, "value": value, "display": display})
    return chips


def chips_to_rules(chips):
    """Convert visual chip descriptors back into preset rules.

    Returns a normalized rules dict.
    """
    rules = {}
    if not isinstance(chips, list):
        return rules
    for chip in chips:
        if not isinstance(chip, dict):
            continue
        key = str(chip.get("key") or "").strip()
        if not key or key not in CHIP_LABELS:
            continue
        value = chip.get("value")
        if key in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores"):
            rules[key] = bool(value)
        elif key == "installed":
            rules[key] = str(value).strip() if value else "all"
        else:
            text = str(value).strip()
            if text and text != "all":
                rules[key] = text
    return rules

