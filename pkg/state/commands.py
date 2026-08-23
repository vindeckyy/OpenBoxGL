"""Command validation and execution helpers for OpenBox.

Extracted from webapp_state.py to keep that module a thin re-export shim.
"""

import logging
from pathlib import Path
import shlex
import subprocess

from openbox import load_state

LOGGER = logging.getLogger("openbox")


def clean_commands(commands):
    if not isinstance(commands, list) or len(commands) > 25:
        raise ValueError("Application commands must be a list of at most 25 entries.")
    clean = []
    for command in commands:
        command = str(command).strip()
        if command:
            if not shlex.split(command):
                raise ValueError("Application command is empty.")
            clean.append(command)
    return clean


def run_configured_commands(key):
    for command in load_state().get("settings", {}).get(key, []):
        try:
            args = shlex.split(command)
            args[0] = str(Path(args[0]).expanduser())
            subprocess.Popen(args, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as e:
            LOGGER.warning("run_configured_commands: %s", e)
