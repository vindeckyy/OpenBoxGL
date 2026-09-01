# ADR 0018: BIOS SHA1 Drift Detection

**Date:** 2026-09-01
**Status:** Accepted

## Context

Launch Doctor's preflight checks already verify BIOS/firmware existence and non-empty BIOS directories. However, a BIOS file can exist but be corrupted, be the wrong version, or be a different file entirely. Without hash verification, Launch Doctor would pass a game that fails to launch due to a mismatched BIOS.

Emulator definitions in `emulator_defs/*.yaml` can specify an expected BIOS filename and, where known, an expected SHA1 hash.

## Decision

Extend the emulator health infrastructure in `pkg/parity/parity_launch_doctor.py` to:

1. Check BIOS existence (existing behavior, preserved).
2. When a BIOS file exists and the emulator definition includes an expected SHA1 hash, compute the file's SHA1 and compare.
3. Report `BIOS_SHA1_DRIFT` when the hash doesn't match.
4. For definitions pointing to a BIOS directory (not a specific file), check directory existence and non-empty status (existing behavior, preserved).
5. Expose health status through `GET /api/v2/emulators/registry?health=1` with `bios_ok`, `firmware_ok`, and `core_ok` per adapter.

SHA1 is computed using `hashlib` from the stdlib — no new dependencies.

## Consequences

- Launch Doctor catches corrupted or incorrect BIOS files before launch.
- `BIOS_SHA1_DRIFT` is a warning, not a hard block — users can still launch.
- 9 new health badge tokens added to `:root` and all 5 themes.
- Definitions without expected hashes fall back to existence-only checks.
