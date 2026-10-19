#!/usr/bin/env python3
import io
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from arcade import import_arcade, parse_catalog


def test():
    xml = b"""<mame>
      <machine name="parent"><description>Parent Game</description><rom name="base.bin"/></machine>
      <machine name="merged" cloneof="parent"><description>Merged Clone</description><rom name="base.bin" merge="base.bin"/></machine>
      <machine name="split" cloneof="parent"><description>Split Clone</description><rom name="base.bin" merge="base.bin"/><rom name="split.bin"/></machine>
      <machine name="full" cloneof="parent"><description>Full Clone</description><rom name="base.bin" merge="base.bin"/></machine>
      <machine name="bios" isbios="yes"><description>BIOS</description></machine>
    </mame>"""
    catalog = parse_catalog(io.BytesIO(xml))
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name, members in {
            "parent.zip": ["base.bin"],
            "split.zip": ["split.bin"],
            "full.zip": ["base.bin"],
        }.items():
            with zipfile.ZipFile(root / name, "w") as archive:
                for member in members:
                    archive.writestr(member, b"rom")
        games = import_arcade(root, command="mame {rom_name}", catalog=catalog)
        types = {game["rom_name"]: game["set_type"] for game in games}
        assert types == {"parent":"parent", "merged":"merged", "split":"split", "full":"non-merged"}
        assert all(game["name"] != "BIOS" for game in games)
    print("arcade self-test: ok")


def test_filters():
    xml = b"""<mame>
      <machine name="four"><description>Four Player</description><input players="4" trackball="1"/><rom name="four.bin"/></machine>
      <machine name="two"><description>Two Player</description><input players="2"/><rom name="two.bin"/></machine>
      <machine name="chd"><description>CHD Game</description><input players="2"/><disk name="disk"/><rom name="chd.bin"/></machine>
    </mame>"""
    catalog = parse_catalog(io.BytesIO(xml))
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name in ("four", "two", "chd"):
            with zipfile.ZipFile(root / f"{name}.zip", "w") as archive:
                archive.writestr(f"{name}.bin", b"rom")
        games = import_arcade(root, command="mame {rom_name}", catalog=catalog)
        assert {game["rom_name"] for game in games} == {"four", "two", "chd"}
        games = import_arcade(
            root,
            command="mame {rom_name}",
            catalog=catalog,
            players="2",
            skip_chd=True,
            skip_inputs=("trackball",),
        )
        assert [game["rom_name"] for game in games] == ["two"]
        try:
            import_arcade(root, command="mame {rom_name}", catalog=catalog, skip_inputs=("keyboard", "unknown"))
        except ValueError as error:
            assert "unknown" in str(error)
        else:
            raise AssertionError("unknown input filter should raise")
    print("arcade filters: ok")


if __name__ == "__main__":
    test()
    test_filters()
