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
    "source",
    "achievements_source",
    "region",
    "vertical",
    "requires_chd",
    "exclude",
)
# Exclusions deliberately cannot hide/show "hidden" or switch the category
# context: their semantics are field-level rejection, not another view.
EXCLUDE_FIELDS = set(PRESET_RULE_KEYS) - {"exclude", "view", "platform_category"}
TEXT_RULE_FIELDS = {"genre", "developer", "publisher", "region"}
TEXT_OPERATORS = {"contains_any", "not_contains_any"}
EXPLORER_FIELDS = {"genre", "developer", "publisher", "platform", "progress", "esrb"}


def _clean_rule_values(value, *, item_limit=80):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("Filter values must be a list of at most 20 entries.")
    values = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ValueError("Filter values must be strings.")
        text = str(item).strip()[:item_limit]
        if text and text not in values:
            values.append(text)
    return values


def _clean_source_rule(value):
    if isinstance(value, str):
        value = [value]
    values = sorted(_clean_rule_values(value))
    return values or None


def _clean_text_rule(value):
    if isinstance(value, dict):
        operator = str(value.get("op", "")).strip()
        if operator not in TEXT_OPERATORS:
            raise ValueError("Text filter operator must be contains_any or not_contains_any.")
        values = _clean_rule_values(value.get("values"), item_limit=80)
        return {"op": operator, "values": values} if values else None
    text = str(value).strip()
    return text if text and text != "all" else None


def _clean_exclude(exclude):
    if exclude in (None, {}):
        return {}
    if not isinstance(exclude, dict):
        raise ValueError("Preset exclusions must be an object.")
    clean = {}
    for field, raw in exclude.items():
        if field not in EXCLUDE_FIELDS:
            continue
        if field == "query":
            text = str(raw).strip()[:500] if isinstance(raw, str) else ""
            if text:
                clean[field] = text
            continue
        if field in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores", "vertical", "requires_chd"):
            if raw in (True, False, "true", "false"):
                clean[field] = raw is True or raw == "true"
            continue
        values = raw if isinstance(raw, list) else [raw]
        if len(values) > 20:
            raise ValueError("Preset exclusions allow at most 20 values per field.")
        cleaned = []
        for item in values:
            if isinstance(item, bool):
                if field == "installed":
                    item = "installed" if item else "uninstalled"
                elif len(values) == 1:
                    cleaned = [item]
                    break
                else:
                    raise ValueError("Boolean exclusions must be a single value.")
            if not isinstance(item, (str, int, float)) or isinstance(item, bool):
                raise ValueError("Preset exclusion values must be strings, numbers, or booleans.")
            text = str(item).strip()[:80]
            if text and text not in cleaned:
                cleaned.append(text)
        if cleaned:
            clean[field] = cleaned if isinstance(raw, list) else cleaned[0]
    return clean


def normalize_rules(rules):
    if not isinstance(rules, dict):
        raise ValueError("Preset rules must be an object.")
    clean = {}
    for key in PRESET_RULE_KEYS:
        if key not in rules:
            continue
        value = rules[key]
        if key == "exclude":
            exclude = _clean_exclude(value)
            if exclude:
                clean[key] = exclude
            continue
        if key in ("source", "achievements_source"):
            normalized = _clean_source_rule(value)
            if normalized:
                clean[key] = normalized
            continue
        if key in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores", "vertical", "requires_chd"):
            if value in (True, False, "true", "false"):
                clean[key] = value is True or value == "true"
            continue
        if key == "installed":
            if value in ("installed", "uninstalled", "all", True, False):
                clean[key] = "installed" if value is True else "uninstalled" if value is False else str(value)
            continue
        if key in TEXT_RULE_FIELDS:
            normalized = _clean_text_rule(value)
            if normalized:
                clean[key] = normalized
            continue
        text = str(value).strip()
        if key == "query":
            text = text[:500]
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
    if progress:
        # F4a: "Unplayed" is the UI label for unset ""; they match each other.
        stored = str(game.get("progress", ""))
        if progress.casefold() == "unplayed":
            if stored not in ("", "Unplayed"):
                return False
        elif stored != progress:
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
        sources = game.get("achievement_sources")
        any_source = isinstance(sources, dict) and any(
            isinstance(entry, dict) and (int(entry.get("total") or 0) > 0 or int(entry.get("earned") or 0) > 0)
            for entry in sources.values()
        )
        actual = bool(game.get("has_achievements") or any_source)
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
    if "vertical" in rules and bool(game.get("vertical")) != bool(rules["vertical"]):
        return False
    if "requires_chd" in rules and bool(game.get("requires_chd")) != bool(rules["requires_chd"]):
        return False
    return True


