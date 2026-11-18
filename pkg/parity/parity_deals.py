"""Deal Radar — opt-in Steam price watch & manual entries (ADR 0111)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
from typing import Any
from urllib.parse import quote_plus, urlencode, urlsplit
import urllib.request

from api_errors import (
    BadRequest,
    DealsHostBlocked,
    DealsInvalidAppId,
    DealsLimitExceeded,
    DealsNotFound,
)
from backend_io import atomic_write_text, read_limited
from notifications import add_notification
from pkg.platform_compat import parse_timestamp
from updates import VERSION

ALLOWED_HOST = "store.steampowered.com"
MAX_WATCHLIST = 200
MAX_HISTORY_POINTS = 50
MAX_NOTE_LEN = 280
MAX_NAME_LEN = 200
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_POLL_HOURS = 168
MIN_POLL_HOURS = 24
MAX_POLL_HOURS = 720
DEFAULT_MIN_DROP_PCT = 10
DEFAULT_STORE_CC = "us"
DEFAULT_SPACING_SECONDS = 1.0

_APPID_RE = re.compile(r"^\d{1,10}$")
_CC_RE = re.compile(r"^[a-z]{2}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _utc_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _utc_iso(now: datetime | None = None, now_iso: str | None = None) -> str:
    if now_iso:
        return str(now_iso)
    return _utc_now(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_appid(raw: Any) -> str:
    """Validate Steam appid as 1..10 ASCII digits."""
    text = str(raw or "").strip()
    if not _APPID_RE.match(text):
        raise DealsInvalidAppId("Steam AppID must be 1–10 digits.")
    return text


def normalize_cc(raw: Any) -> str:
    text = str(raw or DEFAULT_STORE_CC).strip().lower()
    return text if _CC_RE.match(text) else DEFAULT_STORE_CC


def clamp_poll_hours(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POLL_HOURS
    return max(MIN_POLL_HOURS, min(MAX_POLL_HOURS, val))


def clamp_min_drop_pct(raw: Any) -> int:
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_DROP_PCT
    return max(1, min(90, val))


def validate_allowlisted_url(url: str) -> str:
    """Enforce HTTPS to ``store.steampowered.com`` without userinfo or alternate hosts."""
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != ALLOWED_HOST or parsed.username or parsed.password:
        raise DealsHostBlocked(f"Outbound Deal Radar host not allowlisted: {url}")
    return url


class _AllowlistedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect leaving ``https://store.steampowered.com``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_allowlisted_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _steam_get(url: str, *, opener=None) -> Any:
    """Bounded GET restricted strictly to ``https://store.steampowered.com``."""
    validate_allowlisted_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"OpenBox/{VERSION}",
            "Accept": "application/json",
        },
        method="GET",
    )
    active_opener = opener if opener is not None else urllib.request.build_opener(_AllowlistedRedirectHandler())
    with active_opener.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        raw = read_limited(resp, MAX_RESPONSE_BYTES)
    return json.loads(raw.decode("utf-8"))


def _deals_block(state: dict[str, Any]) -> dict[str, Any]:
    deals = state.setdefault("deals", {"watchlist": [], "prices": {}, "last_poll_at": ""})
    if not isinstance(deals, dict):
        deals = {"watchlist": [], "prices": {}, "last_poll_at": ""}
        state["deals"] = deals
    if not isinstance(deals.get("watchlist"), list):
        deals["watchlist"] = []
    if not isinstance(deals.get("prices"), dict):
        deals["prices"] = {}
    if not isinstance(deals.get("last_poll_at"), str):
        deals["last_poll_at"] = ""
    return deals


