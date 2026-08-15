#!/usr/bin/env python3
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import mock

import importers
from importers import import_heroic, import_lutris, import_steam, vdf_values


def test():
    assert vdf_values('"appid" "42"\n"name" "Real Game"') == {"appid": "42", "name": "Real Game"}

    # Steam flatpak: an uninstalled flatpak must fall through to xdg-open,
    # never fabricate a flatpak launch command.
    failed = type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
    which = lambda name: "/usr/bin/flatpak" if name == "flatpak" else "/usr/bin/xdg-open" if name == "xdg-open" else None
    with mock.patch.object(importers.shutil, "which", side_effect=which), \
         mock.patch.object(importers.subprocess, "run", return_value=failed):
        binary, command = importers.steam_command()
    assert "xdg-open" in command, "uninstalled Steam flatpak must not produce a flatpak command"

    # Lutris flatpak: uninstalled flatpak must raise, not run a fake command.
    with mock.patch.object(importers.shutil, "which", side_effect=which), \
         mock.patch.object(importers.subprocess, "run", return_value=failed):
        try:
            import_lutris(Path("/tmp"), run=lambda *args, **kwargs: failed, which=which)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("uninstalled Lutris flatpak must not import")

    with TemporaryDirectory() as directory:
        steamapps = Path(directory) / ".local/share/Steam/steamapps"
        steamapps.mkdir(parents=True)
        (steamapps / "appmanifest_42.acf").write_text(
            '"AppState"\n{\n"appid" "42"\n"name" "Real Game"\n"installdir" "RealGame"\n}'
        )
        games = import_steam(Path(directory))
        assert len(games) == 1
        assert games[0]["steam_app_id"] == "42"
        assert games[0]["launch"]
        heroic = Path(directory) / ".config/heroic"
        (heroic / "legendaryConfig/legendary").mkdir(parents=True)
        (heroic / "gog_store").mkdir()
        (heroic / "nile_config").mkdir()
        (heroic / "legendaryConfig/legendary/installed.json").write_text(
            '{"epic-id":{"title":"Epic Game","install_path":"/games/epic"}}'
        )
        (heroic / "gog_store/installed.json").write_text(
            '{"gog-id":{"title":"GOG Game","install_path":"/games/gog"}}'
        )
        (heroic / "nile_config/installed.json").write_text(
            '{"amazon-id":{"title":"Amazon Game","install_path":"/games/amazon"}}'
        )
        heroic_games = import_heroic(Path(directory))
        assert {game["source"] for game in heroic_games} == {"Epic", "GOG", "Amazon"}
        assert all("heroic://launch/" in game["launch"] for game in heroic_games)
        class Result:
            stdout = '[{"id":7,"name":"EA Game","installed":true,"service":"ea app","runner":"wine"}]'
        lutris_games = import_lutris(
            Path(directory),
            run=lambda *args, **kwargs: Result(),
            which=lambda name: "/usr/bin/lutris" if name == "lutris" else None,
        )
        assert lutris_games[0]["source"] == "EA"
        assert "lutris:rungameid/{lutris_id}" in lutris_games[0]["launch"]
    print("importer self-test: ok")


if __name__ == "__main__":
    test()
