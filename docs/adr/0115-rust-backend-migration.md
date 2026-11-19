# ADR 0115: Rust Backend Migration & Zero-Regression Architecture

Status: Accepted (2026-11-19)

## Context

ADR 0108 documented that the CPython 3.12 backend missed the 50,000-game RSS budget (2,163.3 MB vs 1,500 MB) and required bundling pinned CPython 3.12 against `GLIBC_2.35` on Linux, universal Python frameworks on macOS, and a host Python 3.11+ interpreter on Windows.

Meanwhile, OpenBox has 170 runtime Python modules (75,821 LOC), 417 HTTP endpoints (`api_contracts.json`), 161 `KNOWN_SETTINGS`, 44 emulator definitions, 181 reliability scenarios, and 17 automated contract ratchets (ADR 0060).

## Decision

1. **Modular Rust Workspace (`crates/`):** Migrate the backend incrementally into 14 cycle-free crates plus `openbox-parity-tests`, compiled into a single `openbox-server` binary. All `unsafe` OS FFI is strictly isolated inside `openbox-platform`; every other crate enforces `unsafe_code = "deny"`.
2. **100% On-Disk & Wire Compatibility:**
   - `library.json` schema v7 stays canonical and byte-compatible (including exact Python `ensure_ascii=True` `_stable_game_id` SHA-256 hashing, `@library` portable path tokens, `sessions.json` crash mirror, `.bak` separate-inode backup, hard-link snapshot naming, and ADR-0007 lock ordering).
   - All 413 `/api/v2/*` and 4 `/api/host/*` routes, RFC 9457 `application/problem+json` errors, CSP framing headers, SSE `/api/v2/events`, Backup v2, Causal Sync v3, and SQLite Read Model (via bundled `rusqlite` with guaranteed FTS5) maintain identical semantics.
   - `openbox-server --dump-contracts` feeds all existing ADR-0060 ratchet scripts directly.
3. **Zero UI & Native-Host Churn:** The vanilla ES-module frontend (`static/`, `index.html`, `themes/`, `locales/`) stays untouched. The three native hosts (`native_host.c`, `native_host_win.c`, `native_host_mac.m`) remain C/Objective-C through cutover with minimal `OPENBOX_SERVER_BIN` spawning support.
4. **Plugin API v2 Continuity:** Core OpenBox requires zero Python. Existing `.py` v2 plugins continue running out-of-process via `plugin_runner_v2.py` (JSON Lines + `bwrap` sandbox) using host `python3` strictly on-demand, while native executable v2 plugin entrypoints are supported additively.
5. **Supply-Chain Governance:** Replace Python's stdlib-only check for Rust with `deny.toml` (`cargo deny check`), pinned `Cargo.lock`, pure-Rust TLS (`rustls`; `openssl-sys` and `native-tls` banned), and CycloneDX SBOM generation from `Cargo.lock`.
6. **Strangler-Fig Rollout & Soak:** During migration, `openbox-http` transparently proxies unmigrated routes to the legacy Python server (`OPENBOX_STRANGLER_PY=1`) and supports continuous differential testing (`scripts/diff_oracle.py`). Release `3.1.0` ships Rust as default (with `OPENBOX_BACKEND=python` rollback on source installs); `3.2.0` retires the migrated Python modules after soak.

## Verification

- `cargo fmt --all -- --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `cargo deny check`, `cargo test --workspace`
- `OPENBOX_CONTRACT_SOURCE=rust make ratchets`
- `python3 -B scripts/diff_oracle.py --rust-bin target/release/openbox-server`
- `OPENBOX_SERVER_BIN=target/release/openbox-server python3 -B scripts/run_ui_smoke.py`
- `OPENBOX_SERVER_BIN=target/release/openbox-server python3 -B scripts/perf_bench.py --sizes 10000,20000,50000 --memory`
