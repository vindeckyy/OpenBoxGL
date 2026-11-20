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
   - Streaming full-library projection (`StreamingPublicState` / `StreamingGames`) directly to `bytes::Bytes` + level-8 gzip without retaining a materialized `Vec<serde_json::Value>` of 50k projected games in cache.
   - O(1) zero-copy `Arc<CachedPayload>` / `Bytes` cache hits for full `/api/v2/library`, bounded LRU `page_cache` + cached `source_labels` for paginated `/api/v2/library?offset=&limit=`, 250k-entry zero-allocation media probe cache (`MediaProbeService`), and indexed O(k) projection for `/api/v2/library/delta`.
   - Direct HTTP/1.1 connection loop (`hyper::server::conn::http1::Builder::writev(true)` + `TCP_NODELAY` + `tokio::task::coop::unconstrained`), zero-clone 2xx serialization in `middleware.rs`, zero-clone slice/ref `picker_score` with top-12 bounded selection, O(N) `select_nth_unstable_by` + interned-feature typed `ConstellationGraph` serialization, and bounded top-K `BinaryHeap` milestone collectors in `playerlog.rs`.
   - Zero-clone in-place `JsonStateStore` warm transactions (`Arc::try_unwrap`, in-place `normalize_committed_state_in_place`, zero-allocation streaming BLAKE2b `state_digest`, buffered `BufWriter` serialization without cloning `library.json` to strip `active_sessions`, and `transact_local_state` for non-catalog mutations matching Python's `local_only_mutation`).
   - Catalog-only delta diffing in `openbox-sync::time_machine` (`capture_transaction_snapshot` / `record_transaction_hook`) so single-game edits inspect only changed catalog fields.

## Verification

Single-machine head-to-head release measurement on **2026-11-19**, branch `feat/rust-migration`:

- **Host:** Linux 7.3.0 x86_64, AMD Ryzen 5 4600H, 16 GiB RAM
- **Toolchain / Binary:** `rustc 1.99.1`, `target/release/openbox-server` (standalone binary, zero Strangler proxy, zero Python runtime) vs same-machine CPython 3.14.8 (`web_app.py`)
- **Command:** `OPENBOX_SERVER_BIN=target/release/openbox-server python3 -B scripts/perf_bench.py --sizes 10000,20000,50000 --runs 3 --no-gate --memory`

### 50,000-Game Graduation Comparison (Same-Machine Python 3.14.8 vs Rust 3.0)

| Metric (p95) | ADR 0108 Budget | Python 3.0 (ADR 0108, M4) | Python 3.0 (Same Machine) | Rust 3.0 (`openbox-server`) | Verdict |
|---|---:|---:|---:|---:|---|
| Favorite mutation p95 | <= 1,500 ms | 1,174.2 ms | 2,950.6 ms | **560.4 ms** | Met (**2.68x under budget**, **5.27x faster** than Python) |
| Bulk mutation p95 | <= 1,500 ms | 1,319.5 ms | 2,903.3 ms | **345.9 ms** | Met (**4.34x under budget**, **8.39x faster** than Python) |
| Server RSS after warm-up | — | 1,190.0 MB | 1,094.8 MB | **633.8 MB** | 1.73x lower than Python |
| Server RSS after benchmark | <= 1,500 MB | 2,163.3 MB (missed) | 1,281.9 MB | **468.8 MB** | **Met (31.3% of budget, 2.73x lower than Python)** |
| Full `/api/v2/library` plain p95 | — | — | 111.9 ms (190.3 MB) | **99.3 ms** (172.9 MB) | **1.13x faster** than Python |
| Full `/api/v2/library` gzip p95 | — | — | 5.9 ms (7.67 MB) | **4.8 ms** (7.46 MB) | **1.23x faster** than Python |
| Paginated `/api/v2/library?offset=0&limit=500` p95 | — | — | 12.4 ms | **1.5 ms** | **8.27x faster** than Python |
| `/api/v2/library/delta` search p95 | — | — | 41.3 ms | **0.5 ms** | **82.6x faster** than Python |
| `/api/v2/explorer/facets?field=genre` p95 | — | — | 2,998.0 ms | **21.1 ms** | **142.1x faster** than Python |
| `POST /api/v2/library/pick` p95 | — | — | 544.1 ms | **64.8 ms** | **8.40x faster** than Python |
| `/api/v2/library/constellation?limit=400` p95 | <= 1,500 ms | — | 266.1 ms | **42.1 ms** | Met (**6.32x faster** than Python) |
| `/api/v2/insights/milestones` p95 | — | — | 2,599.9 ms | **157.5 ms** | **16.5x faster** than Python |
| `/api/v2/media?id=0&kind=cover` p95 | — | — | 2,346.9 ms | **0.4 ms** | **5,867x faster** than Python |

### 10,000- and 20,000-Game Blocking Gate Results

| Size | Write p95 (Gate) | Favorite p95 | Bulk p95 | Library gzip p95 | Library page p95 | Picker p95 (Gate) | Constellation p95 (Gate) | End RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **10,000** | **110.6 ms** (<= 500 ms) | 110.0 ms | 70.2 ms | 1.3 ms | 1.3 ms | **14.6 ms** (<= 200 ms) | **13.8 ms** (<= 1,500 ms) | **113.4 MB** |
| **20,000** | **220.7 ms** (<= 1,000 ms) | 216.1 ms | 144.6 ms | 2.4 ms | 1.3 ms | **27.1 ms** (<= 200 ms) | **19.6 ms** (<= 1,500 ms) | **204.1 MB** |

All 10k and 20k blocking gates, every server endpoint across 10k/20k/50k, and both 50k ADR-0098 graduation budgets beat Python and pass on a single machine with wide margin.

## Consequences

- `library.json` schema v7 remains canonical and 100% downgrade-compatible with OpenBox 2.1.x and the Python fallback (`OPENBOX_BACKEND=python`).
- No storage-engine rewrite is needed to support 50,000-game libraries.
- Release bundles (Linux AppImage, macOS universal `.app`, Windows portable `.zip`) ship the standalone Rust `openbox-server` binary with zero bundled Python runtime (host `python3` is invoked strictly on demand for `.py` v2 plugins via `plugin_runner_v2.py`).