def search_steam(query: str, *, cc: str = DEFAULT_STORE_CC, opener=None) -> list[dict[str, Any]]:
    """Query Steam storesearch for up to 10 normalized storefront candidates."""
    q = " ".join(str(query or "").strip().split())
    if not q or len(q) > 100:
        raise BadRequest("Search query must be 1–100 characters.")
    store_cc = normalize_cc(cc)
    url = f"https://{ALLOWED_HOST}/api/storesearch/?term={quote_plus(q)}&cc={store_cc}&l=en"
    payload = _steam_get(url, opener=opener)
    items = payload.get("items") if isinstance(payload, dict) else []
    results: list[dict[str, Any]] = []
    for item in (items if isinstance(items, list) else [])[:10]:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            appid = validate_appid(raw_id)
        except DealsInvalidAppId:
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        final_cents = price.get("final")
        initial_cents = price.get("initial", final_cents)
        currency = str(price.get("currency") or store_cc.upper()).strip().upper()[:3]
        discount_pct = 0
        if isinstance(initial_cents, int) and isinstance(final_cents, int) and initial_cents > 0 and final_cents < initial_cents:
            discount_pct = round((initial_cents - final_cents) * 100 / initial_cents)
        results.append({
            "appid": appid,
            "name": str(item.get("name") or appid)[:MAX_NAME_LEN],
            "artwork_url": str(item.get("tiny_image") or "")[:400],
            "currency": currency if _CURRENCY_RE.match(currency) else "",
            "initial_cents": initial_cents if isinstance(initial_cents, int) else None,
            "final_cents": final_cents if isinstance(final_cents, int) else None,
            "discount_pct": discount_pct,
            "free": final_cents == 0,
        })
    return results


def _normalize_appdetails_payload(payload: Any, appid: str, cc: str) -> dict[str, Any]:
    entry = payload.get(appid) if isinstance(payload, dict) else None
    if not isinstance(entry, dict) or not entry.get("success"):
        return {
            "status": "not_found",
            "appid": appid,
            "cc": cc,
            "name": "",
            "artwork_url": "",
            "currency": "",
            "price_cents": None,
            "initial_cents": None,
            "discount_pct": 0,
            "free": False,
        }
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    name = str(data.get("name") or "")[:MAX_NAME_LEN]
    artwork_url = str(data.get("header_image") or "")[:400]
    if data.get("is_free"):
        return {
            "status": "free",
            "appid": appid,
            "cc": cc,
            "name": name,
            "artwork_url": artwork_url,
            "currency": "",
            "price_cents": 0,
            "initial_cents": 0,
            "discount_pct": 0,
            "free": True,
        }
    po = data.get("price_overview") if isinstance(data.get("price_overview"), dict) else None
    if po is None:
        return {
            "status": "no_price",
            "appid": appid,
            "cc": cc,
            "name": name,
            "artwork_url": artwork_url,
            "currency": "",
            "price_cents": None,
            "initial_cents": None,
            "discount_pct": 0,
            "free": False,
        }
    final_cents = int(po.get("final") or 0)
    initial_cents = int(po.get("initial") or final_cents)
    discount_pct = max(0, min(100, int(po.get("discount_percent") or 0)))
    currency = str(po.get("currency") or "").strip().upper()[:3]
    return {
        "status": "priced",
        "appid": appid,
        "cc": cc,
        "name": name,
        "artwork_url": artwork_url,
        "currency": currency,
        "price_cents": final_cents,
        "initial_cents": initial_cents,
        "discount_pct": discount_pct,
        "free": final_cents == 0,
    }


def fetch_steam_appdetails(
    data_dir: Path | str,
    appid: str,
    *,
    cc: str = DEFAULT_STORE_CC,
    ttl_seconds: int = DEFAULT_POLL_HOURS * 3600,
    force: bool = False,
    now: datetime | None = None,
    opener=None,
) -> dict[str, Any]:
    """Fetch or load cached normalized Steam appdetails."""
    aid = validate_appid(appid)
    store_cc = normalize_cc(cc)
    base = Path(data_dir)
    cache_path = base / "cache" / "deals" / f"{aid}_{store_cc}.json"
    now_ts = _utc_now(now).timestamp()

    if not force and cache_path.is_file():
        try:
            age = now_ts - cache_path.stat().st_mtime
            if age < ttl_seconds:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("appid") == aid:
                    return cached
        except (OSError, ValueError):
            pass

    query = urlencode({
        "appids": aid,
        "cc": store_cc,
        "filters": "name,price_overview,header_image",
    })
    url = f"https://{ALLOWED_HOST}/api/appdetails?{query}"
    payload = _steam_get(url, opener=opener)
    normalized = _normalize_appdetails_payload(payload, aid, store_cc)
    cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(cache_path, json.dumps(normalized, ensure_ascii=False), mode=0o600)
    return normalized


