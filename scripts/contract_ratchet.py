#!/usr/bin/env python3
"""Shared machinery for the ADR 0060 feature-regression ratchets.

Every contract gate obeys one rule:

    A baselined feature may only leave the surface by moving into a ``retired``
    ledger entry carrying a non-empty reason. Growth is always free.

That rule needs two comparisons, and the second one is easy to get wrong:

* **Layer 1** compares the live code against the committed baseline. It needs
  nothing but the tree, so it works everywhere, and it is what protects the
  shipped artifact.
* **Layer 2** compares the baseline against the *previous* baseline, to catch
  the bypass where someone edits the baseline to make a layer 1 failure go away.

Layer 2 needs a reference commit that predates the change. ``git HEAD`` is not
that commit: CI checks out the commit under test, so ``HEAD:contracts/x.json``
is the baseline being tested, the comparison is against itself, and every
bypass passes. This module resolves a real reference instead:

1. ``$OPENBOX_CONTRACT_REF`` when set - explicit, and what the tests drive.
2. **The merge-base with the default branch** (``origin/HEAD``, ``origin/main``,
   ``origin/master``, ``main``, ``master``). That is the fork point, so every
   commit on a local branch is judged at once instead of only the last one.
3. **The parent commit** - the case of a push straight to the default branch,
   where the reference state is the previous integrated commit.
4. **Nothing** - a shallow clone, an export, or a source tarball. Layer 2
   prints which reference it used, or that it was skipped; layer 1 still holds.

Because case 2 and 3 need history, CI must check out with ``fetch-depth: 0``
for the ratchet to be authoritative there. Without it the gates degrade to
layer 1 and say so in the log rather than failing.

Run any gate directly; see ADR 0060.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = "scripts/contracts"
REF_ENV = "OPENBOX_CONTRACT_REF"

# Branch names that stand in for "the integrated default branch", most
# specific first. A merge-base against any of them is the fork point.
_UPSTREAM_CANDIDATES = ("origin/HEAD", "origin/main", "origin/master", "main", "master")


def _git(*args: str) -> str | None:
    """Run git in the repository root; None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def reference_rev() -> tuple[str | None, str]:
    """Return (rev, human label) for the commit the baseline is judged against."""
    override = os.environ.get(REF_ENV, "").strip()
    if override:
        return override, f"{REF_ENV}={override}"
    head = _git("rev-parse", "HEAD")
    for ref in _UPSTREAM_CANDIDATES:
        base = _git("merge-base", "HEAD", ref)
        if base and base != head:
            return base, f"merge-base with {ref}"
    parent = _git("rev-parse", "HEAD^")
    if parent:
        return parent, "the parent commit"
    return None, "no git history available"


def reference_data(filename: str) -> tuple[dict | None, str]:
    """Return (baseline parsed at the reference rev, label describing it).

    ``None`` means layer 2 cannot run, and the label says why. Callers report
    that instead of failing, because layer 1 is still enforcing the rule.
    """
    rev, label = reference_rev()
    if rev is None:
        return None, label
    label = f"{label} {rev[:12]}"
    raw = _git("show", f"{rev}:{CONTRACT_DIR}/{filename}")
    if raw is None:
        return None, f"{label} (baseline absent there)"
    try:
        return json.loads(raw), label
    except json.JSONDecodeError:
        return None, f"{label} (baseline unparseable there)"


def ledger_consistency(items, retired) -> list[str]:
    """Return failures for ledger rules that need no reference commit.

    These hold even in a git-less tree: every record carries a reason, and a
    record cannot be both live and retired.
    """
    failures: list[str] = []
    for key, reason in sorted(retired.items()):
        if not str(reason).strip():
            failures.append(f"RETIRED-NO-REASON {key} has an empty reason")
        if key in items:
            failures.append(f"RETIRED-STILL-LIVE {key} is both present and retired")
    return failures


def ledger_failures(
    reference_items,
    reference_retired,
    items,
    retired,
    *,
    noun="entry",
    drop_label="BASELINE-SHRINK",
    drop_reason="was dropped from the baseline",
    drop_remedy="",
) -> list[str]:
    """Return failures for the reference-dependent half of the ledger rule.

    ``items`` is the current surface (any iterable of identities) and
    ``retired`` maps identity -> reason. ``reference_*`` are the same shapes
    read at the reference commit. Pure: no filesystem, no git, so the rule
    itself is unit-testable rather than only exercised through a gate.
    """
    failures: list[str] = []

    # A recorded entry may only leave by being retired, with a reason.
    for key in sorted(reference_items):
        if key in items or key in retired:
            continue
        remedy = drop_remedy.replace("{key}", key)
        failures.append(f"{drop_label} {key}: {drop_reason} {remedy}".rstrip())

    for key in sorted(retired):
        if key not in reference_items and key not in reference_retired:
            failures.append(f"RETIRED-FABRICATED {key} was never in the reference {noun} or its ledger")

    # The ledger is append-only, so the record outlives the code it describes.
    for key, reason in sorted(reference_retired.items()):
        if key not in retired:
            failures.append(f"RETIRED-REMOVED {key} was deleted from the retired ledger")
        elif retired[key] != reason:
            failures.append(f"RETIRED-REWRITTEN {key}: the recorded reason was changed")

    return failures
