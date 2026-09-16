#!/usr/bin/env python3
"""Resolve the git diff base used by changed-line and new-module gates.

Resolution order:
  1. explicit ref passed by the caller (--diff-base)
  2. OPENBOX_DIFF_BASE environment variable
  3. GitHub Actions pull request base (GITHUB_BASE_SHA, then the event payload)
  4. the branch's upstream merge base (@{upstream})
  5. origin/HEAD, origin/master, or origin/main merge base

An unresolved base is an error. Before 1.13.0 the gates silently fell back to
HEAD, which made changed-line and new-module coverage no-ops on pull requests
(detached HEAD, no upstream). Callers must fail loudly instead; use
OPENBOX_DIFF_BASE=HEAD to intentionally skip the comparison on a machine with
no usable base ref.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DiffBaseUnresolved(RuntimeError):
    """Raised when no diff base can be resolved and none was supplied."""


def _run(command: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def _merge_base(ref: str, run) -> str | None:
    result = run(["git", "merge-base", "HEAD", ref])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _github_event_base() -> str | None:
    sha = os.environ.get("GITHUB_BASE_SHA", "").strip()
    if sha:
        return sha
    event_path = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        return None
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base = payload.get("pull_request", {}).get("base")
    if isinstance(base, dict):
        return str(base.get("sha") or "").strip() or None
    return None


def resolve_diff_base(explicit: str | None = None, run=_run) -> str | None:
    """Return the resolved base ref, or None when nothing usable exists."""
    if explicit and explicit.strip():
        return explicit.strip()

    env_base = os.environ.get("OPENBOX_DIFF_BASE", "").strip()
    if env_base:
        return env_base

    event_base = _github_event_base()
    if event_base:
        resolved = _merge_base(event_base, run)
        if resolved:
            return resolved
        # A shallow checkout may not contain the base commit. Return the raw
        # SHA so the eventual `git diff` failure stays loud instead of
        # silently comparing HEAD to HEAD.
        return event_base

    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream.returncode == 0 and upstream.stdout.strip():
        resolved = _merge_base(upstream.stdout.strip(), run)
        if resolved:
            return resolved

    for candidate in ("origin/HEAD", "origin/master", "origin/main"):
        resolved = _merge_base(candidate, run)
        if resolved:
            return resolved
    return None
