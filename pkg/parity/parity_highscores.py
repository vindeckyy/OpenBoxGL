"""MAME high score discovery and table parsing (P11.5).

MAME stores ``.hi`` files as opaque binary data. OpenBox does not guess that
layout: a table is parsed only through the optional ``hi2txt`` tool, whose
JSON output names its columns. Without the tool (or when it cannot parse the
file) the caller still gets the file metadata and a human-readable message,
and the existing export/import path keeps working.

All subprocesses are argument lists (never a shell) with a 10 s timeout, and
``table`` resolves the file through ``contained_path`` so a symlink cannot
make the reader open a file outside the MAME high score directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from parity_integrations import mame_highscore_path  # noqa: E402
from pkg.platform_compat import contained_path  # noqa: E402

PARSE_TIMEOUT = 10
MAX_ROWS = 100
MAX_DISCOVERY_FILES = 500
_EXECUTABLES = ("hi2txt", "hi2txt.exe")


def _resolve_tool(which) -> str:
    for name in _EXECUTABLES:
        try:
            found = which(name)
        except Exception:  # noqa: BLE001 - a broken PATH lookup is "not installed"
            found = None
        if found:
            return str(found)
    return ""


def _metadata(path: Path) -> dict:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "name": path.name,
        "path": str(path),
        "size": size,
        "label": path.stem,
    }


def _result(path, rom_name, *, parsed=False, scores=None, message="") -> dict:
    meta = _metadata(path)
    return {
        **meta,
        "rom_name": rom_name,
        "source": meta["path"],
        "parsed": parsed,
        "scores": scores or [],
        "message": message,
    }


def discover(home=None) -> dict:
    """List high score files under the MAME ``hi`` directory.

    Bounded to :data:`MAX_DISCOVERY_FILES` entries; only regular files are
    returned, sorted by name. A missing directory yields an empty list.
    """
    root = Path(mame_highscore_path(home))
    files = []
    try:
        entries = sorted(root.glob("*"), key=lambda item: item.name.casefold())
    except OSError:
        entries = []
    for path in entries:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        files.append(_metadata(path))
        if len(files) >= MAX_DISCOVERY_FILES:
            break
    return {"root": str(root), "count": len(files), "files": files}


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _run_once(run, command, timeout):
    try:
        return run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return SimpleNamespace(
            returncode=-1, stdout="", stderr=f"hi2txt timed out after {timeout}s."
        )
    except (OSError, ValueError) as error:
        return SimpleNamespace(returncode=-1, stdout="", stderr=str(error))


def _score_row(raw_rank, raw_score, raw_name=""):
    if isinstance(raw_rank, str):
        raw_rank = raw_rank.strip()
    if isinstance(raw_score, str):
        raw_score = raw_score.replace(",", "").strip()
    try:
        rank = int(raw_rank)
        score = int(raw_score)
    except (TypeError, ValueError):
        return None
    name = str(raw_name).strip()[:200]
    return {"rank": rank, "score": score, "name": name}


def _parse_json_table(text):
    """Parse hi2txt's JSON array of score rows."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    scores = []
    for row in payload[:MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        parsed = _score_row(
            row.get("rank", row.get("Rank")),
            row.get("score", row.get("Score")),
            row.get("name", row.get("Name", "")),
        )
        if parsed is None:
            continue
        scores.append(parsed)
        if len(scores) >= MAX_ROWS:
            break
    return scores or None


def _pipe_columns(header):
    columns = {}
    for index, cell in enumerate(header):
        label = cell.strip().casefold()
        if label in ("rank", "place", "position"):
            columns.setdefault("rank", index)
        elif label in ("score", "points"):
            columns.setdefault("score", index)
        elif label in ("name", "player", "initials"):
            columns.setdefault("name", index)
    return columns if {"rank", "score"} <= set(columns) else None


def _parse_pipe_table(text):
    """Parse hi2txt's fallback pipe-delimited table."""
    columns = None
    scores = []
    for line in str(text or "").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        header = _pipe_columns(cells)
        if header:
            columns = header
            continue
        if columns:
            rank_cell = cells[columns["rank"]] if columns["rank"] < len(cells) else ""
            score_cell = cells[columns["score"]] if columns["score"] < len(cells) else ""
            name_index = columns.get("name")
            name_cell = "|".join(cells[name_index:]) if name_index is not None and name_index < len(cells) else ""
        else:
            rank_cell, score_cell = cells[0], cells[1]
            name_cell = "|".join(cells[2:])
        parsed = _score_row(rank_cell, score_cell, name_cell)
        if parsed is None:
            continue
        scores.append(parsed)
        if len(scores) >= MAX_ROWS:
            break
    return scores or None


def _parse_table(text):
    """Parse hi2txt output as JSON, then as a pipe-delimited table.

    Returns ``(scores, None)`` when at least one valid row is present, else
    ``(None, message)``. Rows are capped at :data:`MAX_ROWS`.
    """
    scores = _parse_json_table(text)
    if scores:
        return scores, None
    scores = _parse_pipe_table(text)
    if scores:
        return scores, None
    if str(text or "").lstrip().startswith(("[", "{")):
        return None, "hi2txt returned no usable score rows."
    return None, "hi2txt did not return a usable score table."


def parse(path, rom_name="", *, which=shutil.which, run=subprocess.run,
          timeout=PARSE_TIMEOUT) -> dict:
    """Parse one high score file through ``hi2txt``.

    ``which`` and ``run`` are injectable for tests. The command is the
    documented ``hi2txt --format json <rom_name> <path>`` form. A missing
    tool, timeout, non-zero exit, or output without a JSON score array returns
    ``parsed: False`` with the file metadata and a message; no binary layout is
    ever guessed.
    """
    file_path = Path(path).expanduser()
    rom = str(rom_name or file_path.stem).strip()
    if not file_path.is_file():
        return _result(file_path, rom, message="High score file not found.")
    tool = _resolve_tool(which)
    if not tool:
        return _result(file_path, rom, message="hi2txt is not installed.")
    command = [tool, "--format", "json", rom, str(file_path)]
    result = _run_once(run, command, timeout)
    stdout = _text(getattr(result, "stdout", ""))
    stderr = _text(getattr(result, "stderr", ""))
    returncode = int(getattr(result, "returncode", 0) or 0)
    if returncode != 0:
        detail = (stderr.strip() or stdout.strip() or f"hi2txt exited with status {returncode}.")[:300]
        return _result(file_path, rom, message=detail)
    scores, error = _parse_table(stdout)
    if scores is None:
        detail = (stderr.strip() or stdout.strip())[:300]
        message = f"{error} {detail}".strip() if detail else error
        return _result(file_path, rom, message=message[:300])
    return _result(file_path, rom, parsed=True, scores=scores)


def table(game, home=None, *, which=shutil.which, run=subprocess.run) -> dict:
    """Return the parsed high score table for a game.

    The ROM name comes from ``rom_name`` or the game path stem; the matching
    ``<rom>.hi`` file is resolved inside the MAME ``hi`` directory with
    containment enforced. A missing game/ROM/file yields ``parsed: False``.
    """
    game = game or {}
    rom = str(game.get("rom_name") or Path(str(game.get("path") or "")).stem).strip()
    root = Path(mame_highscore_path(home))
    if not rom:
        return {
            "source": str(root),
            "path": "",
            "name": "",
            "size": 0,
            "label": "",
            "rom_name": "",
            "parsed": False,
            "scores": [],
            "message": "No ROM name is available for this game.",
        }
    matches = []
    try:
        for candidate in sorted(root.glob("*"), key=lambda item: item.name.casefold()):
            if candidate.suffix.casefold() != ".hi":
                continue
            if candidate.stem.casefold() != rom.casefold():
                continue
            matches.append(candidate)
    except OSError:
        matches = []
    if not matches:
        return {
            "source": str(root),
            "path": "",
            "name": "",
            "size": 0,
            "label": "",
            "rom_name": rom,
            "parsed": False,
            "scores": [],
            "message": "No high score file was found for this game.",
        }
    try:
        target = contained_path(root, matches[0].name)
    except Exception as error:  # noqa: BLE001 - refusal is reported, not raised
        return {
            "source": str(root),
            "path": "",
            "name": matches[0].name,
            "size": 0,
            "label": matches[0].stem,
            "rom_name": rom,
            "parsed": False,
            "scores": [],
            "message": f"High score file is outside the MAME directory: {error}",
        }
    return parse(target, rom, which=which, run=run)
