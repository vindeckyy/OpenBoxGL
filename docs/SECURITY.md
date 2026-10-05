# Security Policy

## Fixed in 1.16.0: what 1.15.x and earlier are exposed to

If you run 1.15.x or older, these are the security defects 1.16.0 closes. None is
backported, so upgrading is the fix.

- **Arbitrary `.json` read through a crafted `preview_id`.** Two code paths (the import
  setup preview and the Launch Doctor) joined the request's `preview_id` into a file path
  unchecked, so `../../…/settings` returned any JSON file on disk, including `settings.json`
  with provider credentials. Every `preview_id` now goes through one validator and a
  containment check.
- **Arbitrary file write through a game's `rom_name`.** Restoring a high-score bundle built
  the destination filename from `rom_name`, so a traversal string wrote outside the
  high-score folder. The name is reduced to a safe component and the destination is
  checked to stay inside it.
- **Plugin sandbox could read the library.** The bubblewrap sandbox masked only `/home`,
  `/tmp`, `/run`, `/mnt` and `/media`. With `OPENBOX_DATA_DIR` (a supported override)
  pointing elsewhere, sandboxed plugins could read `library.json` and `settings.json`.
  The resolved data directory is now masked wherever it lives.
- **Unescaped strings in the Wrapped report.** Game names and an error message reached
  `innerHTML` without escaping. The CSP (`script-src 'self'`) blocked script execution, so
  this was one policy change away from exploitable rather than exploitable; it is escaped
  now and a source gate fails if a bare property reaches an HTML sink again.
- **A silently divergent JSON serializer.** When `orjson` happened to be installed, state
  writes used it and could produce different output from the standard library. It is
  removed; the runtime is standard-library only, enforced by `scripts/check_dependencies.py`.
- Smaller hardening: the RetroAchievements cache name is built from an integer, and the
  `flatpak info` probes have a timeout so a hung Flatpak cannot hang a request.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.16.x | Yes (current) |
| 1.15.x | No — upgrade required (see above) |
| 1.14.x | No — upgrade required |
| 1.13.x | No — upgrade required |
| 1.12.x | No — upgrade required |
| 1.11.x | No — upgrade required |
| 1.10.x | No — upgrade required |
| 1.9.x | No — upgrade required |
| 1.8.x | No — upgrade required |
| 1.7.x | No — upgrade required |
| 1.6.x | No — upgrade required |
| 1.5.x | No — upgrade required |
| 1.4.x | No — upgrade required |
| 1.3.x | No — upgrade required |
| 1.2.x | No — upgrade required |
| 1.1.x | No — upgrade required |
| 1.0.x | No — upgrade required |
| 0.9.x | No — upgrade required |
| 0.8.x | No — upgrade required |
| 0.7.x | Best effort |
| 0.6.x | Best effort |
| 0.5.x | Best effort |
| 0.4.x | Best effort |
| < 0.4.0 | No |

Security fixes are provided for the latest release on the `master` branch. Fixes land on `master` going forward; older lines are not backported — upgrade to the latest release to receive them. The 1.16.x line is the current maintained release; the older rows document the historical support policy and should not be read as promises of backported fixes.

## Reporting a vulnerability

Please do **not** report security vulnerabilities through public GitHub issues.

Instead, report them privately using one of the following methods:

1. Open a **private security advisory** on GitHub: [Create a security advisory](https://github.com/vindeckyy/OpenBoxGL/security/advisories/new)
2. If GitHub advisories are unavailable, open a minimal public issue asking for a private contact channel without disclosing exploit details

Include as much of the following as possible:

- Description of the issue and potential impact
- Steps to reproduce
- Affected version(s)
- Proof of concept, if available
- Suggested remediation, if known

## Response expectations

Maintainers aim to acknowledge valid reports within **5 business days** and provide a remediation plan or status update within **14 business days**, depending on severity and complexity.

## Scope

The following are in scope:

- Remote code execution or privilege escalation in OpenBox server components
- Authentication or authorization bypass in the local web API
- Unsafe command execution introduced by OpenBox launch logic
- Credential leakage through logs, backups, or repository defaults

The following are generally out of scope:

- Vulnerabilities in third-party games, emulators, storefront clients, or operating system components launched by OpenBox
- Issues requiring physical access to an unlocked machine with an already running OpenBox instance
- Social engineering or phishing unrelated to OpenBox itself

## Safe usage guidance

- Run OpenBox on trusted local networks. The web UI uses a session token; do not expose it directly to the public internet without a reverse proxy and additional hardening.
- Do not commit `.env`, API tokens, RetroAchievements credentials, or EmuMovies credentials to the repository.
- Keep OpenBox updated to the latest release.
- Plugins run with an OS sandbox when bubblewrap is available. If the sandbox cannot start, OpenBox skips enabled plugins unless `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` is set explicitly. Setting `OPENBOX_ALLOW_UNSANDBOXED_PLUGINS=1` disables the sandbox entirely, even when bubblewrap is available. Only use that override for plugins you trust.

## Release signing

Release publication is fail-closed. The workflow requires `OPENBOX_SIGNING_KEY`, derives an Ed25519 public key in a mode-`0600` temporary file, compares it with the committed `openbox-release.pub`, signs the AppImage, and verifies the signature before upload. The updater and `scripts/install.sh` reject checksum-only releases.

The committed `openbox-release.pub` pins the production release key. Maintainers must preserve these external release controls:

- Generate a new Ed25519 key pair outside the repository and store only the private key as the `OPENBOX_SIGNING_KEY` repository secret.
- When rotating the signing key, replace `openbox-release.pub` and update `RELEASE_KEY_SHA256` in `scripts/install.sh` in the same reviewed change.
- Require approval for the `release` environment and protect `v*` tags against unreviewed pushes; use annotated, signed tags where the GitHub organization supports the signing-key policy.
- Publish `openbox-release.pub`, the `.sig`, the checksum, and `install.sh` together. Rotate the key and repeat the pin update if it is compromised.

## Disclosure

We prefer coordinated disclosure. Reporters will be credited in release notes when fixes ship, unless they request anonymity.
