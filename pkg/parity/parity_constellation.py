"""parity_constellation.py — relationship graph for a game library."""
from __future__ import annotations

from datetime import datetime
from itertools import combinations

# ponytail: static platform family map. If OpenBox gets a user-editable platform
# taxonomy, replace this with the settings-derived mapping.
PLATFORM_FAMILIES = {
    "PC": "Computer",
    "MS-DOS": "Computer",
    "Windows": "Computer",
    "Linux": "Computer",
    "macOS": "Computer",
    "Steam": "Computer",
    "SNES": "Nintendo",
    "NES": "Nintendo",
    "Nintendo 64": "Nintendo",
    "GameCube": "Nintendo",
    "Wii": "Nintendo",
    "Wii U": "Nintendo",
    "Switch": "Nintendo",
    "Game Boy": "Nintendo",
    "Game Boy Color": "Nintendo",
    "Game Boy Advance": "Nintendo",
    "Nintendo DS": "Nintendo",
    "Nintendo 3DS": "Nintendo",
    "Sega Genesis": "Sega",
    "Sega Mega Drive": "Sega",
    "Sega Master System": "Sega",
    "Sega Game Gear": "Sega",
    "Sega Saturn": "Sega",
    "Sega Dreamcast": "Sega",
    "Sega CD": "Sega",
    "PlayStation": "Sony",
    "PlayStation 2": "Sony",
    "PlayStation 3": "Sony",
    "PlayStation 4": "Sony",
    "PlayStation 5": "Sony",
    "PSP": "Sony",
    "PlayStation Vita": "Sony",
    "Xbox": "Microsoft",
    "Xbox 360": "Microsoft",
    "Xbox One": "Microsoft",
    "Xbox Series X": "Microsoft",
    "Arcade": "Arcade",
    "MAME": "Arcade",
    "Neo Geo": "Arcade",
    "Neo Geo AES": "Arcade",
    "Atari 2600": "Atari",
    "Atari 5200": "Atari",
    "Atari 7800": "Atari",
    "Atari Jaguar": "Atari",
    "Atari Lynx": "Atari",
    "Atari ST": "Atari",
    "Amiga": "Commodore",
    "Commodore 64": "Commodore",
    "Amstrad CPC": "Z80",
    "ZX Spectrum": "Z80",
    "Turbografx-16": "NEC",
    "PC Engine": "NEC",
}

KINDS = ("series", "developer", "publisher", "genre", "platform_family", "co_played")
KIND_WEIGHT = {
    "series": 1.0,
    "developer": 0.8,
    "publisher": 0.5,
    "genre": 0.4,
    "platform_family": 0.3,
    "co_played": 0.6,
}
CO_PLAY_WINDOW_DAYS = 7


