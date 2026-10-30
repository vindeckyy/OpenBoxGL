"""Command-line library maintenance: check, repair, migrate, and roll back.

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

LIBRARY_COMMANDS = ("--check-library", "--repair-library", "--migrate-library", "--rollback-library")
EXIT_OK = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_REFUSED = 3
EXIT_USAGE = 4

USAGE = (
    "usage: openbox --check-library | --repair-library [--refresh-backup] | "
    "--migrate-library | --rollback-library [--yes]"
)


def is_library_command(argv: list[str]) -> bool:
    return any(arg in LIBRARY_COMMANDS for arg in argv)


def _stdin_is_interactive() -> bool:
    """True when stdin can carry a typed answer to a prompt.

    A child started without a console (Windows hands one a NUL device) and a
    closed stdin cannot answer: reading them raises EOFError, so asking there
    would abort the command with a traceback instead of refusing cleanly.
    """
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (OSError, ValueError):
        return False


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
        if command == "--migrate-library":
            if library_v7.server_running(directory):
                raise library_v7.RollbackRefused("OpenBox is running. Quit OpenBox before migrating the library.")
            store = JsonStateStore(directory / library_v7.PRIMARY_NAME)
            state, report = store.migrate_primary_to_v7()
            _print({"migrated": state is not None, "report": report})
            return EXIT_OK
        confirmed = "--yes" in argv
        if not confirmed and _stdin_is_interactive():
            preview = library_v7.rollback(directory, confirmed=False)
            _print(preview)
            try:
                answer = input_fn("Type ROLLBACK to restore the v6 copy: ").strip()
            except (EOFError, OSError):
                # stdin ended without an answer: a console-less child (Windows
                # hands one a NUL device) or a closed pipe. The confirmation
                # cannot be given here, so refuse and say how to proceed.
                print("Re-run with --yes to roll back. Nothing was changed.", file=sys.stderr)
                return EXIT_REFUSED
            if answer != "ROLLBACK":
                print("Rollback cancelled; nothing was changed.", file=sys.stderr)
                return EXIT_REFUSED
            confirmed = True
        if not confirmed:
            _print(library_v7.rollback(directory, confirmed=False))
            print("Re-run with --yes to roll back. Nothing was changed.", file=sys.stderr)
            return EXIT_REFUSED
        _print(library_v7.rollback(directory, confirmed=True))
        return EXIT_OK
    except (library_v7.RollbackRefused, StateCorruptError) as error:
        print(str(error), file=sys.stderr)
        return EXIT_REFUSED
