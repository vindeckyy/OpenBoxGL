# Performance baseline — OpenBox v0.7.0 (pre-optimization)

Date: 2026-08-02
Harness: `scripts/perf_gen_library.py` (synthetic libraries) + `scripts/perf_bench.py`
(raw HTTP against a real server, median of 3, warm cache).
Machine: Linux, Python 3.12.

## Server-side results

| Games | `/api/library` ms | payload bytes | `/api/media` ms | favorite mutation ms |
|---|---|---|---|---|
| 100   | 8.0   | 272,075    | 4.6   | 21.2   |
| 1,000 | 74.8  | 2,694,233  | 30.0  | 212.2  |
| 5,000 | 2659.4 | 13,455,564 | 186.5 | 1299.0 |

## Diagnosis (confirmed root causes)

1. `public_state()` recomputes everything per request: full-state `copy.deepcopy`,
   up to ~14 `stat()` probes per game (`FILE_PROBE_TTL` = 1s), a save-directory
   scan (`games_with_saves`, 2s TTL), and 4+ full-library sorts (`discovery_lists`).
2. The `/api/library` payload carries every field for every game (descriptions,
   notes, screenshot paths, applications) — 13.4MB at 5k games, re-serialized per request.
3. `load_state()` deep-copies the entire library on every call, including each
   `/api/media` request (186ms at 5k games per image).
4. Every mutation (favorite toggle) rewrites the whole library file: normalize +
   `json.dump(indent=2)` + two full backup copies + two `fsync`s — 1.3s at 5k games.
5. `send_file` sends no cache headers; browsers re-fetch every cover on every render.

## Targets (after optimization)

- `/api/library` @ 5,000 games: < 300 ms
- favorite mutation @ 5,000 games: < 150 ms
- `/api/media` @ 5,000 games: < 10 ms (and 0 network re-fetches on re-render)
- Keystroke grid re-render @ 5,000 games (browser): < 200 ms
