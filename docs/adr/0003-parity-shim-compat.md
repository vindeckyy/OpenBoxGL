# ADR 0003: Parity shim compatibility

Date: 2026-08-20
Status: Accepted

## Context

The wide-improvements plan Step 5 proposed deleting the 22 root `parity_*.py` shims and replacing flat `import parity_*` with a lazy `MetaPathFinder` alias in `pkg/parity/__init__.py`. The shims currently do `sys.modules[__name__] = importlib.import_module("pkg.parity.<name>")`.

## Decision

Keep the 22 shims for one release as a compatibility fallback. `import parity_*` before `import pkg.parity` would fail without the shim file on `sys.path`, so deleting shims now would break standalone `python3 -c "import parity_backup"` and any plugin that does flat imports without first importing `pkg.parity`. The shims already alias `sys.modules["parity_*"]` to `pkg.parity.parity_*` on first flat import.

Runtime and tests continue to use both `import parity_*` (flat) and `from pkg.parity.parity_* import` (canonical) via the shim alias, which is verified by `tests/test_parity_playnite.py` and `python3 -c "import parity_backup; print(parity_backup.__file__)"`.

## Consequences

- `runtime_modules.txt` retains both shim and canonical entries (96 lines) for this release.
- `pkg/parity/__init__.py` remains a minimal comment-only package init (no eager import loop) to avoid the `parity_perf -> parity_gamescope` circular import that an eager `for p in glob(...): import_module(...)` would cause.
- Next release will implement a lazy `MetaPathFinder` (`_ParityFlatFinder`) in `pkg/parity/__init__.py` and delete shims after updating all internal imports to `pkg.parity` and ensuring entry points (`openbox.py`, `web_app.py`) import `pkg.parity` early.
- No functional regression; `make check` verifies flat imports still resolve.
