"""Optional Ludusavi and Hoard save-tool integrations."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def which_tool(name, which=shutil.which):
    return which(name)


def save_tool_status(which=shutil.which):
    return {
        "ludusavi": bool(which_tool("ludusavi", which)),
        "hoard": bool(which_tool("hoard", which)),
    }


def run_ludusavi(action, game_name="", path="", force=True, which=shutil.which, run=subprocess.run):
    binary = which_tool("ludusavi", which)
    if not binary:
        raise FileNotFoundError("ludusavi is not installed. Install it from https://github.com/mtkennerly/ludusavi")
    if action not in {"backup", "restore", "backups", "find"}:
        raise ValueError("Ludusavi action must be backup, restore, backups, or find.")
    command = [binary, action, "--api"]
    if force and action in {"backup", "restore"}:
        command.append("--force")
    if path:
        command.extend(["--path", str(Path(path).expanduser())])
    if game_name and action in {"backup", "restore", "find"}:
        command.append(str(game_name))
    result = run(command, capture_output=True, text=True, timeout=600)
    stdout = (result.stdout or "").strip()
    payload = {}
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {"raw": stdout}
    if result.returncode != 0 and not payload:
        raise RuntimeError((result.stderr or stdout or "ludusavi failed").strip())
    return {"ok": result.returncode == 0, "action": action, "result": payload, "stderr": (result.stderr or "").strip()}


def run_hoard(action, game_name="", which=shutil.which, run=subprocess.run):
    binary = which_tool("hoard", which)
    if not binary:
        raise FileNotFoundError("hoard is not installed. Install it from https://github.com/rleeon/hoard")
    if action not in {"backup", "restore", "list"}:
        raise ValueError("Hoard action must be backup, restore, or list.")
    if action == "list":
        command = [binary, "list"]
    elif game_name:
        command = [binary, action, str(game_name)]
    else:
        command = [binary, action]
    result = run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "hoard failed").strip())
    return {"ok": True, "action": action, "output": (result.stdout or "").strip()}
