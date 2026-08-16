# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |
| 0.9.x | Yes |
| 0.8.x | Yes |
| 0.7.x | Best effort |
| 0.6.x | Best effort |
| 0.5.x | Best effort |
| 0.4.x | Best effort |
| < 0.4.0 | No |

Security fixes are provided for the latest release on the `master` branch.

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

## Release signing

Release publication is fail-closed. The workflow requires `OPENBOX_SIGNING_KEY`, derives an Ed25519 public key in a mode-`0600` temporary file, compares it with the committed `openbox-release.pub`, signs the AppImage, and verifies the signature before upload. The updater and `scripts/install.sh` reject checksum-only releases.

The committed `openbox-release.pub` is still a placeholder, so a maintainer must complete these external release controls before publishing the first usable signed release:

- Generate a new Ed25519 key pair outside the repository and store only the private key as the `OPENBOX_SIGNING_KEY` repository secret.
- Replace `openbox-release.pub` with the real 32-byte public key and update `RELEASE_KEY_SHA256` in `scripts/install.sh` in the same reviewed change.
- Require approval for the `release` environment and protect `v*` tags against unreviewed pushes; use annotated, signed tags where the GitHub organization supports the signing-key policy.
- Publish `openbox-release.pub`, the `.sig`, the checksum, and `install.sh` together. Rotate the key and repeat the pin update if it is compromised.

## Disclosure

We prefer coordinated disclosure. Reporters will be credited in release notes when fixes ship, unless they request anonymity.