def _matches_source(game, rules):
    sources = rules.get("source")
    if not sources:
        return True
    values = sources if isinstance(sources, list) else [sources]
    actual = str(game.get("source", "")).strip().casefold()
    return bool(actual) and actual in {str(value).strip().casefold() for value in values}


def _matches_achievements_source(game, rules):
    sources = rules.get("achievements_source")
    if not sources:
        return True
    values = sources if isinstance(sources, list) else [sources]
    entries = game.get("achievement_sources")
    if not isinstance(entries, dict):
        return False
    for source in values:
        entry = entries.get(source)
        if isinstance(entry, dict) and (int(entry.get("total") or 0) > 0 or int(entry.get("earned") or 0) > 0):
            return True
    return False


def _matches_installed_state(game, rules):
    installed = rules.get("installed")
    if installed == "installed" and game.get("store_installed") is False:
        return False
    if installed == "uninstalled" and game.get("store_installed") is not False:
        return False
    return True


def _text_matches(value, rule):
    """Apply a plain substring rule or a contains_any/not_contains_any rule."""
    haystack = str(value or "").casefold()
    if isinstance(rule, dict):
        operator = str(rule.get("op", ""))
        values = rule.get("values") if isinstance(rule.get("values"), list) else []
        matched = any(str(candidate).casefold() in haystack for candidate in values)
        return matched if operator == "contains_any" else not matched
    return str(rule).strip().casefold() in haystack


def _matches_text_fields(game, rules):
    for field in ("genre", "developer", "publisher", "region"):
        if field in rules and not _text_matches(game.get(field, ""), rules[field]):
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


def _matches_rule_value(game, field, value):
    """Return whether one exclusion value matches the game's field."""
    if field == "query":
        return _matches_query(game, {"query": value})
    if field == "source":
        return _matches_source(game, {"source": [value]})
    if field == "achievements_source":
        return _matches_achievements_source(game, {"achievements_source": [value]})
    if field == "region":
        return _text_matches(game.get("region", ""), value)
    if field in ("genre", "developer", "publisher"):
        return _text_matches(game.get(field, ""), value)
    if field == "platform":
        return str(game.get("platform", "")).casefold() == str(value).casefold()
    if field == "esrb":
        return str(game.get("esrb", "")).casefold() == str(value).casefold()
    if field == "progress":
        return _matches_esrb_progress(game, {"progress": value})
    if field == "installed":
        return _matches_installed_state(game, {"installed": value})
    if field in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores"):
        return _matches_booleans(game, {field: value})
    if field in ("vertical", "requires_chd"):
        return bool(game.get(field)) == bool(value)
    return False


def game_matches_exclusions(game, exclude):
    """Return True when the game should be rejected by an exclusion rule."""
    if not isinstance(game, dict) or not isinstance(exclude, dict):
        return False
    for field, raw in exclude.items():
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            if _matches_rule_value(game, field, value):
                return True
    return False


def game_matches_rules(game, rules):
    if not isinstance(game, dict) or not isinstance(rules, dict):
        return True
    matched = all(
        helper(game, rules)
        for helper in (
            _matches_platform,
            _matches_view,
            _matches_esrb_progress,
            _matches_booleans,
            _matches_installed_state,
            _matches_text_fields,
            _matches_source,
            _matches_achievements_source,
            _matches_query,
        )
    )
    return matched and not game_matches_exclusions(game, rules.get("exclude"))


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
            label = str(game.get("progress", "")).strip() or "Unplayed"
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
    "vertical": "Vertical",
    "requires_chd": "Requires CHD",
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
        if key in ("favorite", "hidden", "has_saves", "has_achievements", "has_missing_media", "has_highscores", "vertical", "requires_chd"):
            rules[key] = bool(value)
        elif key == "installed":
            rules[key] = str(value).strip() if value else "all"
        else:
            text = str(value).strip()
            if text and text != "all":
                rules[key] = text
    return rules

