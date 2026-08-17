# ADR 0002: Repository layout, AGENTS.md and CI gates

## Status
Accepted

## Context
Root holds 97 Python files (48 runtime + 49 tests), 12 markdown docs, 4 shell scripts. For a machine grep is fine, for a human there is no entry point or visible boundary. Handlers/static/scripts/themes are already well organized, root is the exception. Theming token layer exists (--brand etc. in static/app.css :root) but 625 raw hex live outside :root, forcing each theme to redeclare 51-74 rules. Coverage floor (55% total) cannot see untested new features.

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
- Flat `import parity_*` continues to work via root copy, new code can use `from openbox.parity import *`
- Themes shrink to palette when token set widens

## Alternatives considered
Single giant move vs phased. Phased wins: each PR green, each step independently useful.
