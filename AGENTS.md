# OpenBox - Agent Conventions

Read this before writing code. A convention that lives only in a human's head is one prompt away from being ignored.

## Where things go
- `openbox.py` is the launcher entry point, stays at root.
- `handlers/` - HTTP handlers, one module per domain or feature surface (library, imports, media, party, picker, etc.). Shared helpers live in `_shared.py`.
- `pkg/parity/` - parity_* modules (clear names, cohesive cluster) plus the shared `launch_tokens.py` helper. New parity code goes there. Flat `import parity_x` still resolves via the MetaPathFinder in `pkg/parity/__init__.py`; entry points (`openbox.py`, `web_app.py`) must `import pkg.parity` before any flat `parity_*` import.
- `static/` - 27 JS modules (25 domain modules plus app.js and worker.search.js) and app.css.
- `themes/` - theme CSS files, each overrides `:root` tokens only.
- `emulator_defs/` - YAML definitions.
- `tests/` - standalone `test_*.py` scripts run via `./run_all_tests.sh` (`python3 -B file.py` style). Tests are feature/module-scoped, not a strict 1:1 mirror; every new runtime module still needs a covering test file.
- `scripts/` - check_tests.py, check_tokens.py, check_v1_contract.py, etc.
- `docs/` - all markdown except README.md and LICENSE. ADR in `docs/adr/`. Superseded plans and old release notes go in `docs/archive/`, never deleted.
- `build/`, `native_host`, `.venv-dev/`, `.coverage` - generated, never commit.

New integration goes in `pkg/parity/` or `handlers/` as appropriate, never at root. New runtime module must be added to `runtime_modules.txt`.

## Design system
- Every color, spacing, and typography value comes from a token in `static/app.css` `:root` (`--brand`, `--focus`, `--surface-card`, `--font-heading`, etc.).
- No raw hex in component rules outside `:root`. A new visual value means a new token plus its entry in each theme file.
- Token names are the theme contract. Themes override `:root` and mostly nothing else.
- `scripts/check_tokens.py` enforces this with a ratcheting baseline (ratcheted to 0, originally 625).

## Non-negotiables
- Runtime stays dependency-free. `pyproject.toml` dev deps are not installed in AppImage.
- v1 route surface is frozen (`scripts/check_v1_contract.py` + `v1_contracts.json`). Drift fails the gate.
- Coverage floors go up, never down. `COVERAGE_FLOOR` and `WEB_APP_FLOOR` in `scripts/check_tests.py` are ratcheted.
- New runtime module must be added to `runtime_modules.txt` (enforced for root `*.py`, `handlers/`, `pkg/state/`, `pkg/parity/`, `routes/`) and have a `test_*.py` (convention, not gate-enforced).

## Tests
- One `test_*.py` per module, standalone-script style.
- Run all with `./run_all_tests.sh` (walks `tests/` preferentially; root `test_*.py` only run when `tests/` has none).
- Full gate with `make check` → `scripts/check_tests.py`: ruff, runtime_modules, v1_contract, version_sync, frontend, i18n, py_compile, tests under coverage, coverage floors (total + web_app), changed-line, new-module, tokens.

## Before opening a PR
- `make check` must pass locally.
- Add changelog entry if user-visible.
- Add ADR in `docs/adr/` when changing layout, contracts, or gates.

## Conventions
- Commit format: conventional, scope is module.
- When adding a UI component, add a token if needed and update all themes.
