"""Package a redacted diagnostic report for pasting into a GitHub issue.

Local-only and opt-in: nothing leaves the machine unless the user copies it.
"""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from openbox_logging import read_diagnostic_log


def system_facts() -> dict:
    """Best-effort host facts, each guarded so a missing module never blocks."""
    facts = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "user_agent_os": "",
        "data_dir": "",
        "version": "",
    }
    try:
        import updates  # local import keeps this importable pre-install

        facts["version"] = getattr(updates, "VERSION", "")
    except Exception:  # pragma: no cover - import failure is the point
        pass
    return facts


def build_report(data_dir, *, include_log=True, log_limit=250_000):
    """Return the diagnostic report text; the log is redacted on read."""
    root = Path(data_dir)
    facts = system_facts()
    facts["data_dir"] = str(root)
    try:
        with (root / "library.json").open(encoding="utf-8") as source:
            library_size = len(source.read().encode("utf-8", errors="replace"))
        facts["library_bytes"] = library_size
    except OSError:
        facts["library_bytes"] = 0
    log = read_diagnostic_log(root) if include_log else ""
    return json.dumps(
        {
            "report": "openbox-diagnostic",
            "version": facts.pop("version"),
            "system": facts,
            "diagnostic_log": log[-log_limit:] if include_log else "",
            "python_executable": sys.executable,
        },
        indent=2,
    )
