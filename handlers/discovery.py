"""DiscoveryHandlers — Game DNA smart search (Flagship 9).

Three /api/v2 routes plus the DNA orchestration used by the picker
integration. The index itself lives in pkg/parity/parity_dna.py; this
module owns the HTTP surface, freshness policy, and background rebuilds.

Freshness policy (documented, no lifecycle events exist yet):
- Library mutations hook the concrete routes in handlers/library.py, which
  call parity_dna.note_game_upserted/note_game_removed incrementally.
- Every search/status call runs a cheap reconciliation pass (per-game
  corpus hashes; tokenization only for drifted games) and persists the
  state signature when it changed.
- Signature mismatch with heavy drift (>10% of games) → serve the stale
  index with degraded:true and kick off a background rebuild job.
- Metadata refreshes (handlers/metadata.py) are not hooked; their drift
  is corrected by the same reconciliation pass.
"""

from __future__ import annotations

import time

from api_errors import BadRequest
from openbox import load_state_readonly
from pkg.parity import parity_dna
from routes.registry import route
from webapp_state import JOB_MANAGER

# Sync-build threshold: small libraries build inline; large ones rebuild as
# a background job so the request never blocks for seconds.
SYNC_BUILD_THRESHOLD = 5000
# Drift ratio above which a signature mismatch triggers a background
# rebuild instead of trusting the incremental reconciliation.
HEAVY_DRIFT_RATIO = 0.10

_last_build_ms = 0.0


def _locale_for(state) -> str:
    settings = state.get("settings") or {}
    return str(settings.get("locale") or "en")[:5]


def _state_signature():
    try:
        from openbox import STATE_STORE

        return STATE_STORE.signature()
    except Exception:
        return None


def reconcile_index(index, games, locale, signature):
    """Cheap freshness pass: per-game corpus hashes, tokenize only drift.

    Returns (index, changed_count). Never raises — DNA is derived data.
    """
    changed = 0
    try:
        by_id = {}
        for game in games or []:
            if isinstance(game, dict):
                by_id[parity_dna.doc_id_for(game)] = game
        live_ids = set(by_id)
        stored_ids = set((index.get("games") or {}).keys())
        for doc_id in stored_ids - live_ids:
            parity_dna.note_game_removed(index, doc_id)
            changed += 1
        for doc_id, game in by_id.items():
            entry = (index.get("games") or {}).get(doc_id)
            if entry is None or entry.get("h") != parity_dna.corpus_hash(game):
                parity_dna.note_game_upserted(index, game, locale)
                changed += 1
        index["state_signature"] = None if signature is None else list(signature)
    except Exception:
        pass
    return index, changed


def _submit_rebuild_job(reason="manual"):
    def worker(cancel_event=None):
        global _last_build_ms
        started = time.perf_counter()
        state = load_state_readonly()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        locale = _locale_for(state)
        signature = _state_signature()
        total = len(games)
        # Build in chunks so progress + cancel stay responsive.
        merged: dict = {}
        df: dict = {}
        chunk = 500
        for start in range(0, total, chunk):
            if cancel_event is not None and cancel_event.is_cancelled():
                return {"cancelled": True, "processed": start}
            partial = parity_dna.rebuild_index(games[start : start + chunk], locale)
            for doc_id, entry in (partial.get("games") or {}).items():
                merged[doc_id] = entry
            for term, count in (partial.get("df") or {}).items():
                df[term] = df.get(term, 0) + count
            if cancel_event is not None:
                cancel_event.progress(processed=min(start + chunk, total), total=total)
        count = len(merged)
        index = {
            "format": parity_dna.INDEX_FORMAT,
            "lexicon_version": parity_dna.LEXICON_VERSION,
            "state_signature": None if signature is None else list(signature),
            "doc_count": count,
            "avg_len": (sum(e.get("l", 0.0) for e in merged.values()) / count) if count else 1.0,
            "df": df,
            "games": merged,
        }
        parity_dna.save_index_atomic(index)
        _last_build_ms = (time.perf_counter() - started) * 1000.0
        return {"indexed": count, "build_ms": round(_last_build_ms, 1), "reason": reason}

    return JOB_MANAGER.submit("dna-index-rebuild", worker, replace=True)


def dna_index_for_search(state):
    """Return (index, degraded, building) per the freshness policy."""
    games = [g for g in state.get("games", []) if isinstance(g, dict)]
    locale = _locale_for(state)
    signature = _state_signature()
    index = parity_dna.load_index()
    if index is None:
        if len(games) >= SYNC_BUILD_THRESHOLD:
            _submit_rebuild_job(reason="missing-index")
            return None, True, True
        started = time.perf_counter()
        index = parity_dna.rebuild_index(games, locale, signature)
        # This build shares the index file with the background rebuild job and
        # with any other in-flight request, so its write can lose that race.
        # The freshly built index is already returned to the caller below, so
        # failing to persist it is not a request failure -- the next search
        # reconciles and writes it.
        try:
            parity_dna.save_index_atomic(index)
        except OSError:
            pass
        global _last_build_ms
        _last_build_ms = (time.perf_counter() - started) * 1000.0
        return index, False, False
    stale = not parity_dna.signature_matches(index, signature)
    index, changed = reconcile_index(index, games, locale, signature)
    if changed:
        try:
            parity_dna.save_index_atomic(index)
        except OSError:
            pass
    drift = changed / max(1, len(games))
    if stale and drift > HEAVY_DRIFT_RATIO:
        # Import/restore style change: serve stale, rebuild in background.
        _submit_rebuild_job(reason="signature-mismatch")
        return index, True, False
    return index, False, False


