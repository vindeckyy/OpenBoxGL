# OpenBox Plugin API v1

Status: **frozen** (ADR 0050). Additive changes (optional fields, new hooks)
may ship in 1.x; removing fields or changing hook semantics requires API v2.

Plugins are local packages under `<data>/plugins/<id>/` with a `plugin.json`
manifest and a Python entry file. They run in a bubblewrap sandbox with no
home directory, no network, a 5 second timeout, a 2 MiB payload cap, and a
stripped environment. On hosts without bubblewrap a plugin does nothing until
it is trusted: either individually in the Plugins manager ("Trust and run",
bound to the package SHA-256 per ADR 0056) or, for operators, globally with
`OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` for trusted local code; unsandboxed and
malformed packages are shown in the Plugins manager instead of being hidden.

## Manifest (`plugin.json`)

```json
{
  "id": "vendor.plugin",
  "name": "Vendor Plugin",
  "version": "1.2.0",
  "api_version": 1,
  "entry": "plugin.py",
  "hooks": ["command"],
  "commands": [
    {"id": "scan", "label": "Scan library", "description": "Reports a count."}
  ]
}
```

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `^[a-z0-9][a-z0-9._-]{1,63}$` |
| `name` | yes | Shown in the Plugins manager |
| `version` | yes | Free-form; shown verbatim |
| `api_version` | no | Integer, default `1`; newer versions are refused and surfaced with an error |
| `entry` | no | Python file inside the package, default `plugin.py` |
| `hooks` | no | Defaults to `[]`; subset of `before_launch`, `after_session`, `library`, `command`, `library_source`, `events` |
| `commands` | no | Up to 32 `{id, label, description?}` entries (used by `command`) |

## Hooks

Every hook is a function named after the hook in the entry module. It receives
one JSON object on stdin and returns a JSON object on stdout:

```python
def command(payload):
    games = payload["library"]
    return {"notification": {"level": "success", "message": f"{len(games)} games"}}
```

| Hook | Payload | Notes |
|---|---|---|
| `before_launch` | `{game, args, cwd}` | May adjust `args`/`cwd`; the host validates bounds and falls back to the original launch command |
| `after_session` | session record | Best-effort; failures are logged and never break session bookkeeping |
| `library` | `{"games": [...]}` | Full public game projection; read-only. A failing plugin's output is discarded with a logged warning and the chain continues |
| `command` | `{command, library}` | Palette command invocation; `library` here is bounded to 500 entries with only `game_id`, `name`, `platform`, `progress`, `favorite`, `playtime_seconds` |
| `library_source` | `{"api_version": 1, "settings"?}` | Importer; must return `{"games": [...]}` (details below) |
| `events` | `{"event": ..., ...}` | Lifecycle events (details below) |

A `command` result may include `notification: {level: "info"|"success"|"warning"|"error",
message: "..."}`.

## Palette commands

- `GET /api/v2/plugins/commands` returns `{api_version, sandbox, commands}`.
  `commands` are the manifest-declared commands from enabled, valid plugins.
- `POST /api/v2/plugins/command` with `{plugin_id, command}` runs the
  `command` hook in the sandbox and returns the plugin result and any
  notification.
- `static/palette.js` lists registered commands under the `>` prefix and
  posts the selection to the route above.

## Installing

- Local directory or ZIP: `POST /api/plugins/install` with `{path}` (also
  available from the Plugins manager). Directories may not contain symlinks.
- Remote catalog: bundled `plugins/catalog.json` entries with a `url` must be
  HTTPS and carry a `sha256`; the archive is checksummed before extraction.
  Entries marked `local_only` are documentation-only and must be installed
  from a local package.
- Enabling/disabling: `POST /api/plugins/toggle` with `{id, enabled}`.
  Removing keeps a recoverable copy under `<data>/plugins/.removed/`.

## Plugins 2.0 additions (1.14.0)

Additive on top of the frozen v1 surface; nothing above changed.

### Permissions

