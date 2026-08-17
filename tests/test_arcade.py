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


if __name__ == "__main__":
    test()