def _normalize(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _family(platform):
    return PLATFORM_FAMILIES.get(platform, "Other")


def _to_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _rank_key(game):
    return (
        -int(game.get("playtime_seconds") or 0),
        -int(game.get("play_count") or 0),
        -bool(game.get("favorite")),
        str(game.get("name") or ""),
    )


def _co_play_days(history, game_ids):
    """Return set of (i,j) tuples and shared-day counts for games played within a window."""
    days_for = {}
    for entry in history:
        game_id = entry.get("game_id")
        if game_id is None:
            continue
        started = _to_date(entry.get("started"))
        if started is None:
            continue
        if game_id not in days_for:
            days_for[game_id] = set()
        days_for[game_id].add(started)

    pairs = {}
    for a, b in combinations(game_ids, 2):
        a_days = days_for.get(a, set())
        b_days = days_for.get(b, set())
        if not a_days or not b_days:
            continue
        shared = 0
        for d in a_days:
            for delta in range(-CO_PLAY_WINDOW_DAYS, CO_PLAY_WINDOW_DAYS + 1):
                if d.fromordinal(d.toordinal() + delta) in b_days:
                    shared += 1
                    break
        if shared:
            pairs[frozenset((a, b))] = min(shared, 5)
    return pairs


def _shared_value(a, b, field):
    va = _normalize(a.get(field))
    vb = _normalize(b.get(field))
    if va and va == vb:
        return va
    return None


def build_graph(games, history, kinds=None, limit=400, focus=None, depth=1):
    """Build a deterministic relationship graph from a game list.

    Returns a dict with nodes, edges, kinds, and truncated flag.
    focus/depth optionally restrict to an ego-graph neighborhood: only nodes
    within `depth` hops of `focus` (matched by game_id or id) are returned,
    with edges reindexed. Unknown focus yields an honest empty graph.
    """
    kinds = set(kinds) if kinds else set(KINDS)
    kinds = {k for k in kinds if k in KINDS}
    if not kinds:
        kinds = set(KINDS)
    try:
        depth = int(depth)
    except (TypeError, ValueError):
        depth = 1
    depth = max(1, min(2, depth))
    focus_key = str(focus).strip() if focus is not None and str(focus).strip() else None

    candidates = sorted(games, key=_rank_key)
    truncated = len(candidates) > limit
    candidates = candidates[:limit]

    # Build node list with indices.
    id_to_index = {}
    nodes = []
    for i, game in enumerate(candidates):
        game_id = game.get("game_id") or str(game.get("id"))
        id_to_index[game_id] = i
        nodes.append({
            "i": i,
            "game_id": game_id,
            "id": game.get("id"),
            "name": str(game.get("name") or ""),
            "platform": game.get("platform", ""),
            "year": game.get("year", ""),
            "playtime_seconds": int(game.get("playtime_seconds") or 0),
            "degree": 0,
            "cover": game.get("cover", ""),
            "has_cover": bool(game.get("has_cover")),
        })

    # Pre-compute co-played pairs if requested.
    co_play = _co_play_days(history or [], [n["game_id"] for n in nodes]) if "co_played" in kinds else {}

    edges = []
    for a_idx, b_idx in combinations(range(len(nodes)), 2):
        a = candidates[a_idx]
        b = candidates[b_idx]
        best_kind = None
        best_weight = 0.0

        for kind in kinds:
            if kind == "co_played":
                continue
            if kind == "platform_family":
                va = _family(a.get("platform"))
                vb = _family(b.get("platform"))
                if va == vb and va != "Other":
                    weight = KIND_WEIGHT["platform_family"]
                    if weight > best_weight:
                        best_kind = "platform_family"
                        best_weight = weight
            else:
                shared = _shared_value(a, b, kind)
                if shared:
                    weight = KIND_WEIGHT[kind]
                    if weight > best_weight:
                        best_kind = kind
                        best_weight = weight

        # Co-play can tiebreak or win if enabled and stronger.
        cp = co_play.get(frozenset((a.get("game_id"), b.get("game_id"))))
        if "co_played" in kinds and cp:
            weight = min(cp / 5.0, 1.0) * KIND_WEIGHT["co_played"]
            if weight > best_weight:
                best_kind = "co_played"
                best_weight = weight

        if best_kind:
            edges.append({"s": a_idx, "t": b_idx, "kind": best_kind, "w": round(best_weight, 2)})
            nodes[a_idx]["degree"] += 1
            nodes[b_idx]["degree"] += 1

    # Deterministic sort.
    edges.sort(key=lambda e: (e["s"], e["t"], e["kind"]))

    base = {
        "nodes": nodes,
        "edges": edges,
        "kinds": sorted(kinds),
        "truncated": truncated,
        "empty": not nodes,
        "focus": focus_key,
        "depth": depth,
    }
    if focus_key is None:
        return base

    # Ego-graph: BFS from the focus node over undirected edges.
    focus_idx = None
    for n in nodes:
        if str(n.get("game_id") or "") == focus_key or str(n.get("id") or "") == focus_key:
            focus_idx = n["i"]
            break
    if focus_idx is None:
        return {
            "nodes": [],
            "edges": [],
            "kinds": sorted(kinds),
            "truncated": False,
            "empty": True,
            "focus": focus_key,
            "depth": depth,
        }
    adjacency: dict[int, set[int]] = {n["i"]: set() for n in nodes}
    for e in edges:
        adjacency[e["s"]].add(e["t"])
        adjacency[e["t"]].add(e["s"])
    seen: set[int] = {focus_idx}
    frontier: set[int] = {focus_idx}
    for _ in range(depth):
        nxt: set[int] = set()
        for idx in frontier:
            nxt |= adjacency.get(idx, set()) - seen
        seen |= nxt
        frontier = nxt
        if not frontier:
            break
    keep = sorted(seen)
    remap = {old: new for new, old in enumerate(keep)}
    new_nodes = []
    for new_i, old_i in enumerate(keep):
        n = dict(nodes[old_i])
        n["i"] = new_i
        new_nodes.append(n)
    new_edges = [
        {"s": remap[e["s"]], "t": remap[e["t"]], "kind": e["kind"], "w": e["w"]}
        for e in edges
        if e["s"] in remap and e["t"] in remap
    ]
    new_edges.sort(key=lambda e: (e["s"], e["t"], e["kind"]))
    return {
        "nodes": new_nodes,
        "edges": new_edges,
        "kinds": sorted(kinds),
        "truncated": truncated,
        "empty": not new_nodes,
        "focus": focus_key,
        "depth": depth,
    }
