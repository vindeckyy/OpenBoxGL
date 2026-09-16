# OpenBox Plugin API v1

Status: **frozen** (ADR 0056). Additive changes (optional fields, new hooks)
may ship in 1.x; removing fields or changing hook semantics requires API v2.

Plugins are local packages under `<data>/plugins/<id>/` with a `plugin.json`
manifest and a Python entry file. They run in a bubblewrap sandbox with no
home directory, no network, a 5 second timeout, a 2 MiB payload cap, and a
stripped environment. Plugins never run unsandboxed unless the operator sets
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
| `hooks` | yes | Subset of `before_launch`, `after_session`, `library`, `command` |
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
| `library` | catalog projection | Read-only |
| `command` | `{command, library}` | Palette command invocation |

`library` entries are bounded to 500 games and contain only `game_id`, `name`,
`platform`, `progress`, `favorite`, and `playtime_seconds`. A command result
may include `notification: {level: "info"|"success"|"warning"|"error",
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

## Compatibility checklist for authors

- Declare `api_version: 1` and only the hooks you implement.
- Never assume network or home-directory access; both are removed.
- Return a JSON object; other output is ignored with a warning.
- Keep work under 5 seconds or the host kills the run.
