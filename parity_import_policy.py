"""Import exclusion list for storefront rescans."""


def exclusion_key(game):
    if game.get("steam_app_id"):
        return ("steam", str(game["steam_app_id"]))
    if game.get("heroic_app_id"):
        return ("heroic", str(game.get("source", "")), str(game["heroic_app_id"]))
    if game.get("lutris_id"):
        return ("lutris", str(game["lutris_id"]))
    if game.get("gameyfin_id"):
        return ("gameyfin", str(game["gameyfin_id"]))
    return None


def list_exclusions(state):
    items = state.get("settings", {}).get("import_exclusions", [])
    return items if isinstance(items, list) else []


def exclusion_set(state):
    blocked = set()
    for item in list_exclusions(state):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", "")).strip().casefold()
        external_id = str(item.get("external_id", "")).strip()
        if source and external_id:
            blocked.add((source, external_id))
        heroic = item.get("heroic")
        if isinstance(heroic, (list, tuple)) and len(heroic) == 3:
            blocked.add(tuple(str(part) for part in heroic))
    return blocked


def add_exclusion(state, source, external_id, heroic_source=""):
    source_key = str(source).strip().casefold()
    external_id = str(external_id).strip()
    if not source_key or not external_id:
        raise ValueError("Source and external id are required.")
    entry = {"source": source_key, "external_id": external_id}
    if heroic_source:
        entry["heroic"] = ["heroic", str(heroic_source).strip().casefold(), external_id]
    items = list_exclusions(state)
    if any(
        isinstance(item, dict)
        and str(item.get("source", "")).casefold() == source_key
        and str(item.get("external_id", "")) == external_id
        for item in items
    ):
        return entry
    items.append(entry)
    state.setdefault("settings", {})["import_exclusions"] = items
    return entry


def remove_exclusion(state, source, external_id):
    source_key = str(source).strip().casefold()
    external_id = str(external_id).strip()
    items = [
        item for item in list_exclusions(state)
        if not (
            isinstance(item, dict)
            and str(item.get("source", "")).casefold() == source_key
            and str(item.get("external_id", "")) == external_id
        )
    ]
    state.setdefault("settings", {})["import_exclusions"] = items
    return True


def filter_imported(imported, state):
    blocked = exclusion_set(state)
    if not blocked:
        return imported
    kept = []
    for game in imported:
        key = exclusion_key(game)
        if not key:
            kept.append(game)
            continue
        normalized = tuple(str(part).casefold() if index == 0 or (len(key) == 3 and index == 1) else str(part) for index, part in enumerate(key))
        if normalized in blocked or (len(normalized) >= 2 and (normalized[0], normalized[1]) in blocked):
            continue
        kept.append(game)
    return kept
