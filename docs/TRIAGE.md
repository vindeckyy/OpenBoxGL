# Issue triage

OpenBox is maintained part-time. This page sets expectations so reports get useful answers.

## Response targets

- **Security reports**: acknowledged within 5 business days, status within 14 (see SECURITY.md).
- **Bug reports**: first human response within 7 days.
- **Feature requests**: triaged into roadmap or closed with a reason within 30 days.

## How we triage

1. Reproduce or mark `needs-info`: a report without version, distro, and steps to reproduce gets a `needs-info` label and a request for the diagnostic report (Settings -> Library Audit, or `/api/diagnostic`).
2. Confirm or close: unreproducible reports stay open for 14 days, then close with an invitation to reopen with more detail.
3. Severity: `critical` (data loss, launch breakage for everyone), `major` (broken feature for many), `minor` (edge case), `polish` (cosmetic).

## What helps most

- The OpenBox version, distro, desktop, and Python version when running from source.
- The diagnostic report from Settings. It is redacted and local-only.
- Steps that reproduce the issue from a fresh data dir (`OPENBOX_DATA_DIR=/tmp/openbox-repro python3 web_app.py --no-browser`).

## Labels

- `bug`, `enhancement`, `documentation`
- `needs-info`, `reproduced`, `confirmed`, `wontfix`
- `critical`, `major`, `minor`, `polish`
- `good first issue` for small, well-scoped work
