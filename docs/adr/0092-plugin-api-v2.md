# ADR 0092: Plugin API v2

Status: Accepted (2026-10-29). Implemented in 2.0.0.

## Context

Plugin API v1 is one JSON object in and one out per hook, with no streaming, cancellation, or versioned host services. The 2.0 plan asked for a v2 runner (JSON Lines), versioned read-only host services, and declared subscriptions, with v1 plugins running unchanged.

## Decision

- Manifest v2: `api_version: 2`, optional `min_openbox`, `subscriptions` (at most 8), and the unchanged `network` permission whitelist.
- Runner v2 (`plugin_runner_v2.py`): one JSON request per line on stdin, one response per line on stdout, a 4 MiB stdout cap, SIGTERM then SIGKILL on timeout.
- Host services are versioned and read-only. No service writes the library.
- v1 plugins keep the v1 runner and byte-identical payloads. Removing the v1 runner is a 3.0 task.
- Sandbox: Linux keeps bubblewrap with the same masks. macOS and Windows report the sandbox as unavailable and run plugins only when the user trusts them.

## What was built

- `plugins.py` accepts `api_version` 1 or 2. A v2 manifest may declare `min_openbox` (the oldest OpenBox that serves it) and `subscriptions` (at most 8 known event names). A v1 manifest that carries either field is refused, and a manifest with an unsupported API version is refused naming that version.
- `plugin_runner_v2.py` speaks JSON Lines: an `invoke` request per line, a `result`, `error`, or `cancelled` response per line, a `cancel` request type, and a 4 MiB output cap that stops the runner with `PLUGIN_OUTPUT_TOO_LARGE`.
- The host picks the runner from the manifest's API version. The v1 path is unchanged: the v1 runner, the v1 payload, and the v1 result shapes.
- `pkg/parity/parity_plugin_services.py` publishes a versioned service catalog. Every service is read-only and bounded, and a plugin asking for a version the host does not serve is refused.
- The compatibility report (`GET /api/v2/compat/report`) names every installed plugin with its API version, its disposition, and the sandbox status.
- `plugins/openbox.sample-v2/` is a working v2 sample, listed in the plugin catalog.

## Consequences

- `GET /api/v2/version` advertises `plugin_api_version: 2`, the newest version this build serves. A v1 plugin keeps running unchanged.
- The plugin API stays additive. Removing the v1 runner is a 3.0 task, and a v1 plugin stays valid for the whole 2.x line.
- A v2 plugin that names a `min_openbox` newer than the running build is refused with a message naming both versions, so the user knows what to install.

## Verification

- `tests/test_plugin_api_v2.py`: manifest validation, the runner protocol (result, session, cancel, error, output cap), the host path for both API versions, and the sandbox masks.
- `tests/test_plugin_services.py`: the versioned, read-only service catalog and the refusal of an unserved version.
- `tests/test_compat_report.py`: the plugin section of the compatibility report.
- `tests/test_plugins.py`, `tests/test_plugin_runner.py`, and the other plugin suites still pass unchanged, which is the v1 compatibility evidence.
- `docs/plugin-api-v2.md` is the reference for plugin authors.
