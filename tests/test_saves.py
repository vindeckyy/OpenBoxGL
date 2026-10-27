#!/usr/bin/env python3
import os
import json
import stat
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saves import (  # noqa: E402
    backup_metadata,
    backup_saves,
    discover_save_paths,
    file_sha256,
    inspect_save_backup,
    list_backup_metadata,
    list_backups,
    preview_restore,
    restore_saves,
    save_source_for,
)


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        save = root / "saves"
        save.mkdir()
        file = save / "slot1.sav"
        file.write_text("before", encoding="utf-8")
        game = {"name": "Real Game", "path": "/games/real", "save_paths": [str(save)]}
        archive = backup_saves(game, root / "backups")
        if os.name != "nt":
            assert stat.S_IMODE(archive.stat().st_mode) == 0o600
            assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
        assert manifest["entries"][0]["sha256"]
        assert manifest["entries"][0]["relative"] == "slot1.sav"
        inspected = inspect_save_backup(game, root / "backups", archive.name)
        assert inspected["files"][0]["archive"].startswith("roots/0/")
        file.write_text("after", encoding="utf-8")
        dry = restore_saves(game, root / "backups", archive.name, dry_run=True)
        assert dry["dry_run"] and file.read_text(encoding="utf-8") == "after"
        only = inspected["files"][0]["archive"]
        restore_saves(game, root / "backups", archive.name, selected=[only])
        assert file.read_text(encoding="utf-8") == "before"
        assert len(list_backups(game, root / "backups")) == 2
        single = root / "single.sav"
        single.write_text("one", encoding="utf-8")
        file_game = {"name":"File Game", "path":"/games/file", "save_paths":[str(single)]}
        file_backup = backup_saves(file_game, root / "backups")
        single.write_text("two", encoding="utf-8")
        restore_saves(file_game, root / "backups", file_backup.name)
        assert single.read_text(encoding="utf-8") == "one"
        steam_save = root / ".local/share/Steam/userdata/1/42/remote"
        steam_save.mkdir(parents=True)
        assert discover_save_paths({"name":"Steam Game","steam_app_id":"42"}, root)[0]["path"] == str(steam_save)

        # A relative save_paths entry resolves against the process cwd, so run
        # this part from the fixture root: Windows cannot spell a relative path
        # across drives, and the temp dir may sit on a different one.
        relative = root / "relative-saves"
        relative.mkdir()
        rel_file = relative / "slot.sav"
        rel_file.write_text("rel", encoding="utf-8")
        previous_cwd = os.getcwd()
        os.chdir(root)
        try:
            rel_game = {"name":"Relative", "path":"/games/rel", "save_paths":["relative-saves"]}
            rel_archive = backup_saves(rel_game, root / "backups")
            rel_file.write_text("rel2", encoding="utf-8")
            restore_saves(rel_game, root / "backups", rel_archive.name)
        finally:
            os.chdir(previous_cwd)
        assert rel_file.read_text(encoding="utf-8") == "rel"
    test_restore_with_root_missing_at_backup_time()
    test_restore_preview_add_overwrite_and_missing()
    test_restore_preview_bucket_cap()
    test_backup_metadata_round_trip()
    test_shared_memory_card_isolation()
    print("save self-test: ok")


def test_restore_with_root_missing_at_backup_time():
    # A configured root that did not exist at backup time is left out of the
    # manifest; the backup must still restore the roots it did capture.
    with TemporaryDirectory() as directory:
        root = Path(directory)
        present = root / "present"
        present.mkdir()
        (present / "slot.sav").write_text("before", encoding="utf-8")
        missing = root / "missing"
        game = {"name": "Two Roots", "path": "/games/two", "save_paths": [str(missing), str(present)]}
        archive = backup_saves(game, root / "backups")
        (present / "slot.sav").write_text("after", encoding="utf-8")
        restore_saves(game, root / "backups", archive.name)
        assert (present / "slot.sav").read_text(encoding="utf-8") == "before"
        assert not missing.exists()
        other = {"name": "Two Roots", "path": "/games/two", "save_paths": [str(missing)]}
        try:
            restore_saves(other, root / "backups", archive.name)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError("a backup for another root set must not restore")


