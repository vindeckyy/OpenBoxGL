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
        if key == "favorite":
            if value in (True, False, "true", "false"):
                clean[key] = value is True or value == "true"
            continue
        if key == "hidden":
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


def game_matches_rules(game, rules):
    if not isinstance(game, dict) or not isinstance(rules, dict):
        return True
    platform = str(rules.get("platform", "")).strip()
    if platform and platform != "all" and str(game.get("platform", "")) != platform:
        return False
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
    esrb = str(rules.get("esrb", "")).strip()
    if esrb and str(game.get("esrb", "")) != esrb:
        return False
    progress = str(rules.get("progress", "")).strip()
    if progress and str(game.get("progress", "")) != progress:
        return False
    if "favorite" in rules and bool(game.get("favorite")) != bool(rules["favorite"]):
        return False
    if "hidden" in rules and bool(game.get("hidden")) != bool(rules["hidden"]):
        return False
    installed = rules.get("installed")
    if installed == "installed" and game.get("store_installed") is False:
        return False
    if installed == "uninstalled" and game.get("store_installed") is not False:
        return False
    genre = str(rules.get("genre", "")).strip()
    if genre and genre.casefold() not in str(game.get("genre", "")).casefold():
        return False
    developer = str(rules.get("developer", "")).strip()
    if developer and developer.casefold() not in str(game.get("developer", "")).casefold():
        return False
    publisher = str(rules.get("publisher", "")).strip()
    if publisher and publisher.casefold() not in str(game.get("publisher", "")).casefold():
        return False
    query = str(rules.get("query", "")).strip().casefold()
    if query:
        haystack = " ".join(
            str(game.get(field, "")) for field in (
                "name", "sort_title", "platform", "genre", "developer", "publisher", "series", "notes",
            )
        ).casefold()
        if query not in haystack:
            return False
    return True


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

