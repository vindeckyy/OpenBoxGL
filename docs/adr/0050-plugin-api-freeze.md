# ADR 0050: Plugin API v1 freeze and catalog trust model

**Date:** 2026-09-16
**Status:** Accepted

## Context

`plugins.py` ran `before_launch`, `after_session`, and `library` hooks in a
bubblewrap sandbox with a 5 second timeout, but the surface was undocumented
and undeclared: malformed packages vanished from the manager, there was no way
for a plugin to contribute commands to the command palette, and the catalog
was a single documentation-only stub. Any change now would be a breaking
change for plugin authors.

## Decision

Freeze **plugin API v1** (documented in `docs/plugin-api.md`):

- Manifest: `id`, `name`, `version`, optional `api_version` (default 1,
  newer versions are refused and reported), optional `entry`, `hooks` from
  `{before_launch, after_session, library, command}`, and `commands`
  (`[{id, label, description?}]`, max 32).
- Hooks receive one JSON object on stdin and return a JSON object on stdout.
  `command` payloads additionally carry a bounded read-only `library` array
  (`game_id`, `name`, `platform`, `progress`, `favorite`, `playtime_seconds`;
  capped at 500 entries). A command result may include a
  `notification: {level, message}` post.
- Palette commands reach the UI through `GET /api/v2/plugins/commands` and
  `POST /api/v2/plugins/command`; commands are declared in the plugin
  manifest.
- Sandbox policy is unchanged and enforced: bubblewrap with no home/network,
  5 second timeout, 2 MiB payload cap, stripped credentials. Plugins do not
  run unsandboxed unless the operator sets
  `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1`.
- Malformed packages and unsandboxed hosts are **surfaced**, not hidden:
  `list_plugins()` returns `valid: false`, an `error`, and a `sandbox`
  status, and the manager renders both.
- Catalog trust: remote entries must be HTTPS with a valid SHA-256 and are
  downloaded before install; bundled `plugins/catalog.json` entries may be
  `local_only` and are documented as install-from-directory/ZIP. No code
  executes during catalog fetch.

## Consequences

- Adding hooks, manifest fields, or payload fields requires a v2 API; v1 is
  append-only (new optional fields are allowed, removals are not).
- Plugin authors can rely on the runner contract and the sandbox guarantees;
  the README-level docs live in `docs/plugin-api.md`.
- The catalog is still intentionally small: a downloadable entry needs a
  hosted, checksummed artifact. Local-only entries stay supported.
