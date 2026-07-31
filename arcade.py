"""MAME and FinalBurn full-set import."""

import io
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


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
            catalog[name.casefold()] = {
                "name": name,
                "description": description,
                "cloneof": element.get("cloneof", "").strip(),
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
    if path.suffix.casefold() != ".zip":
        return set()
    try:
        with zipfile.ZipFile(path) as archive:
            return {PurePosixPath(name).name for name in archive.namelist()}
    except zipfile.BadZipFile:
        return set()


def import_arcade(folder, dat_path="", command="", source="MAME", catalog=None):
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError("The arcade ROM folder does not exist.")
    source = "FinalBurn Neo" if source == "FinalBurn Neo" else "MAME"
    archives = {
        path.stem.casefold(): path
        for path in root.iterdir()
        if path.is_file() and path.suffix.casefold() in {".zip", ".7z"}
    }
    catalog = catalog if catalog is not None else load_catalog(dat_path, source)
    if not command:
        binary = shutil.which("mame" if source == "MAME" else "fbneo")
        if not binary:
            raise FileNotFoundError(f"{source} is not installed. Enter a launch command for the configured emulator.")
        command = shlex.join([binary, "-rompath", str(root), "{rom_name}"]) if source == "MAME" else shlex.join([binary, "{path}"])
    if not shlex.split(command):
        raise ValueError("The arcade launch command is empty.")
    games = []
    for key, record in catalog.items():
        own = archives.get(key)
        parent = archives.get(record["cloneof"].casefold()) if record["cloneof"] else None
        archive = own or parent
        if not archive:
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
        })
    return games
