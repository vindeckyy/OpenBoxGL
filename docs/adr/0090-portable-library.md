# ADR 0090: Portable library

Status: Accepted. The path rules are implemented and tested (`pkg/state/paths.py`). Store-level tokenization and the move wizard are described below and tracked in the 2.0 notes.

## Context

Game paths are absolute. A library moved to another drive, machine, or NAS keeps the old paths, so ROMs and media break. Moving the data directory while the server runs is unsafe, so the library root is what moves.

## Decision

- A stored path is an absolute POSIX path, a Windows drive path (stored with forward slashes), a UNC path (never tokenized), a relative path (tokens only), or `@library` / `@library/<relative>`.
- Containment is lexical after `.`/`..` resolution and Unicode NFC normalization. Case folds only where the platform folds it. Stored spelling is never rewritten.
- Decoding refuses NUL bytes, drive prefixes (including alternate-data-stream colons on Windows), UNC prefixes, leading separators, and any `..` segment, then checks containment of the joined path again.
- The library root must be absolute and local. UNC roots are refused.
- The move wizard rebases paths: the user moves the folder with a file manager and OpenBox rewrites stored paths under the old root. OpenBox never copies files.

## Consequences

- Media under the data directory stays absolute; it moves with the data directory, not with the library root.
- Symlinks that resolve outside the root are reported, not tokenized (`SYMLINK_OUTSIDE_ROOT`, planned with the move preview).

## Verification

`tests/test_portable_paths.py` (17 rule tests across POSIX, Windows, NFC/NFD, and root validation), `tests/test_state_v7.py` (settings mirror), `tests/test_state_migration_rollback.py` (integrity categories).