def test_restore_preview_add_overwrite_and_missing():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        saves = root / "saves"
        saves.mkdir()
        changed = saves / "slot1.sav"
        changed.write_text("before", encoding="utf-8")
        same = saves / "slot2.sav"
        same.write_text("same", encoding="utf-8")
        game = {"name": "Preview", "path": "/games/preview", "save_paths": [str(saves)]}
        archive = backup_saves(game, root / "backups")
        preview = preview_restore(game, root / "backups", archive.name)
        assert preview["add"] == [] and preview["overwrite"] == []
        assert preview["conflict"] is False
        assert preview["unchanged"] == 2
        assert preview["remove"] == []
        assert preview["truncated"]["overwrite"] is False

        changed.write_text("after", encoding="utf-8")
        same.unlink()
        preview = preview_restore(game, root / "backups", archive.name)
        assert preview["conflict"] is True
        overwrite = preview["overwrite"]
        assert len(overwrite) == 1
        assert overwrite[0]["path"].endswith("slot1.sav")
        assert len(overwrite[0]["backup_sha"]) == 64
        assert overwrite[0]["disk_sha"] == file_sha256(changed)
        assert overwrite[0]["backup_sha"] != overwrite[0]["disk_sha"]
        added = preview["add"]
        assert len(added) == 1 and added[0]["path"].endswith("slot2.sav")
        assert len(added[0]["sha256"]) == 64
        assert preview["counts"] == {
            "add": 1, "overwrite": 1, "remove": 0, "missing": 0, "unchanged": 0,
        }

        # A manifest entry whose archive member vanished is reported as missing
        # instead of silently shrinking the restore set.
        rewritten = root / "rewritten.zip"
        with zipfile.ZipFile(root / "backups" / archive.parent.name / archive.name) as source:
            with zipfile.ZipFile(rewritten, "w") as target:
                for info in source.infolist():
                    if info.filename.endswith("slot2.sav"):
                        continue
                    target.writestr(info, source.read(info.filename))
        rewritten.replace(root / "backups" / archive.parent.name / archive.name)
        preview = preview_restore(game, root / "backups", archive.name)
        assert preview["counts"]["missing"] == 1
        assert preview["missing"][0]["archive"].endswith("slot2.sav")
        assert preview["missing"][0]["path"].endswith("slot2.sav")

        # Restore still only touches the selected members.
        inspected = inspect_save_backup(game, root / "backups", archive.name)
        only = inspected["files"][0]["archive"]
        restore_saves(game, root / "backups", archive.name, selected=[only])
        assert changed.read_text(encoding="utf-8") == "before"


def test_restore_preview_bucket_cap():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        saves = root / "saves"
        saves.mkdir()
        for index in range(205):
            (saves / f"slot{index:03d}.sav").write_text(f"value-{index}", encoding="utf-8")
        game = {"name": "Cap", "path": "/games/cap", "save_paths": [str(saves)]}
        archive = backup_saves(game, root / "backups")
        for path in saves.glob("*.sav"):
            path.unlink()
        preview = preview_restore(game, root / "backups", archive.name)
        assert preview["counts"]["add"] == 205
        assert len(preview["add"]) == 200
        assert preview["truncated"]["add"] is True
        assert preview["truncated"]["overwrite"] is False


def test_backup_metadata_round_trip():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        saves = root / "saves"
        saves.mkdir()
        (saves / "slot1.sav").write_text("data", encoding="utf-8")
        source_path = str(saves / "slot1.sav")
        game = {
            "name": "Metadata",
            "path": "/games/metadata",
            "emulator_adapter_id": "retroarch-snes",
            "save_paths": [source_path],
            "save_sources": {source_path: {"emulator_id": "retroarch", "core": "snes9x", "slot": "2"}},
        }
        archive = backup_saves(game, root / "backups", "pre-update")
        metadata = backup_metadata(game, root / "backups", archive.name)
        assert metadata["name"] == archive.name
        assert metadata["label"] == "pre-update"
        assert metadata["emulator_id"] == "retroarch-snes"
        assert metadata["core"] == "snes9x"
        assert metadata["slot"] == "2"
        assert metadata["size"] > 0 and metadata["mtime"] > 0
        listed = list_backup_metadata(game, root / "backups")
        assert [entry["name"] for entry in listed] == [archive.name]
        assert save_source_for(game, source_path) == {
            "emulator_id": "retroarch", "core": "snes9x", "slot": "2",
        }
        assert save_source_for(game, str(saves / "missing.sav")) == {
            "emulator_id": "", "core": "", "slot": "",
        }
        # An equivalent path spelling resolves to the recorded source.
        odd = f"{saves}/./slot1.sav"
        assert save_source_for(game, odd)["core"] == "snes9x"
        # Restore's safety backup records the scanned source, not just the
        # adapter id from the game record.
        (saves / "slot1.sav").write_text("changed", encoding="utf-8")
        restore_saves(game, root / "backups", archive.name)
        safety = next(item for item in list_backup_metadata(game, root / "backups") if item["label"] == "before-restore")
        assert safety["emulator_id"] == "retroarch"
        assert safety["core"] == "snes9x"
        assert safety["slot"] == "2"


