# ADR 0056: Plugin trust model — per-plugin, checksum-bound, default deny

**Date:** 2026-09-22
**Status:** Accepted

## Context

ADR 0050 froze the plugin API v1 sandbox policy: bubblewrap with no
home/network, and plugins do not run unsandboxed unless the operator sets
`OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1`. That escape hatch is a host-wide
override with no per-plugin record and no user-facing prompt — the sandbox
is either on for everyone or off for everyone. Plugins 2.0 needs a model
where a host without bubblewrap (e.g. Windows) can still run a *chosen*
plugin unsandboxed, while everything else stays denied.

## Decision

Replace the global escape hatch with per-plugin, checksum-bound trust:

- **Default deny.** A plugin on a host without bubblewrap does nothing
  until the user explicitly trusts it. There is no global trust toggle and
  no env var that silently trusts everything.
- **Trust is keyed by `(plugin id, package SHA-256)`.** The grant records
  the checksum of the installed package; any update that changes the
  package invalidates the grant and re-prompts. A malicious or accidental
  package swap cannot ride on an old approval.
- **Prompt is in-product, not in config.** The Plugins dialog shows a
  trust prompt with the plugin name, version, and checksum, with explicit
  "Trust and run" / deny choices and a "Revoke trust" control. Deny is
  the safe default; the plugin simply does not execute.
- **The env escape hatch stays for parity.** `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1`
  keeps its historical behavior (back-compat), but the UI never sets it and
  the docs steer users to per-plugin trust instead.
- **Permissions follow the same shape (F1b).** The only permission in
  1.14.0 is `network` (grants `--share-net`); ungranted permissions are
  denied by default, grants are stored per plugin, and the prompt appears
  at install/enable time in the Android-style install-flow position.

## Consequences

- On sandbox-capable hosts nothing changes: trust is irrelevant and the UI
  hides the prompt.
- On Windows/no-bubblewrap hosts, each plugin requires one informed click
  before it runs. Removing a plugin clears its trust, permission, and
  settings state.
- Trust state lives in the state file alongside disabled flags; checksums
  are recomputed from the installed package at prompt time, not cached.
