# ADR 0116: 50,000-Game Scale Graduation on the Rust Backend

Status: Accepted (2026-11-19) — Supersedes ADR 0108's 20,000-game ceiling hold.

## Context

ADR 0098 established the single-machine graduation rule for OpenBox 3.0's canonical JSON store (`library.json` schema v7):

- **Write latency at 50,000 games:** favorite mutation and bulk mutation p95 **<= 1,500 ms**
- **Resident memory at 50,000 games:** server RSS after warm-up and full benchmark **<= 1,500 MB**

In ADR 0108 (2026-11-13), the CPython 3.15.2 backend met the 50k write-latency targets (1,174.2 ms favorite p95, 1,319.5 ms bulk p95) on Apple M4, but **missed the RSS budget at 2,163.3 MB**, holding the formal scale ceiling at 20,000 games.

ADR 0115 migrated all 170 runtime Python modules and all 417 HTTP endpoints (413 public `/api/v2/*` + 4 `/api/host/*`) into the standalone Rust `openbox-server` binary while keeping `library.json` schema v7 canonical and 100% byte-compatible (zero Strangler proxy).

## Decision

1. **Graduate the formal scale ceiling to 50,000 games** on the Rust `openbox-server` backend while keeping `library.json` schema v7 canonical and `sqlite_readmodel.db` (via bundled `rusqlite 0.43` with FTS5) as a derived read projection.
2. **Retain the 10k and 20k per-push CI blocking gates** (`10k_write_ms_p95 <= 500 ms`, `20k_write_ms_p95 <= 1,000 ms`, `picker_score_ms_p95 <= 200 ms`, `constellation_build_ms_p95 <= 1,500 ms`) and establish the 50,000-game release verification run (`--sizes 50000 --memory`).
3. **Preserve key Rust storage & projection invariants** that make 50k graduation sustainable:
   - Streaming full-library projection (`StreamingPublicState` / `StreamingGames`) directly to `bytes::Bytes` + gzip without retaining a materialized `Vec<serde_json::Value>` of 50k projected games in cache.
   - O(1) zero-copy `Arc<CachedPayload>` / `Bytes` cache hits for full `/api/v2/library`, slice-only projection for paginated `/api/v2/library?offset=&limit=`, and indexed O(k) projection for `/api/v2/library/delta`.
   - Zero-clone in-place `JsonStateStore` warm transactions (`Arc::try_unwrap`, in-place `normalize_committed_state_in_place`, zero-allocation streaming BLAKE2b `state_digest`, buffered `BufWriter` serialization without cloning `library.json` to strip `active_sessions`, and `transact_local_state` for non-catalog mutations matching Python's `local_only_mutation`).
   - Catalog-only delta diffing in `openbox-sync::time_machine` (`capture_transaction_snapshot` / `record_transaction_hook`) so single-game edits inspect only changed catalog fields.

## Verification

Single-machine release measurement on **2026-11-19**, branch `feat/rust-migration`:

- **Host:** Linux 7.3.0 x86_64, AMD Ryzen 5 4600H, 16 GiB RAM
- **Toolchain / Binary:** `rustc 1.99.1`, `target/release/openbox-server` (22 MB standalone binary, zero Strangler proxy, zero Python runtime)
- **Commands:**
  - `OPENBOX_SERVER_BIN=target/release/openbox-server python3 -B scripts/perf_bench.py --sizes 10000,20000 --runs 3 --memory --out build/perf-rust-gate.json`
  - `OPENBOX_SERVER_BIN=target/release/openbox-server python3 -B scripts/perf_bench.py --sizes 50000 --runs 3 --no-gate --memory --out build/perf-rust-50k.json`

### 50,000-Game Graduation Comparison vs ADR 0108

| Metric | ADR 0108 Budget | Python 3.0 (ADR 0108, M4) | Rust 3.0 (`openbox-server`, Ryzen 5 4600H) | Verdict |
|---|---:|---:|---:|---|
| Favorite mutation p95 | <= 1,500 ms | 1,174.2 ms | **545.4 ms** | Met (**2.75x under budget**, 2.15x faster than Python) |
| Bulk mutation p95 | <= 1,500 ms | 1,319.5 ms | **346.1 ms** | Met (**4.33x under budget**, 3.81x faster than Python) |
| Server RSS after warm-up | — | 1,190.0 MB | **582.2 MB** | 2.04x lower than Python |
| Server RSS after benchmark | <= 1,500 MB | 2,163.3 MB (missed) | **774.1 MB** | **Met (51.6% of budget, 2.79x lower than Python)** |
| Full `/api/v2/library` plain p95 | — | — | **110.1 ms** (171.7 MB) | ~1.56 GB/s loopback delivery |
| Full `/api/v2/library` gzip p95 | — | — | **12.4 ms** (18.5 MB) | Cached `Bytes` |
| Paginated `/api/v2/library?offset=0&limit=500` p95 | — | — | **135.2 ms** | Slice-only projection |
| `/api/v2/library/delta` search p95 | — | — | **0.5 ms** | O(1) indexed lookup |
| `/api/v2/explorer/facets?field=genre` p95 | — | — | **20.7 ms** | — |
| `/api/v2/library/constellation?limit=400` p95 | <= 1,500 ms | — | **320.2 ms** | Met (4.68x under budget) |

### 10,000- and 20,000-Game Blocking Gate Results

| Size | Write p95 (Gate) | Favorite p95 | Bulk p95 | Library gzip p95 | Picker p95 (Gate) | Constellation p95 (Gate) | End RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| **10,000** | **116.7 ms** (<= 500 ms) | 111.2 ms | 69.6 ms | 2.9 ms | **69.7 ms** (<= 200 ms) | **123.6 ms** (<= 1,500 ms) | **198.1 MB** |
| **20,000** | **224.5 ms** (<= 1,000 ms) | 220.7 ms | 141.4 ms | 5.4 ms | **154.3 ms** (<= 200 ms) | **168.7 ms** (<= 1,500 ms) | **356.6 MB** |

All 10k and 20k blocking gates and both 50k ADR-0098 graduation budgets passed on a single machine with wide margin.

## Consequences

- `library.json` schema v7 remains canonical and 100% downgrade-compatible with OpenBox 2.1.x and the Python fallback (`OPENBOX_BACKEND=python`).
- No storage-engine rewrite is needed to support 50,000-game libraries.
- Release bundles (Linux AppImage, macOS universal `.app`, Windows portable `.zip`) ship the standalone Rust `openbox-server` binary with zero bundled Python runtime (host `python3` is invoked strictly on demand for `.py` v2 plugins via `plugin_runner_v2.py`).
