"""Command-line library maintenance: check, repair, and migrate.

Dispatched by ``web_app.py`` (and the launchers that exec it) before any
server, window, or handler starts. Output is JSON on stdout; refusals and
usage text go to stderr.

Exit codes:

- 0: success, or a healthy library
- 1: ``--check-library`` found warnings
- 2: ``--check-library`` found errors
- 3: refused (OpenBox running, sessions active, invalid library or copy)
- 4: usage error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from collections.abc import Callable

LIBRARY_COMMANDS = ("--check-library", "--repair-library", "--migrate-library")
REMOVED_COMMANDS = {
    "--rollback-library": (
        "openbox --rollback-library was removed in 3.0. "
        "Use OpenBox 2.1.0 to restore library.v6.backup.json."
    ),
}
EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_REFUSED = 3
EXIT_USAGE = 4

USAGE = (
    "usage: openbox --check-library | --repair-library [--refresh-backup] | "
    "--migrate-library"
)


def is_library_command(argv: list[str]) -> bool:
    return any(arg in LIBRARY_COMMANDS or arg in REMOVED_COMMANDS for arg in argv)


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(
    argv: list[str],
    *,
    data_dir: Path | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    from pkg.platform_compat import resolved_data_dir
    from pkg.state import library_v7
    from state_store import JsonStateStore, StateCorruptError

    del input_fn
    removed = [arg for arg in argv if arg in REMOVED_COMMANDS]
    if removed:
        print(REMOVED_COMMANDS[removed[0]], file=sys.stderr)
        return EXIT_USAGE
    commands = [arg for arg in argv if arg in LIBRARY_COMMANDS]
    if len(commands) != 1:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    command = commands[0]
    directory = Path(data_dir) if data_dir is not None else resolved_data_dir()
    try:
        if command == "--check-library":
            report = library_v7.check_library(directory)
            _print(report)
            return int(report["exit_code"])
        if command == "--repair-library":
            _print(library_v7.repair_library(directory, refresh_backup="--refresh-backup" in argv))
            return EXIT_OK
        if library_v7.server_running(directory):
            raise library_v7.RollbackRefused("OpenBox is running. Quit OpenBox before migrating the library.")
        store = JsonStateStore(directory / library_v7.PRIMARY_NAME)
        state, report = store.migrate_primary_to_v7()
        _print({"migrated": state is not None, "report": report})
        return EXIT_OK
    except (library_v7.RollbackRefused, StateCorruptError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