def test_shared_memory_card_isolation():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        memcards = root / "PCSX2" / "memcards"
        memcards.mkdir(parents=True)
        card_a = memcards / "game-a.ps2"
        card_b = memcards / "game-b.ps2"
        card_a.write_text("a-before", encoding="utf-8")
        card_b.write_text("b-original", encoding="utf-8")
        game_a = {
            "name": "Game A",
            "path": "/roms/a.iso",
            "platform": "PlayStation 2",
            "save_paths": [str(card_a)],
            "save_sources": {str(card_a): {"emulator_id": "pcsx2", "core": "", "slot": ""}},
        }
        game_b = {
            "name": "Game B",
            "path": "/roms/b.iso",
            "platform": "PlayStation 2",
            "save_paths": [str(card_b)],
            "save_sources": {str(card_b): {"emulator_id": "pcsx2", "core": "", "slot": ""}},
        }
        archive = backup_saves(game_a, root / "backups")
        card_a.write_text("a-after", encoding="utf-8")
        preview = preview_restore(game_a, root / "backups", archive.name)
        overwritten = [entry["path"] for entry in preview["overwrite"]]
        assert len(overwritten) == 1 and overwritten[0].endswith("game-a.ps2")
        assert all(not path.endswith("game-b.ps2") for path in overwritten)
        restore_saves(game_a, root / "backups", archive.name)
        assert card_a.read_text(encoding="utf-8") == "a-before"
        assert card_b.read_text(encoding="utf-8") == "b-original"
        assert save_source_for(game_b, str(card_b))["emulator_id"] == "pcsx2"


def test_macos_save_discovery_uses_only_documented_folders():
    from unittest import mock

    import saves

    with TemporaryDirectory() as directory:
        home = Path(directory)
        library = home / "Library" / "Application Support"
        ppsspp = home / ".config" / "ppsspp" / "PSP" / "SAVEDATA"
        dolphin = library / "Dolphin" / "GC"
        linux_pcsx2 = home / ".config" / "PCSX2" / "memcards"
        steam_remote = library / "Steam" / "userdata" / "1" / "42" / "remote"
        for folder in (ppsspp, dolphin, linux_pcsx2, steam_remote):
            folder.mkdir(parents=True)
        retroarch = home / "Documents" / "RetroArch" / "saves"
        retroarch.mkdir(parents=True)
        (retroarch / "Sonic.srm").write_bytes(b"save")
        with mock.patch.object(saves, "steam_candidate_roots", return_value=[library / "Steam"]):
            psp = [c["path"] for c in saves._discover_save_paths_darwin({"name": "Game", "platform": "PSP"}, home)]
            gamecube = [c["path"] for c in saves._discover_save_paths_darwin({"name": "Game", "platform": "GameCube"}, home)]
            ps2 = saves._discover_save_paths_darwin({"name": "Game", "platform": "PlayStation 2"}, home)
            steam = [c["path"] for c in saves._discover_save_paths_darwin({"name": "Game", "steam_app_id": "42"}, home)]
            saturn = [c["path"] for c in saves._discover_save_paths_darwin({"name": "Sonic", "platform": "Sega Saturn"}, home)]
            with mock.patch.object(saves, "IS_MACOS", True), mock.patch.object(
                saves, "IS_WINDOWS", False
            ), mock.patch.object(saves.Path, "home", return_value=home):
                dispatched = [c["path"] for c in saves.discover_save_paths({"name": "Game", "platform": "PSP"})]
    assert psp == [str(ppsspp)]
    assert gamecube == [str(dolphin)]
    # PCSX2 has no verified macOS folder, so its Linux folder is not searched on macOS.
    assert ps2 == []
    assert steam == [str(steam_remote)]
    assert str(retroarch / "Sonic.srm") in saturn
    assert dispatched == [str(ppsspp)]


if __name__ == "__main__":
    test()
    test_macos_save_discovery_uses_only_documented_folders()
