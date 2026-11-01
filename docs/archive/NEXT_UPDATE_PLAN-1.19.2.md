# OpenBox 1.19.2 preflight plan (W0-07, W6-08)

Status: Dropped. Decision D-04 was declined — the 1.19.2 preflight release was not
cut. The readiness checks listed here shipped instead inside 2.0's first-launch
migration gate (`docs/CHANGELOG.md`, 2.0.0 "Changed"). Kept for the record.

## Goal

Let a 1.19 user see what 2.0 will change, and make a backup that 2.0 can read and 1.19 can restore, before the upgrade happens.

## Scope

1. **Readiness panel** in Settings > About: custom themes with unknown tokens, plugins with an unknown API, a sync folder with format 3 events, backups without a format, a library over 20,000 games, and a non-default data directory.
2. **"Create a 2.0-ready backup"**: writes a 1.19-compatible backup (format 1, v6 library layout) and a copy of `library.json`.
3. **Notice** that explains the API change and links to `docs/upgrade-2.0.md`.
4. **Startup check**: a v7 `library.json` makes 1.19.2 print "This library was upgraded by OpenBox 2.0. Install 2.0 or restore `library.v6.backup.json`." and never normalize it as v6.

## Constraints

- No state schema change. 1.19.2 reads and writes schema 6.
- The 1.19.2 release is cut from `release/1.19` (created at the 1.19.1 baseline, commit b3c4cec).

## Not done in this change

The 1.19.2 code is not written. The readiness panel and the startup check need a 1.19 branch build and a tag, which are release steps. If the maintainer declines D-04, the same checks run on the first 2.0 launch before migration, as the plan allows.