def _normalize_target_cents(raw: Any) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise BadRequest("target_price_cents must be a non-negative integer or null.")
    try:
        val = int(raw)
    except (TypeError, ValueError) as error:
        raise BadRequest("target_price_cents must be a non-negative integer.") from error
    if val < 0 or val > 1_000_000_00:
        raise BadRequest("target_price_cents is out of range.")
    return val


def watch_game(state: dict[str, Any], payload: dict[str, Any], *, now_iso: str | None = None) -> dict[str, Any]:
    """Add or update a Steam or manual Deal Radar watchlist entry."""
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    deals = _deals_block(state)
    watchlist: list[dict[str, Any]] = deals["watchlist"]
    prices: dict[str, list[dict[str, Any]]] = deals["prices"]

    manual = bool(payload.get("manual")) or (payload.get("appid") is None and str(payload.get("store") or "") == "manual")
    appid: str | None = None
    store = str(payload.get("store") or ("manual" if manual else "steam")).strip()[:40] or "steam"
    if not manual:
        appid = validate_appid(payload.get("appid"))
        store = "steam"

    target = _normalize_target_cents(payload.get("target_price_cents"))
    note = str(payload.get("note") or "").strip()[:MAX_NOTE_LEN]
    name = str(payload.get("name") or (f"Steam {appid}" if appid else "Watch item")).strip()[:MAX_NAME_LEN]
    if not name:
        raise BadRequest("Watch entry name is required.")
    artwork_url = str(payload.get("artwork_url") or "").strip()[:400]

    # Deduplicate existing same Steam appid entry
    if appid is not None:
        existing = next((row for row in watchlist if row.get("appid") == appid), None)
        if existing is not None:
            if payload.get("target_price_cents") is not None or "target_price_cents" in payload:
                existing["target_price_cents"] = target
            if note:
                existing["note"] = note
            return existing

    if len(watchlist) >= MAX_WATCHLIST:
        raise DealsLimitExceeded(f"Deal Radar watchlist is capped at {MAX_WATCHLIST} entries.")

    item_id = f"deal-{secrets.token_hex(8)}"
    record = {
        "id": item_id,
        "name": name,
        "store": store,
        "appid": appid,
        "manual": manual,
        "artwork_url": artwork_url,
        "target_price_cents": target,
        "note": note,
        "added_at": _utc_iso(now_iso=now_iso),
        "last_price_cents": None,
        "last_discount_pct": 0,
        "last_currency": "",
        "last_seen_at": "",
        "last_notified_price_cents": None,
        "last_notified_at": "",
        "fetch_error": "",
    }
    watchlist.append(record)
    prices.setdefault(item_id, [])
    return record


