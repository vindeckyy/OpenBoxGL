"""Centralized launch command placeholder replacement for OpenBox.

Single source of truth for ``{token}`` → value mapping used by
``openbox.build_launch``, ``pkg.state.imports._filled_launch_command``,
and ``pkg.parity.parity_emulator_defs.build_launch_command``.
"""

from __future__ import annotations

import shlex
from pathlib import Path


def _build_context(game, *, path=None, data_dir="", emulator_dir=""):
    """Return a flat dict of every token value needed by :data:`PLACEHOLDERS`.

    *game* is any mapping with at least ``name`` (or the caller pre-populates
    ``path`` via the keyword override).  Optional keyword arguments let callers
    supply values that are not stored on the game dict itself.
    """
    resolved = path if path is not None else str(game.get("path", ""))
    rom_p = Path(resolved)
    return {
        "path": resolved,
        "name": str(game.get("name", "")),
        "dir": str(rom_p.parent),
        "file": rom_p.name,
        "stem": rom_p.stem,
        "platform": str(game.get("platform", "")),
        "app_id": str(game.get("steam_app_id", "")),
        "heroic_app_id": str(game.get("heroic_app_id", "")),
        "lutris_id": str(game.get("lutris_id", "")),
        "rom_name": str(game.get("rom_name", "")),
        "data_dir": data_dir,
        "emulator_dir": emulator_dir,
    }


# PLACEHOLDERS maps each ``{token}`` to a function that extracts its value
# from a context dict built by :func:`_build_context`.
PLACEHOLDERS = {
    "{path}": lambda ctx: ctx["path"],
    "{ImagePath}": lambda ctx: ctx["path"],
    "{name}": lambda ctx: ctx["name"],
    "{Name}": lambda ctx: ctx["name"],
    "{dir}": lambda ctx: ctx["dir"],
    "{Dir}": lambda ctx: ctx["dir"],
    "{file}": lambda ctx: ctx["file"],
    "{File}": lambda ctx: ctx["file"],
    "{stem}": lambda ctx: ctx["stem"],
    "{FileNameWithoutExtension}": lambda ctx: ctx["stem"],
    "{platform}": lambda ctx: ctx["platform"],
    "{Platform}": lambda ctx: ctx["platform"],
    "{app_id}": lambda ctx: ctx["app_id"],
    "{heroic_app_id}": lambda ctx: ctx["heroic_app_id"],
    "{lutris_id}": lambda ctx: ctx["lutris_id"],
    "{rom_name}": lambda ctx: ctx["rom_name"],
    "{DataDir}": lambda ctx: ctx["data_dir"],
    "{EmulatorDir}": lambda ctx: ctx["emulator_dir"],
}


def apply_tokens(template, game, *, path=None, data_dir="", emulator_dir=""):
    """Replace all ``{token}`` placeholders in *template*.

    Parameters
    ----------
    template : str
        A launch command string that may contain ``{token}`` placeholders.
    game : dict
        Game (or definition) mapping supplying ``name``, ``platform``, etc.
    path : str, optional
        Override for the resolved ROM/executable path (e.g. after archive
        extraction).  Defaults to ``game["path"]``.
    data_dir : str, optional
        Value for ``{DataDir}`` — typically ``DATA.parent``.
    emulator_dir : str, optional
        Value for ``{EmulatorDir}`` — the directory containing the emulator
        binary.

    Returns
    -------
    str
        *template* with every recognised ``{token}`` replaced.
    """
    ctx = _build_context(game, path=path, data_dir=data_dir, emulator_dir=emulator_dir)
    result = template
    for token, extractor in PLACEHOLDERS.items():
        result = result.replace(token, extractor(ctx))
    return result


def build_launch_args(template, game, *, path=None, data_dir="", emulator_dir=""):
    """Shlex-split *template* and replace ``{token}`` placeholders in each arg.

    Parameters match :func:`apply_tokens`.

    Returns
    -------
    list[str]
        The argument list with all recognised tokens substituted.
    """
    try:
        parts = shlex.split(str(template))
    except ValueError:
        parts = str(template).split()
    ctx = _build_context(game, path=path, data_dir=data_dir, emulator_dir=emulator_dir)
    result = []
    for part in parts:
        for token, extractor in PLACEHOLDERS.items():
            part = part.replace(token, extractor(ctx))
        result.append(part)
    return result
