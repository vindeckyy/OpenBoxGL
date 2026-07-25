"""Optional IGDB metadata provider."""

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from env_config import ensure_env_loaded

IGDB_ENDPOINT = "https://api.igdb.com/v4"
_TOKEN_CACHE = {"client_id": "", "value": "", "expires": 0.0}


def credentials():
    ensure_env_loaded()
    client_id = os.environ.get("IGDB_CLIENT_ID", "").strip()
    client_secret = os.environ.get("IGDB_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ValueError("Set IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in ~/.env to use IGDB.")
    return client_id, client_secret


def access_token(client_id=None, client_secret=None):
    now = time.time()
    client_id = client_id or credentials()[0]
    if (
        _TOKEN_CACHE["client_id"] == client_id
        and _TOKEN_CACHE["value"]
        and _TOKEN_CACHE["expires"] > now + 30
    ):
        return _TOKEN_CACHE["value"]
    client_secret = client_secret or credentials()[1]
    body = urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    }).encode()
    request = Request(
        "https://id.twitch.tv/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    token = payload.get("access_token", "")
    if not token:
        raise ValueError("IGDB token request failed.")
    _TOKEN_CACHE["client_id"] = client_id
    _TOKEN_CACHE["value"] = token
    _TOKEN_CACHE["expires"] = now + int(payload.get("expires_in", 3600))
    return token


def igdb_request(path, query, client_id=None, client_secret=None):
    client_id = client_id or credentials()[0]
    token = access_token(client_id, client_secret)
    request = Request(
        f"{IGDB_ENDPOINT}/{path}",
        data=query.encode(),
        headers={
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def search_games(name, platform="", limit=12):
    name = str(name).strip()
    if not name:
        return []
    try:
        limit = min(50, max(1, int(limit)))
    except (TypeError, ValueError):
        limit = 12
    query = f'search "{name.replace(chr(34), "")}"; fields id,name,summary,first_release_date,genres.name,platforms.name,rating,aggregated_rating; limit {int(limit)};'
    records = igdb_request("games", query)
    results = []
    for record in records:
        if not isinstance(record, dict):
            continue
        results.append({
            "id": record.get("id"),
            "name": record.get("name", ""),
            "summary": record.get("summary", ""),
            "year": _year_from_timestamp(record.get("first_release_date")),
            "genres": ", ".join(item.get("name", "") for item in record.get("genres", []) if isinstance(item, dict)),
            "platforms": ", ".join(item.get("name", "") for item in record.get("platforms", []) if isinstance(item, dict)),
            "rating": record.get("rating"),
            "critic_score": record.get("aggregated_rating"),
        })
    if platform:
        platform = platform.casefold()
        filtered = [item for item in results if platform in item.get("platforms", "").casefold()]
        if filtered:
            return filtered
    return results


def fetch_game(game_id):
    game_id = int(game_id)
    query = (
        f"fields name,summary,storyline,first_release_date,genres.name,platforms.name,"
        f"involved_companies.company.name,involved_companies.developer,involved_companies.publisher,"
        f"rating,aggregated_rating,time_to_beat.normally,time_to_beat.completely;"
        f" where id = {game_id};"
    )
    records = igdb_request("games", query)
    if not records:
        raise ValueError("IGDB game not found.")
    record = records[0]
    developers, publishers = [], []
    for entry in record.get("involved_companies", []):
        if not isinstance(entry, dict):
            continue
        company = entry.get("company", {})
        name = company.get("name", "") if isinstance(company, dict) else ""
        if not name:
            continue
        if entry.get("developer"):
            developers.append(name)
        if entry.get("publisher"):
            publishers.append(name)
    beat = record.get("time_to_beat") or {}
    return {
        "name": record.get("name", ""),
        "description": record.get("summary") or record.get("storyline") or "",
        "year": _year_from_timestamp(record.get("first_release_date")),
        "genre": ", ".join(item.get("name", "") for item in record.get("genres", []) if isinstance(item, dict)),
        "developer": ", ".join(developers),
        "publisher": ", ".join(publishers),
        "rating": round(float(record.get("rating", 0) or 0) / 20, 2) if record.get("rating") else None,
        "critic_score": record.get("aggregated_rating"),
        "time_to_beat_hours": beat.get("normally") or beat.get("completely"),
        "igdb_id": game_id,
    }


def apply_to_game(game, metadata):
    if not isinstance(game, dict) or not isinstance(metadata, dict):
        raise ValueError("Invalid IGDB metadata payload.")
    for field in ("name", "description", "year", "genre", "developer", "publisher"):
        value = metadata.get(field)
        if value:
            game[field] = value
    if metadata.get("rating") is not None:
        game["rating"] = metadata["rating"]
    if metadata.get("igdb_id") is not None:
        game["igdb_id"] = metadata["igdb_id"]
    if metadata.get("time_to_beat_hours"):
        game["time_to_beat_hours"] = metadata["time_to_beat_hours"]
    return game


def _year_from_timestamp(value):
    try:
        return str(time.gmtime(int(value)).tm_year)
    except (OverflowError, TypeError, ValueError, OSError):
        return ""
