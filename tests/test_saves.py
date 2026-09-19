#!/usr/bin/env python3
import os
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saves import backup_saves, discover_save_paths, list_backups, restore_saves  # noqa: E402


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
        file.write_text("after", encoding="utf-8")
        restore_saves(game, root / "backups", archive.name)
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
    print("save self-test: ok")


if __name__ == "__main__":
    test()
