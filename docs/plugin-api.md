# OpenBox Plugin API v1

Status: **frozen** (ADR 0050). Additive changes (optional fields, new hooks)
may ship in 1.x; removing fields or changing hook semantics requires API v2.

> **Not deprecated, but superseded for new plugins.** API v1 stays valid for the whole 2.x line
> and its payloads do not change. A new plugin should declare `"api_version": 2` and follow
> [plugin-api-v2.md](plugin-api-v2.md) (ADR 0092): `min_openbox`, declared subscriptions, the
> JSON Lines runner, and versioned host services. The v1 runner is removed in 3.0.

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
| `hooks` | no | Defaults to `[]`; the base hooks (`before_launch`, `after_session`, `library`, `command`, `library_source`, `events`) plus the capability hook required by a declared capability (`storefront`, `metadata`, `media`, `stats`, `emulator`, `discovery`) |
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
  from a local package. Optional `author` (80 chars) and `homepage` (HTTPS)
  fields are shown in the catalog browser.
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
sandbox. Storefront, metadata, media, and stats capabilities refuse to call
their hook without a declared and granted network permission. Grants are
stored per plugin; removing the plugin clears grants, settings, data, and
credentials.

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

### Capability plugins (1.18)

A manifest may declare `capabilities` alongside the hook that implements each
one. Declaring a capability without its hook is a manifest error.

```json
"hooks": ["metadata", "storefront"],
"capabilities": {
  "metadata": {"id": "vendor.metadata", "name": "Vendor metadata", "platform": "Nintendo Switch"},
  "storefront": {"id": "vendor.store", "name": "Vendor store"}
}
```

| Capability | Required hook | Extra fields |
|---|---|---|
| `storefront` | `storefront` | — |
| `metadata` | `metadata` | optional `platform` |
| `media` | `media` | optional `media_kinds` |
| `stats` | `stats` | — |
| `emulator_settings` | `emulator` | list of `{emulator_id, label, schema, scopes}` |
| `discovery` | `discovery` | — |

Capability hooks receive the usual payload plus:

- `plugin_data` — the plugin's bounded data bag (1 MiB, depth 4).
- `settings` — non-secret settings values.
- `credentials` — values from the capability credential store (never
  password-format settings).

Capability hooks may return `data`
(shallow-merged into the data bag), `service_requests` (up to 8, one round
only), and the usual `notification`. The eight read-only services are
`library.query`, `achievements`, `profile`, `activity`, `leaderboards`,
`catalog.search`, `metadata.search`, and `media.search`. Credentials cannot be
written from a hook result. Backups exclude the capability credential store
and blank password-format setting values.

Capability hook timeout budgets: `storefront` 30 s, `metadata`/`media`/`stats`
15 s, `emulator`/`discovery` 10 s. Legacy hooks keep 5 s.

### Localized messages and notifications

A manifest may include a bounded `messages` table:

```json
"messages": {
  "sync_done": {"en": "{count} games synced", "de": "{count} Spiele synchronisiert"}
}
```

A hook notification may reference it as
`{"level": "success", "key": "sync_done", "params": {"count": 12}}`. The host
resolves the active locale, then English, then the raw key. `level` is one of
`info`, `success`, `warning`, `error`. Each manifest template is capped at 500
characters; the resolved notification is capped at 2,000.

### Storage routes

- `GET /api/v2/plugins/settings?id=<id>` returns the schema, non-secret values,
  `secret_set`, and a credential key summary.
- `POST /api/v2/plugins/settings` `{id, values, clear_secrets?}` saves settings.
  A blank password field keeps the stored secret; list the field in
  `clear_secrets` to remove it.
- `POST /api/v2/plugins/credentials` `{id, values, clear}` writes capability
  credentials. Responses contain key names and presence flags, never values.

Credentials and the data bag live under `<data>/plugins/credentials/` and
`<data>/plugins/data/` with `0600` permissions. A malformed file reads as
empty; it never prevents a plugin from loading.

## Compatibility checklist for authors

- Declare `api_version: 1` and only the hooks you implement.
- Never assume network or home-directory access; both are removed.
- Return a JSON object. Invalid output (bad JSON, a non-object, oversized
  output, or a nonzero exit) is a hook error: palette commands surface it
  to the caller, while chained hooks (`library`) discard that plugin's
  output with a logged warning and continue.
- Keep work under 5 seconds or the host kills the run.
