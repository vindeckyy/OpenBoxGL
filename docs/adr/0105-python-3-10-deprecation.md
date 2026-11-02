# ADR 0105: Python 3.10 stays in 2.1 and is deprecated

Status: Accepted (2026-11-01)

## Context

ADR 0100 asks 2.1 to revisit the Python 3.10 floor with the AppImage base. The AppImage is built inside `ubuntu:22.04` to keep the glibc 2.35 floor, and that container ships Python 3.10. Windows source installs require Python 3.10+, and the macOS bundle carries CPython 3.12.

## Decision

- 2.1 keeps Python 3.10 support.
- Python 3.10 is deprecated: the server logs a warning at startup, and the docs recommend 3.12.
- CI keeps a 3.10 lane and a 3.12 lane.
- Removal is planned for 3.0, together with a new AppImage base/glibc-floor decision.

## Consequences

- No user is forced to upgrade Python to run 2.1.
- The 3.0 plan must choose a new base and floor before dropping 3.10.

## Verification

`docs/SUPPORT.md`, `updates.py`, `.github/workflows/ci.yml`, `web_app.py`.
