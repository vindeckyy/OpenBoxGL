# ADR 0061: Emulator-definition update channel

**Date:** 2026-09-29
**Status:** Accepted (1.15)

## Context

Twenty-four emulator definitions ship in `emulator_defs/`, but a user on a newer
emulator build had no way to receive a newer definition short of editing YAML by
hand. 1.14 shipped the backend for a signed community pack
(`pkg/parity/parity_emulator_defs_update.py`, `handlers/defs.py`, four `/api/v2`
routes) and credited it to ADR 0060, which is about the feature-regression
ratchets. The channel therefore had no decision record of its own, no UI, and no
way to produce the pack it downloads: `PACK_BASE` pointed at a pinned commit that
never contained an `index.json`, so the first real check would have 404ed.

## Decision

- **Routes** (additive `/api/v2`, the v1 surface is untouched):
  `GET /api/v2/emulators/defs/status`, `GET` and `POST /api/v2/emulators/defs/update`,
  `POST /api/v2/emulators/defs/rollback`.
- **Verification is never re-implemented.** The archive is verified with
  `updates.verify_artifact` against `openbox-release.pub`, which already rejects
  small-order keys, fails closed on a malformed signature payload and normalizes the
  digest. A bad signature raises a persisted `security` notification, not a generic
  failure.
- **All-or-nothing.** The pack is fetched, verified, parsed and validated in full
  before a single file is written.
- **Local wins.** The pack installs into the per-user data directory and shadows the
  bundled set; it never overwrites it, and a definition the user already has is kept.
  Rollback removes exactly what the channel installed.
- **Publishing.** `scripts/build_defs_pack.py` builds a byte-deterministic
  `community-defs.tar.gz` and `index.json`. It never touches a key. Signing needs the
  private half of the release key, so it is a maintainer step (`scripts/sign_release.py`),
  followed by uploading the archive, its `.sig` and `index.json` to the rolling
  `emulator-defs` release that `PACK_BASE` names.
- **Mutable location, immutable trust.** `PACK_BASE` is no longer a pinned commit. The
  location can change; the trust cannot, because nothing is installed unless the
  signature verifies. The index is only a cheap "is there something newer" hint.
- **No pack published is a normal state.** A 404 from the index is shown as "No community
  definition pack is published yet", not as an error.
- **UI.** Settings > Emulators shows the installed pack, the definitions the user edited
  (never overwritten), and the check / install / roll back actions.

## Consequences

- The channel is reachable from the UI and can be turned on by publishing one signed pack.
- A downgrade (serving an older, validly signed pack) is possible from a mutable location.
  The blast radius is a definition set that was once valid, and rollback restores the
  bundled set. If that ever matters, put the pack version inside the signed archive and
  refuse a lower one.
- Publishing depends on a maintainer holding the key; the code cannot do it.

## Addendum (1.16.1): schema 2, the core key, and the signed pack release

- **Schema 2.** A RetroArch definition names its core once, in `retroarch_core: <file>`, and puts
  `{retroarch_core}` where the core's path goes in `startup_args`. The builder puts the system path
  (`/usr/lib/libretro/`) there, and the Flatpak mapping applies to it as before. Schema 1 definitions
  keep working. The 23 RetroArch definitions are on schema 2.
- **Refusal.** A pack whose `schema_version` is above `SUPPORTED_SCHEMA_VERSION` (2) is refused at
  validation with a message that names the version it needs. It is not half-read.
- **Per-game core.** A game can launch with an installed core instead of the definition's default. The
  choice is stored on the game as `retroarch_core` (`POST /api/v2/launch/core`) and only a plain
  `*_libretro.so` name is accepted.
- **Pack release.** `release.yml` builds `community-defs.tar.gz`, signs it with the release key, verifies
  it against the committed public key, and uploads it with its signature and `index.json` to the
  `emulator-defs` release, which is where the channel reads (`PACK_BASE`). The step refuses to run
  without `OPENBOX_SIGNING_KEY`, as the other artifacts do. Verified locally with a throwaway key:
  `verify_release` and the channel's `download_and_verify` both accept the signed pack. The first real
  pack needs a tagged CI run with the maintainer's key.
