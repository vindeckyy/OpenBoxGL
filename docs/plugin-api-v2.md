# OpenBox Plugin API v2

Status: **accepted** (ADR 0092, implemented in 2.0.0). Plugin API v1 stays valid for the whole
2.x line; see [plugin-api.md](plugin-api.md) for the v1 contract and the deprecation notice.

Plugin API v2 is additive. A v1 plugin keeps the v1 runner, the v1 payload, and the v1 result
shapes, byte for byte. A v2 plugin declares its API version in the manifest and is served by the
JSON Lines runner.

## The manifest

```json
{
  "id": "openbox.sample-v2",
  "name": "Sample v2 Plugin",
  "version": "2.0.0",
  "entry": "plugin.py",
  "hooks": ["after_session", "command"],
  "api_version": 2,
  "min_openbox": "1.19.0",
  "subscriptions": ["session.ended"]
}
```

| Field | Meaning |
|---|---|
| `api_version` | `2` selects the v2 runner. A manifest without it is a v1 manifest. |
| `min_openbox` | The oldest OpenBox that serves this plugin. A build older than this refuses the plugin with both versions named. |
| `subscriptions` | At most 8 event names the plugin wants to be told about. An unknown name is refused. |

The known event names are `library.changed`, `library.progress`, `session.started`,
`session.ended`, and `game.launched`.

The `network` permission whitelist, the hook names, the command list, the settings schema, and
the capabilities are unchanged from v1.

## The runner protocol

The host talks to the plugin over JSON Lines: one JSON object per line in, one per line out.

Request:

```json
{"id": "1", "type": "invoke", "hook": "before_launch", "payload": {"game_id": "g1"}}
```

Response:

```json
{"id": "1", "type": "result", "result": {"game_id": "g1"}}
```

A failing hook answers with `{"type": "error", "error": "..."}`, and a cancelled call answers with
`{"type": "cancelled"}`. A `{"type": "cancel"}` request stops the calls that follow it.

The plugin module itself is ordinary Python: the host calls the hook with the payload object and
reads the object it returns. The JSON Lines framing belongs to the host and the runner, not to
your plugin.

The output is capped at 4 MiB. A plugin that exceeds the cap is stopped with
`PLUGIN_OUTPUT_TOO_LARGE` instead of being allowed to flood the host.

## Host services

A v2 plugin can ask the host for read-only data through the same `service_requests` channel v1
uses. Every service is versioned and bounded:

| Service | Version | Bound |
|---|---:|---|
| `library.query` | 1 | 500 games |
| `achievements` | 1 | 500 items |
| `profile` | 1 | 1 |
| `activity` | 1 | 100 |
| `leaderboards` | 1 | 100 |
| `catalog.search` | 1 | 20 |
| `metadata.search` | 1 | 10 |
| `media.search` | 1 | 20 |

No service writes the library. A request for a version the host does not serve is refused.

## The sandbox

A v2 plugin runs under the same bubblewrap masks as a v1 plugin: no home directory, no network
unless the user granted the `network` permission, and the data directory masked. On macOS and
Windows the sandbox is reported as unavailable, and a plugin runs only when the user trusts it.

## Porting a v1 plugin

| Step | v1 | v2 |
|---|---|---|
| Manifest | no `api_version` | `"api_version": 2` |
| Entry module | one hook function per declared hook | unchanged |
| Hook payload | one JSON object | unchanged |
| Result | one JSON object | unchanged |
| Runner | `plugin_runner.py` | `plugin_runner_v2.py` (the host picks it) |
| Services | `service_requests` | unchanged, now versioned |

A v1 plugin usually needs no change at all: it keeps working. Set `api_version: 2` only when you
want `min_openbox`, subscriptions, or the versioned service catalog.

## What is not in v2

- No plugin can write the library, the settings, or the plugin data bag. The host owns that.
- No network access by default. The `network` permission still has to be declared and granted.
- No plugin is loaded from the network. Packages are installed from a local directory or a ZIP.
