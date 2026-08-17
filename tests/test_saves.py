#!/usr/bin/env python3
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from saves import backup_saves, discover_save_paths, list_backups, restore_saves


def test():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        save = root / "saves"
        save.mkdir()
        file = save / "slot1.sav"
        file.write_text("before")
        game = {"name": "Real Game", "path": "/games/real", "save_paths": [str(save)]}
        archive = backup_saves(game, root / "backups")
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
        file.write_text("after")
        restore_saves(game, root / "backups", archive.name)
        assert file.read_text() == "before"
        assert len(list_backups(game, root / "backups")) == 2
        single = root / "single.sav"
        single.write_text("one")
        file_game = {"name":"File Game", "path":"/games/file", "save_paths":[str(single)]}
        file_backup = backup_saves(file_game, root / "backups")
        single.write_text("two")
        restore_saves(file_game, root / "backups", file_backup.name)
        assert single.read_text() == "one"
        steam_save = root / ".local/share/Steam/userdata/1/42/remote"
        steam_save.mkdir(parents=True)
        assert discover_save_paths({"name":"Steam Game","steam_app_id":"42"}, root)[0]["path"] == str(steam_save)

        # A relative save_paths entry must back up and restore against the
        # resolved path, not the process cwd.
        relative = root / "relative-saves"
        relative.mkdir()
        rel_file = relative / "slot.sav"
        rel_file.write_text("rel")
        rel_path = os.path.relpath(relative, Path.cwd())
        rel_game = {"name":"Relative", "path":"/games/rel", "save_paths":[rel_path]}
        rel_archive = backup_saves(rel_game, root / "backups")
        rel_file.write_text("rel2")
        restore_saves(rel_game, root / "backups", rel_archive.name)
        assert rel_file.read_text() == "rel"
    print("save self-test: ok")


if __name__ == "__main__":
    test()
