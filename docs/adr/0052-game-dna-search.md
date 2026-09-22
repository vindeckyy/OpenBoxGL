# ADR 0052: Game DNA search (offline semantic search without embeddings)

**Date:** 2026-09-22
**Status:** Accepted

## Context

The 1.14.0 plan calls for "Game DNA search": describe a vibe ("cozy farming sim with
combat") or pick a game ("games like Hollow Knight") and get ranked matches with
explanations. The hard constraints are dependency-free runtime (no numpy, no
torch, no downloads) and honest privacy ("no AI cloud, no downloads" in the UI).
Neural embeddings were investigated and ruled infeasible under those constraints,
so the search must be classical IR only, and the v1 API surface stays frozen.

## Decision

- `pkg/parity/parity_dna.py` owns the whole engine: Unicode NFKC tokenizer
  (min length 2, 400-term cap per game), en/de/es/fr/pt stopwords with English
  fallback, weighted fields (name 3 → play mode 0.5), atomic sidecar index
  `<APP_DIR>/dna_index.json` (temp file + fsync + `os.replace`, honoring
  `OPENBOX_DATA_DIR`, excluded from cloud sync), BM25 (k1=1.2, b=0.75),
  a curated 151-concept English lexicon with de/es/fr/pt overlays (lexicon
  version 3), query-side expansion at 0.5 weight with explanation chips,
  trigram anchor resolution, and 1024-dimensional deterministic feature hashing
  blended 50/50 with BM25. Taste boost uses the exact F4 field names
  `progress` and `user_rating`.
- `parse_dna_query()` delegates to the authoritative
  `parity_query.parse_query()`. Genre and free-text predicates from the parse
  are soft: genre words (`rules["genre"]`, `genre_any` clause terms) are
  folded back into the BM25 ranking terms with a `genre: …` why-chip, so a
  natural query like "cozy farming adventure" ranks instead of filtering to
  zero results. Only the remaining structured predicates (platform,
  time-to-beat, …) hard-filter via `filter_games_by_query()`; when none
  remain the filter pass is skipped. Unresolvable anchors degrade to BM25
  with an explicit explanation chip.
- New routes live under `/api/v2` only (`handlers/discovery.py`):
  `POST /api/v2/library/dna/search`, `GET /api/v2/library/dna/status`,
  `POST /api/v2/library/dna/index/rebuild` (synchronous for small libraries,
  cancelable `dna-index-rebuild` job otherwise). Missing/building index
  degrades to title search with `degraded: true`; heavy drift or signature
  mismatch serves existing results degraded and schedules a rebuild.
- Library mutations hook DNA invalidation best-effort in `handlers/library.py`
  (save, bulk edit, tag edit, bulk wizard, delete paths, manual entries);
  derived-index failures never fail the primary mutation. Picker gains additive
  `seed_query`/`dna_boost` parameters (`handlers/picker.py`); defaults preserve
  existing behavior.
- Frontend (`static/dna.js`): Title | Smart toggle next to the sidebar search
  box, default Title, persisted in settings, 250 ms debounce, results panel
  with why-chips, honest building (with cancel) / degraded / no-description
  states, "More like this" in the details pane and card context menu, and a
  gamepad-operable toggle plus why-chip subtitles in Big Box (`static/bigbox.js`
  search-toggle region only, feeding the existing `#bigBoxHybridSearch` input).
  All new styles are token-only (`scripts/check_tokens.py` baseline stays 0);
  new tokens are added to `:root` in `static/app.css` and every theme.

## Consequences

- Search quality depends on description/notes coverage; the UI surfaces this
  honestly (empty-coverage state deep-links to Metadata → Fill missing).
- Index format 1 / lexicon version 3 are checked on load; mismatch triggers a
  rebuild rather than silent wrong results.
- Similarity on a 20k synthetic corpus is slower than BM25 (~326 ms warm in a
  pathological corpus vs ~116 ms BM25); the generous 1500 ms CI margin reflects
  real-world selectivity, not the synthetic worst case.
- `bm25_search()` scores via a transient in-memory inverted index
  (`_postings`, never persisted) instead of a full-corpus scan; per-doc term
  contributions are still summed in query order, so scores are bit-identical
  to the scan. Mutations drop the transient postings for lazy rebuild.