A manifest may declare `"permissions": ["network"]` (the only permission in
1.14.0). Declared permissions are denied by default: the plugin runs without
network unless the user grants it at install/enable time (Android-style
prompt in the Plugins manager). Granting `network` adds `--share-net` to
the sandbox argv; without the grant the plugin keeps the no-network
sandbox. Grants are stored per plugin and cleared on removal.

### Settings

A manifest may declare a JSON Schema subset under `"settings"`:

```json
"settings": {
  "type": "object",
  "properties": {
    "api_key": {"type": "string", "format": "password", "title": "API key"},
    "region": {"type": "string", "enum": ["us", "eu"], "default": "us"},
    "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 10},
    "verbose": {"type": "boolean", "default": false}
  },
  "required": ["api_key"]
}
```

Supported types: `string`, `number`, `integer`, `boolean`, plus `enum`.
`minLength`/`maxLength` and `minimum`/`maximum` are enforced. The Plugins
manager renders a settings form from the schema (text, number, checkbox,
select, password), stores validated values per plugin, and injects them
into the stdin payload as `payload["settings"]` — but only for plugins
whose manifest declares a `settings` schema. Unset optional
fields fall back to their `default`.

### Trust (sandbox-unavailable hosts)

On hosts without bubblewrap, plugins do nothing until the user trusts them
individually in the Plugins manager ("Trust and run", per ADR 0056). The
grant is bound to the installed package's SHA-256: any update that changes
the package invalidates the grant and re-prompts. There is no global trust
switch; the legacy `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` env override is
unchanged for operators who need it.

### `library_source` hook (importer)

Declaring `library_source` makes the plugin a library importer: the hook
receives `{"api_version": 1}` on stdin (plus `settings` when the manifest
declares a settings schema) and must return `{"games": [...]}` on stdout.
A bare list or any other shape is rejected with a logged warning and the
plugin contributes nothing on that build. Entries are validated like folder
imports, namespaced as
`plugin:<plugin_id>:<their_id>`, and merged into the public library on
every state build with `plugin_source` / `plugin_source_name` provenance
(the UI shows a source badge). Disabling or removing the plugin drops its
games on the next rebuild. Returned lists are capped at the library entry
limit.

### `events` hook (lifecycle)

One hook for all lifecycle events (ADR 0057). The stdin payload always
carries an `event` field plus a small bounded payload:

| Event | Payload fields |
|---|---|
| `app_startup` | — |
| `app_shutdown` | — |
| `scan_finished` | `folder`, `added`, `scanned` |
| `playtime_milestone` | `game_id`, `name`, `hours`, `playtime_seconds` |
| `game_added` | `game_ids` (up to 500 ids) |
| `game_removed` | `game_ids` (up to 500 ids) |
| `game_updated` | `changes` (up to 100 `{game_id, changed: [...]}` entries) |

All emission is best-effort and bounded by the standard per-plugin
timeout; failures never break the host operation.

```python
def events(payload):
    if payload["event"] == "playtime_milestone":
        # congratulate the player every hour
        return {"notification": {"level": "success", "message": f"{payload['hours']}h in {payload['name']}!"}}
    return {}
```

### New v2 routes

- `GET /api/v2/plugins/trust?id=<id>` / `POST /api/v2/plugins/trust`
  `{id, trusted}` — per-plugin sandbox-bypass trust (checksum-bound).
- `POST /api/v2/plugins/permissions` `{id, permissions}` — grant
  declared permissions (currently `network` only).
- `GET /api/v2/plugins/settings?id=<id>` / `POST /api/v2/plugins/settings`
  `{id, values}` — schema, stored values, and validated save.
- `GET /api/v2/plugins/catalog` — catalog entries enriched with
  `installed`, `installed_version`, `update_available`; `sandbox` is a
  top-level field of the response.

## Compatibility checklist for authors

- Declare `api_version: 1` and only the hooks you implement.
- Never assume network or home-directory access; both are removed.
- Return a JSON object. Invalid output (bad JSON, a non-object, oversized
  output, or a nonzero exit) is a hook error: palette commands surface it
  to the caller, while chained hooks (`library`) discard that plugin's
  output with a logged warning and continue.
- Keep work under 5 seconds or the host kills the run.
