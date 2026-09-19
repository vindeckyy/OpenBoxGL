"""Command validation and execution helpers for OpenBox.

Extracted from webapp_state.py to keep that module a thin re-export shim.
"""

import logging
from pathlib import Path
import subprocess

from openbox import load_state
from pkg.platform_compat import launch_kwargs, split_command

LOGGER = logging.getLogger("openbox")


def clean_commands(commands):
    if not isinstance(commands, list) or len(commands) > 25:
        raise ValueError("Application commands must be a list of at most 25 entries.")
    clean = []
    for command in commands:
        command = str(command).strip()
        if command:
            if not split_command(command):
                raise ValueError("Application command is empty.")
            clean.append(command)
    return clean


def run_configured_commands(key):
    for command in load_state().get("settings", {}).get(key, []):
        try:
            args = split_command(command)
            if not args:
                continue
            args[0] = str(Path(args[0]).expanduser())
            subprocess.Popen(args, **launch_kwargs())
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
            LOGGER.warning("run_configured_commands: %s", e)
