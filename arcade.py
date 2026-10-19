"""MAME and FinalBurn full-set import."""

import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from pkg.platform_compat import join_command, split_command


def parse_catalog(source):
    catalog = {}
    for _, element in ElementTree.iterparse(source, events=("end",)):
        if element.tag.rsplit("}", 1)[-1] not in {"machine", "game"}:
            continue
        name = element.get("name", "").strip()
        if name and element.get("runnable", "yes") != "no" and element.get("isbios", "no") != "yes" and element.get("isdevice", "no") != "yes":
            description = next((
                child.text.strip() for child in element
                if child.tag.rsplit("}", 1)[-1] == "description" and child.text
            ), name)
            display = next((
                child for child in element
                if child.tag.rsplit("}", 1)[-1] == "display"
            ), None)
            input_node = next((
                child for child in element
                if child.tag.rsplit("}", 1)[-1] == "input"
            ), None)
            controls = {
                key: (input_node.get(key, "") if input_node is not None else "")
                for key in ("trackball", "lightgun", "paddle", "spinner", "keyboard", "mouse")
            }
            players = element.get("players", "")
            if not players and input_node is not None:
                players = input_node.get("players", "")
            catalog[name.casefold()] = {
                "name": name,
                "description": description,
                "cloneof": element.get("cloneof", "").strip(),
                "players": str(players or "").strip(),
                "vertical": bool(
                    display is not None and (
                        str(display.get("rotate", "0")).strip() not in {"", "0"}
                        or str(display.get("orientation", "")).strip().casefold() == "vertical"
                    )
                ),
                "requires_chd": any(
                    child.tag.rsplit("}", 1)[-1] == "disk" for child in element
                ),
                "controls": controls,
                "merged_roms": {
                    child.get("name", "")
                    for child in element
                    if child.tag.rsplit("}", 1)[-1] == "rom" and child.get("merge")
                },
            }
        element.clear()
    return catalog


def load_catalog(dat_path="", source="MAME"):
    if dat_path:
        path = Path(dat_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError("The arcade DAT/XML file does not exist.")
        with path.open("rb") as file:
            return parse_catalog(file)
    if source != "MAME" or not (binary := shutil.which("mame")):
        raise FileNotFoundError("Choose a DAT/XML file, or install MAME so OpenBox can run mame -listxml.")
    process = subprocess.Popen([binary, "-listxml"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        output, _ = process.communicate(timeout=300)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise RuntimeError("MAME catalog generation timed out.") from error
    if len(output) > 256 * 1024 * 1024:
        raise RuntimeError("MAME catalog is too large.")
    return_code = process.returncode
    if return_code != 0:
        raise RuntimeError("MAME could not produce its XML catalog.")
    return parse_catalog(io.BytesIO(output))


def zip_members(path):
    p = path if isinstance(path, Path) else Path(path)
    if p.suffix.casefold() != ".zip":
        return set()
    try:
        with zipfile.ZipFile(p) as archive:
            return {PurePosixPath(name).name for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError):
        return set()


def import_arcade(
    folder,
    dat_path="",
    command="",
    source="MAME",
    catalog=None,
    players="any",
    skip_chd=False,
    skip_inputs=(),
):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError("The arcade ROM folder does not exist.")
    source = "FinalBurn Neo" if source == "FinalBurn Neo" else "MAME"
    players = str(players or "any").strip().lower()
    if players not in {"any", "2", "4"}:
        raise ValueError("Player filter must be any, 2, or 4.")
    skip_inputs = {str(value).strip().lower() for value in (skip_inputs or []) if str(value).strip()}
    allowed_inputs = {"trackball", "lightgun", "paddle", "spinner", "keyboard", "mouse"}
    unknown_inputs = skip_inputs - allowed_inputs
    if unknown_inputs:
        raise ValueError(f"Unknown input filter: {sorted(unknown_inputs)[0]}")
    archives = {}
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=True):
                        name = entry.name
                        idx = name.rfind(".")
                        if idx != -1 and name[idx:].casefold() in {".zip", ".7z"}:
                            stem = name[:idx].casefold()
                            archives[stem] = Path(entry.path)
                except OSError:
                    continue
    except OSError:
        pass
    catalog = catalog if catalog is not None else load_catalog(dat_path, source)
    if not command:
        binary = shutil.which("mame" if source == "MAME" else "fbneo")
        if not binary:
            raise FileNotFoundError(f"{source} is not installed. Enter a launch command for the configured emulator.")
        command = join_command([binary, "-rompath", str(root), "{rom_name}"]) if source == "MAME" else join_command([binary, "{path}"])
    if not split_command(command):
        raise ValueError("The arcade launch command is empty.")
    games = []
    for key, record in catalog.items():
        own = archives.get(key)
        parent = archives.get(record["cloneof"].casefold()) if record["cloneof"] else None
        archive = own or parent
        if not archive:
            continue
        if players != "any":
            try:
                supported = int(record.get("players", "0") or 0)
            except (TypeError, ValueError):
                supported = 0
            if supported < int(players):
                continue
        if skip_chd and record.get("requires_chd"):
            continue
        controls = record.get("controls") or {}
        if skip_inputs and any(controls.get(name) for name in skip_inputs):
            continue
        if record["cloneof"] and not own:
            set_type = "merged"
        elif record["cloneof"] and record["merged_roms"] - zip_members(own):
            set_type = "split"
        elif record["cloneof"]:
            set_type = "non-merged"
        else:
            set_type = "parent"
        games.append({
            "name": record["description"],
            "platform": "Arcade",
            "source": source,
            "collection": source,
            "path": str(archive),
            "launch": command,
            "rom_name": record["name"],
            "clone_of": record["cloneof"],
            "set_type": set_type,
            "players": record.get("players", ""),
            "vertical": bool(record.get("vertical")),
            "requires_chd": bool(record.get("requires_chd")),
        })
    return games