def _title_fallback(games, query, limit):
    """Today's title search, used when the DNA index is unavailable."""
    query_lower = query.casefold()
    results = []
    for game in games:
        if query_lower in str(game.get("name", "")).casefold():
            results.append(
                {
                    "game_id": parity_dna.doc_id_for(game),
                    "score": 1.0,
                    "why": ["title match"],
                }
            )
            if len(results) >= limit:
                break
    return results


def _apply_filters(results, games, filters):
    if not filters or not isinstance(filters, dict):
        return results
    by_id = {parity_dna.doc_id_for(g): g for g in games if isinstance(g, dict)}
    kept = []
    for row in results:
        game = by_id.get(row["game_id"])
        if game is None:
            continue
        ok = True
        for field, wanted in filters.items():
            if field not in ("genre", "platform", "play_mode", "developer", "publisher"):
                continue
            values = wanted if isinstance(wanted, list) else [wanted]
            hay = str(game.get(field, "")).casefold()
            if not any(str(value).casefold() in hay for value in values):
                ok = False
                break
        if ok:
            kept.append(row)
    return kept


class DiscoveryHandlers:
    @route("POST", "/api/v2/library/dna/search")
    def _api_post_api_v2_library_dna_search(self, payload):
        """Smart search over the library: BM25 + concept lexicon + similarity.

        Body: {query: str, limit?: int (1..100, default 20), filters?: {...}}.
        Response: {results: [{game_id, name, score, why: [chips]}], parse, branch,
        anchor?, degraded}. degraded:true means the title fallback served
        the request (index missing/building) or the index is stale.
        """
        body = payload
        if not isinstance(body, dict):
            raise BadRequest("body must be an object")
        query = str(body.get("query", "") or "")
        if not query.strip():
            raise BadRequest("query is required")
        try:
            limit = int(body.get("limit", 20))
        except (TypeError, ValueError) as error:
            raise BadRequest("limit must be an integer") from error
        limit = max(1, min(limit, 100))
        filters = body.get("filters")

        state = load_state_readonly()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        locale = _locale_for(state)
        index, degraded, building = dna_index_for_search(state)
        if index is None:
            self.send_json(
                200,
                {
                    "results": _title_fallback(games, query.strip(), limit),
                    "parse": {"parsed": False},
                    "branch": "title-fallback",
                    "anchor": None,
                    "degraded": True,
                    "building": building,
                },
            )
            return
        outcome = parity_dna.parse_dna_query(query, games, index, locale, limit=limit)
        outcome["results"] = _apply_filters(outcome["results"], games, filters)
        outcome["degraded"] = degraded
        self.send_json(200, outcome)
        return

    @route("GET", "/api/v2/library/dna/status")
    def _api_get_api_v2_library_dna_status(self, parsed):
        """Index health: {indexed, total, coverage_pct, index_version,
        lexicon_version, build_ms, state}."""
        state = load_state_readonly()
        games = [g for g in state.get("games", []) if isinstance(g, dict)]
        locale = _locale_for(state)
        signature = _state_signature()
        index = parity_dna.load_index()
        building = False
        if index is None:
            if len(games) >= SYNC_BUILD_THRESHOLD:
                _submit_rebuild_job(reason="missing-index")
                building = True
            state_name = "building" if building else "missing"
            indexed = 0
        else:
            stale = not parity_dna.signature_matches(index, signature)
            index, changed = reconcile_index(index, games, locale, signature)
            if changed:
                try:
                    parity_dna.save_index_atomic(index)
                except OSError:
                    pass
            indexed = int(index.get("doc_count") or 0)
            state_name = "stale" if stale else "ready"
        total = len(games)
        with_description = sum(1 for g in games if str(g.get("description") or "").strip())
        self.send_json(
            200,
            {
                "indexed": indexed,
                "total": total,
                "coverage_pct": round(100.0 * with_description / max(1, total), 1),
                "index_version": parity_dna.INDEX_FORMAT,
                "lexicon_version": parity_dna.LEXICON_VERSION,
                "build_ms": round(_last_build_ms, 1),
                "state": state_name,
            },
        )
        return

    @route("POST", "/api/v2/library/dna/index/rebuild")
    def _api_post_api_v2_library_dna_index_rebuild(self, payload):
        """Full index rebuild as a background job (progress + cancel via
        the jobs routes)."""
        job = _submit_rebuild_job(reason="manual")
        self.send_json(
            202,
            {
                "state": job.get("state", "queued"),
                "job_id": job.get("job_id", ""),
            },
        )
        return
