# ADR 0002: Repository layout, AGENTS.md and CI gates

## Status
Accepted

## Context
Root held 97 Python files (48 runtime + 49 tests), 12 markdown docs, and 4 shell scripts before the layout work. For a machine grep was fine, for a human there was no entry point or visible boundary. Handlers/static/scripts/themes were already well organized, root was the exception. Theming token layer existed (`--brand` etc. in `static/app.css :root`) but 625 raw hex values lived outside `:root`, forcing each theme to redeclare 51-74 rules. The coverage floor at the time (55% total) could not see untested new features.

## Decision
- Move docs except README.md/LICENSE to docs/
- Move tests to tests/, runner walks tests/ then root for compat
- Move parity_* (18 files, 14% of root) to pkg/parity/ as package, keep shims at root for compat during transition
- Keep at root: README.md, LICENSE, Makefile, pyproject.toml, runtime_modules.txt, openbox.py, index.html, native_host.c, static/, themes/, emulator_defs/, handlers/, scripts/
- Add AGENTS.md (and CLAUDE.md symlink) stating layout, design system token rule, non-negotiables, tests, before-PR
- Add scripts/check_tokens.py with ratcheting baseline (625)
- Add diff coverage gate (lines added >=80% covered) via coverage json, and ratchet floors in scripts/check_tests.py
- Defer native/web/packaging moves to later phases

## Consequences
- runtime_modules.txt, run_all_tests.sh, pyproject.toml, Makefile, build_appimage.sh updated to handle both layouts
- ~~Flat `import parity_*` continues to work through root shim modules; new parity implementation lives under `pkg/parity/`.~~ Root shims deleted in 1.9.0; flat `import parity_*` now resolves through the `_ParityFlatFinder` bridge in `pkg/parity/__init__.py` (ADR 0003). Token baseline ratcheted 625 → 0; coverage floors since raised (72.0 total / 54.0 web_app / 80 changed-line / 85 new-module).
- Themes shrink to palette when token set widens

## Alternatives considered
Single giant move vs phased. Phased wins: each PR green, each step independently useful.