def unwatch_game(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    """Remove both the watchlist entry and its ``prices[id]`` history."""
    did = str(item_id or "").strip()
    deals = _deals_block(state)
    before = len(deals["watchlist"])
    deals["watchlist"] = [row for row in deals["watchlist"] if row.get("id") != did]
    if len(deals["watchlist"]) == before:
        raise DealsNotFound(f"Deal watchlist item not found: {did}")
    deals["prices"].pop(did, None)
    return {"deleted": True, "id": did}


def _append_price_point(
    deals: dict[str, Any],
    item_id: str,
    *,
    price_cents: int,
    currency: str,
    discount_pct: int,
    source: str,
    now_iso: str,
) -> list[dict[str, Any]]:
    hist = deals["prices"].setdefault(item_id, [])
    if not isinstance(hist, list):
        hist = []
        deals["prices"][item_id] = hist
    hist.append({
        "at": now_iso,
        "price_cents": int(price_cents),
        "currency": str(currency or "").strip().upper()[:3],
        "discount_pct": max(0, min(100, int(discount_pct))),
        "source": source,
    })
    if len(hist) > MAX_HISTORY_POINTS:
        del hist[: len(hist) - MAX_HISTORY_POINTS]
    return hist


def record_manual_entry(state: dict[str, Any], payload: dict[str, Any], *, now_iso: str | None = None) -> dict[str, Any]:
    """Update target/note or log a manual price observation on a watchlist item."""
    if not isinstance(payload, dict):
        raise BadRequest("Request body must be an object.")
    did = str(payload.get("id") or "").strip()
    deals = _deals_block(state)
    entry = next((row for row in deals["watchlist"] if row.get("id") == did), None)
    if entry is None:
        raise DealsNotFound(f"Deal watchlist item not found: {did}")

    if "target_price_cents" in payload:
        entry["target_price_cents"] = _normalize_target_cents(payload.get("target_price_cents"))
    if "note" in payload:
        entry["note"] = str(payload.get("note") or "").strip()[:MAX_NOTE_LEN]

    stamp = _utc_iso(now_iso=now_iso)
    if "price_cents" in payload and payload.get("price_cents") is not None:
        price_val = _normalize_target_cents(payload.get("price_cents"))
        if price_val is not None:
            currency = str(payload.get("currency") or entry.get("last_currency") or "").strip().upper()[:3]
            if currency and not _CURRENCY_RE.match(currency):
                raise BadRequest("currency must be a 3-letter ISO code.")
            try:
                discount_pct = max(0, min(100, int(payload.get("discount_pct") or 0)))
            except (TypeError, ValueError) as error:
                raise BadRequest("discount_pct must be an integer.") from error
            _append_price_point(
                deals,
                did,
                price_cents=price_val,
                currency=currency,
                discount_pct=discount_pct,
                source="manual",
                now_iso=stamp,
            )
            entry["last_price_cents"] = price_val
            entry["last_currency"] = currency
            entry["last_discount_pct"] = discount_pct
            entry["last_seen_at"] = stamp
            entry["fetch_error"] = ""
    return entry


def _format_price(price_cents: int, currency: str) -> str:
    cur = currency or "USD"
    return f"{cur} {price_cents / 100:.2f}"


def apply_poll_observation(
    state: dict[str, Any],
    item_id: str,
    obs: dict[str, Any],
    settings: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Commit one fetched storefront observation with silent seeding & durable dedupe."""
    deals = _deals_block(state)
    entry = next((row for row in deals["watchlist"] if row.get("id") == item_id), None)
    if entry is None:
        return {"updated": False, "notified": False}
    stamp = _utc_iso(now)

    if obs.get("name") and (not entry.get("name") or entry["name"].startswith("Steam ")):
        entry["name"] = str(obs["name"])[:MAX_NAME_LEN]
    if obs.get("artwork_url") and not entry.get("artwork_url"):
        entry["artwork_url"] = str(obs["artwork_url"])[:400]

    status = obs.get("status")
    if status == "not_found":
        entry["fetch_error"] = "not_found"
        entry["last_seen_at"] = stamp
        return {"updated": True, "notified": False}
    if status == "no_price" or obs.get("price_cents") is None:
        entry["fetch_error"] = ""
        entry["last_seen_at"] = stamp
        return {"updated": True, "notified": False}

    new_price = int(obs["price_cents"])
    currency = str(obs.get("currency") or "").strip().upper()[:3]
    discount_pct = int(obs.get("discount_pct") or 0)
    prev_price = entry.get("last_price_cents")

    _append_price_point(
        deals,
        item_id,
        price_cents=new_price,
        currency=currency,
        discount_pct=discount_pct,
        source="steam",
        now_iso=stamp,
    )
    entry["last_price_cents"] = new_price
    entry["last_discount_pct"] = discount_pct
    entry["last_currency"] = currency
    entry["last_seen_at"] = stamp
    entry["fetch_error"] = ""

    # First poll seeds silently; subsequent polls check drop / target + durable marker.
    notified = False
    if prev_price is not None:
        min_drop = clamp_min_drop_pct(settings.get("deals_min_drop_pct", DEFAULT_MIN_DROP_PCT))
        target = entry.get("target_price_cents")
        drop_hit = discount_pct >= min_drop and new_price < int(prev_price)
        target_hit = target is not None and new_price <= int(target)
        if (drop_hit or target_hit) and entry.get("last_notified_price_cents") != new_price:
            entry["last_notified_price_cents"] = new_price
            entry["last_notified_at"] = stamp
            price_label = _format_price(new_price, currency)
            add_notification(
                state,
                kind="deal",
                level="info",
                title=f"Price alert: {entry['name']}",
                body=f"{entry['name']} is now {price_label} (-{discount_pct}%).",
                source="deals",
                dedupe_key=f"deal:{item_id}:{new_price}",
                title_key="notify.server.deal_drop_title",
                body_key="notify.server.deal_drop_body",
                params={"name": entry["name"], "price": price_label, "discount": discount_pct},
                now=stamp,
            )
            notified = True

    return {"updated": True, "notified": notified}


def list_deals(state: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only projection joining watchlist items with library paid prices & history stats."""
    deals = state.get("deals") if isinstance(state.get("deals"), dict) else {}
    watchlist = [dict(row) for row in (deals.get("watchlist") or []) if isinstance(row, dict)]
    prices = deals.get("prices") if isinstance(deals.get("prices"), dict) else {}
    games_by_appid: dict[str, dict[str, Any]] = {}
    for g in state.get("games") or []:
        if isinstance(g, dict) and g.get("steam_app_id") is not None:
            games_by_appid[str(g["steam_app_id"]).strip()] = g

    now_dt = _utc_now(now)
    enriched: list[dict[str, Any]] = []
    for item in watchlist:
        did = str(item.get("id") or "")
        hist = [dict(p) for p in (prices.get(did) or []) if isinstance(p, dict)]
        price_vals = [int(p["price_cents"]) for p in hist if isinstance(p.get("price_cents"), int)]
        lowest_retained = min(price_vals) if price_vals else None

        age_hours: int | None = None
        seen_raw = str(item.get("last_seen_at") or "").strip()
        if seen_raw:
            parsed_seen = parse_timestamp(seen_raw)
            if parsed_seen is not None:
                seen_dt = parsed_seen.astimezone(timezone.utc) if seen_raw.endswith(("Z", "z")) or "+" in seen_raw[10:] else parsed_seen.replace(tzinfo=timezone.utc)
                age_hours = max(0, int((now_dt - seen_dt).total_seconds() // 3600))
            else:
                age_hours = None

        linked_game = games_by_appid.get(str(item.get("appid") or "")) if item.get("appid") else None
        paid_cents = linked_game.get("price_paid_cents") if isinstance(linked_game, dict) else None
        paid_currency = str(linked_game.get("price_currency") or "").strip().upper() if isinstance(linked_game, dict) else ""
        paid_delta_cents: int | None = None
        if (
            isinstance(paid_cents, int)
            and isinstance(item.get("last_price_cents"), int)
            and paid_currency
            and paid_currency == str(item.get("last_currency") or "").upper()
        ):
            paid_delta_cents = int(item["last_price_cents"]) - paid_cents

        enriched.append({
            **item,
            "history": hist,
            "lowest_retained_cents": lowest_retained,
            "price_age_hours": age_hours,
            "library_game_id": str(linked_game.get("game_id") or "") if linked_game else "",
            "library_paid_cents": paid_cents if isinstance(paid_cents, int) else None,
            "library_paid_currency": paid_currency,
            "paid_delta_cents": paid_delta_cents,
        })

    return {
        "available": True,
        "watchlist": enriched,
        "last_poll_at": str(deals.get("last_poll_at") or ""),
    }


def deals_due(state: dict[str, Any], settings: dict[str, Any], *, now: datetime | None = None) -> bool:
    cfg = settings if isinstance(settings, dict) else {}
    if not cfg.get("deals_enabled", False):
        return False
    deals = state.get("deals") if isinstance(state.get("deals"), dict) else {}
    watchlist = [r for r in (deals.get("watchlist") or []) if isinstance(r, dict) and not r.get("manual") and r.get("appid")]
    if not watchlist:
        return False
    poll_seconds = clamp_poll_hours(cfg.get("deals_poll_hours", DEFAULT_POLL_HOURS)) * 3600
    raw_last = str(deals.get("last_poll_at") or "").strip()
    if not raw_last:
        return True
    parsed_last = parse_timestamp(raw_last)
    if parsed_last is None:
        return True
    last_dt = parsed_last.astimezone(timezone.utc) if raw_last.endswith(("Z", "z")) or "+" in raw_last[10:] else parsed_last.replace(tzinfo=timezone.utc)
    return (_utc_now(now) - last_dt).total_seconds() >= poll_seconds


def refresh_deals_pass(
    data_dir: Path | str,
    state_snapshot: dict[str, Any],
    settings: dict[str, Any],
    *,
    commit_fn,
    cancel_event=None,
    progress_cb=None,
    spacing_seconds: float = DEFAULT_SPACING_SECONDS,
    opener=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Poll non-manual Steam watchlist entries sequentially with per-entry isolation."""
    cfg = settings if isinstance(settings, dict) else {}
    if not cfg.get("deals_enabled", False):
        return {"polled": 0, "notified": 0, "errors": []}

    deals = state_snapshot.get("deals") if isinstance(state_snapshot.get("deals"), dict) else {}
    entries = [
        dict(row) for row in (deals.get("watchlist") or [])
        if isinstance(row, dict) and not row.get("manual") and row.get("appid")
    ]
    cc = normalize_cc(cfg.get("deals_store_cc", DEFAULT_STORE_CC))
    ttl = clamp_poll_hours(cfg.get("deals_poll_hours", DEFAULT_POLL_HOURS)) * 3600
    polled = 0
    notified = 0
    errors: list[str] = []
    total = len(entries)
    cancelled = False

    for idx, item in enumerate(entries, start=1):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        did = str(item.get("id") or "")
        appid = str(item.get("appid") or "")
        try:
            obs = fetch_steam_appdetails(
                data_dir,
                appid,
                cc=cc,
                ttl_seconds=ttl,
                force=True,
                now=now,
                opener=opener,
            )

            def _apply(current, target_id=did, observation=obs):
                return apply_poll_observation(current, target_id, observation, current.get("settings", cfg), now=now)

            outcome = commit_fn(_apply)
            polled += 1
            if isinstance(outcome, dict) and outcome.get("notified"):
                notified += 1
        except Exception as error:
            err_msg = str(error)[:200]
            errors.append(f"{item.get('name', appid)}: {err_msg}")

            def _mark_err(current, target_id=did, msg=err_msg):
                blk = _deals_block(current)
                row = next((r for r in blk["watchlist"] if r.get("id") == target_id), None)
                if row is not None:
                    row["fetch_error"] = msg
                return False

            try:
                commit_fn(_mark_err)
            except Exception:
                pass
        if progress_cb is not None:
            progress_cb(idx, total)
        if idx < total and spacing_seconds > 0 and cancel_event is not None:
            if cancel_event.wait(spacing_seconds):
                cancelled = True
                break

    if not cancelled:
        stamp = _utc_iso(now)

        def _mark_poll(current):
            _deals_block(current)["last_poll_at"] = stamp
            return stamp

        commit_fn(_mark_poll)

    return {"polled": polled, "notified": notified, "errors": errors[:20], "cancelled": cancelled}


def deals_compat_section(state: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    cfg = settings if isinstance(settings, dict) else {}
    deals = state.get("deals") if isinstance(state, dict) and isinstance(state.get("deals"), dict) else {}
    watchlist = deals.get("watchlist") if isinstance(deals.get("watchlist"), list) else []
    return {
        "enabled": bool(cfg.get("deals_enabled", False)),
        "host_allowlist": [ALLOWED_HOST],
        "payload": "appid_and_search_query_only",
        "watchlist_count": len(watchlist),
    }


__all__ = [
    "ALLOWED_HOST",
    "DEFAULT_MIN_DROP_PCT",
    "DEFAULT_POLL_HOURS",
    "DEFAULT_STORE_CC",
    "MAX_HISTORY_POINTS",
    "MAX_NAME_LEN",
    "MAX_NOTE_LEN",
    "MAX_WATCHLIST",
    "apply_poll_observation",
    "clamp_min_drop_pct",
    "clamp_poll_hours",
    "deals_compat_section",
    "deals_due",
    "fetch_steam_appdetails",
    "list_deals",
    "normalize_cc",
    "record_manual_entry",
    "refresh_deals_pass",
    "search_steam",
    "unwatch_game",
    "validate_allowlisted_url",
    "validate_appid",
    "watch_game",
]
