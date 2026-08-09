#!/usr/bin/env python3
from emulators import emulator_status, install_emulator


class Result:
    returncode = 0
    stdout = ""
    stderr = ""


def test():
    calls = []
    def run(args, **_):
        calls.append(args)
        return Result()
    statuses = emulator_status(run=run, which=lambda name: f"/usr/bin/{name}" if name in {"flatpak", "dolphin-emu"} else None)
    dolphin = next(item for item in statuses if item["name"] == "Dolphin")
    assert dolphin["mode"] == "native" and dolphin["profiles"]["Wii"].startswith("/usr/bin/dolphin-emu")
    profiles = install_emulator("org.ppsspp.PPSSPP", run=run, which=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    assert profiles["PSP"].startswith("/usr/bin/flatpak run org.ppsspp.PPSSPP")
    assert calls[-1][-1] == "org.ppsspp.PPSSPP"
    print("emulator self-test: ok")


if __name__ == "__main__":
    test()
