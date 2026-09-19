import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import updates
from updates import (
    ASSET,
    RELEASE_API,
    TRUSTED_RELEASE_PREFIX,
    VERSION,
    check_update,
    install_update,
    load_checksum_file,
    verify_artifact,
    verify_release_signature,
    version_tuple,
)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def next_patch_version(value):
    major, minor, patch = version_tuple(value)
    return f"{major}.{minor}.{patch + 1}"


def main():
    assert version_tuple("v1.2.3") > version_tuple("1.2.2")
    assert RELEASE_API == "https://api.github.com/repos/vindeckyy/OpenBoxGL/releases/latest"
    assert TRUSTED_RELEASE_PREFIX == "https://github.com/vindeckyy/OpenBoxGL/releases/download/"
    latest = next_patch_version(VERSION)
    tag = f"v{latest}"
    payload = b"new appimage"
    digest = hashlib.sha256(payload).hexdigest()
    public_key = bytes.fromhex("03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8")
    signature = base64.b64decode("53/D1Z5YXcVxj9ZgLlTSt/jkeX9MDO53qddaj7bh64byCG1YW1/oTIbdiO8tNmoO7WaHkjkmM5SbYWusjIp6Dg==")
    signature_payload = {
        "algorithm": "ed25519",
        "artifact": ASSET,
        "digest_algorithm": "sha256",
        "digest": digest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    release = {
        "tag_name": tag,
        "html_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/tag/{tag}",
        "assets": [
            {
                "name": ASSET,
                "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}",
                "digest": f"sha256:{digest}",
            },
            {"name": f"{ASSET}.sha256", "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}.sha256"},
            {"name": f"{ASSET}.sig", "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/{ASSET}.sig"},
        ],
    }
    def opener(request, timeout=0):
        url = request.full_url
        if url.endswith(".sha256"):
            return Response(f"{digest}  {ASSET}\n".encode())
        if url.endswith(".sig"):
            return Response(json.dumps(signature_payload).encode())
        if url.endswith(ASSET):
            return Response(payload)
        return Response(json.dumps(release).encode())
    update = check_update(opener)
    assert update["available"] and update["latest"] == latest
    assert update["checksum"] == digest
    assert update["sig"] is True
    if os.name != "nt":
        # The AppImage channel replaces the running file in place.
        with mock.patch("updates._release_public_key", return_value=public_key):
            with tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / ASSET
                destination.write_bytes(b"old appimage")
                result = install_update(update, destination, opener)
                assert destination.read_bytes() == payload
                assert Path(result["backup"]).read_bytes() == b"old appimage"
    try:
        unsigned = dict(release, assets=[asset for asset in release["assets"] if not asset["name"].endswith(".sig")])
        check_update(lambda request, timeout=0: Response(json.dumps(unsigned).encode()))
        raise AssertionError("unsigned release should fail closed")
    except ValueError as error:
        assert "signature" in str(error).casefold()
    from updates import PLACEHOLDER_PUBLIC_KEY
    with mock.patch("updates._release_public_key", return_value=PLACEHOLDER_PUBLIC_KEY):
        try:
            verify_release_signature(update, digest, opener)
            raise AssertionError("placeholder release key should fail closed")
        except ValueError as error:
            assert "placeholder" in str(error).casefold()
    try:
        load_checksum_file(
            f"{TRUSTED_RELEASE_PREFIX}{tag}/{ASSET}.sha256",
            opener=lambda request, timeout=0: Response(b" \n"),
        )
        raise AssertionError("empty checksum should fail")
    except ValueError as error:
        assert "checksum" in str(error).casefold()

    # A pre-release tag parses without raising and is not "available".
    from updates import _version_key
    assert _version_key("1.2.3-rc1") < _version_key("1.2.3")
    assert _version_key("1.2.3+build") < _version_key("1.2.3")
    pre_release = dict(release, tag_name=f"v{latest}-rc1")
    def pre_opener(request, timeout=0):
        return Response(json.dumps(pre_release).encode())
    pre_update = check_update(pre_opener)
    assert pre_update["available"] is False

    # Malformed Ed25519 points must be rejected, never verified.
    from updates import _point_decompress, _verify_ed25519
    p = 2 ** 255 - 19
    for bad in (
        (p).to_bytes(32, "little"),          # y == p: out of range
        (2).to_bytes(32, "little"),          # not on the curve
        bytes(32),                           # y == 0: order-4 point
        (1).to_bytes(32, "little"),          # identity
        (p - 1).to_bytes(32, "little"),      # order-2 point
        # Order-8 points (libsodium/ZIP-215 blacklist), both sign bits.
        bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
        bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85"),
        bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
        bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
    ):
        try:
            _point_decompress(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid point accepted")
    for bad in (
        bytes(32),
        (1).to_bytes(32, "little"),
        (p).to_bytes(32, "little"),
        bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"),
        bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa"),
    ):
        try:
            result = _verify_ed25519(bad, bytes(64), b"msg")
        except ValueError:
            result = False
        assert result is False

    # Architecture-aware updater: ASSET follows the host arch, and a release
    # without the matching-arch artifact is refused (ADR 0024).
    from updates import _arch_asset, _current_arch
    asset_pattern = "OpenBox-{}-windows.zip" if os.name == "nt" else "OpenBox-{}.AppImage"
    assert _arch_asset("x86_64") == asset_pattern.format("x86_64")
    assert _arch_asset("aarch64") == asset_pattern.format("aarch64")
    assert _current_arch("x86_64") == "x86_64"
    assert _current_arch("amd64") == "x86_64"
    assert _current_arch("aarch64") == "aarch64"
    assert _current_arch("arm64") == "aarch64"
    # ASSET is bound at import time from the host arch, so an aarch64 host is
    # simulated by patching ASSET/SIGNATURE_ASSET directly. A release shipping
    # only the x86_64 AppImage must then be refused (matching asset absent).
    x86_only_release = {
        "tag_name": tag,
        "html_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/tag/{tag}",
        "assets": [
            {
                "name": "OpenBox-x86_64.AppImage",
                "browser_download_url": f"https://github.com/vindeckyy/OpenBoxGL/releases/download/{tag}/OpenBox-x86_64.AppImage",
                "digest": f"sha256:{digest}",
            },
        ],
    }
    with mock.patch("updates.ASSET", "OpenBox-aarch64.AppImage"), \
         mock.patch("updates.SIGNATURE_ASSET", "OpenBox-aarch64.AppImage.sig"):
        try:
            check_update(lambda request, timeout=0: Response(json.dumps(x86_only_release).encode()))
            raise AssertionError("aarch64 host should refuse an x86_64-only release")
        except ValueError as error:
            assert "asset" in str(error).casefold()

    # A non-dict releases payload fails closed with ValueError, not a crash.
    try:
        check_update(lambda request, timeout=0: Response(json.dumps([1, 2, 3]).encode()))
        raise AssertionError("non-dict release payload should fail")
    except ValueError as error:
        assert "payload" in str(error).casefold()

    # A .sig digest that JSON-decodes to a non-string is validated cleanly.
    from updates import load_release_signature
    int_digest_payload = dict(signature_payload, digest=int(digest, 16))
    try:
        load_release_signature(
            f"{TRUSTED_RELEASE_PREFIX}{tag}/{ASSET}.sig",
            opener=lambda request, timeout=0: Response(json.dumps(int_digest_payload).encode()),
        )
        raise AssertionError("non-string digest should fail")
    except ValueError as error:
        assert "digest" in str(error).casefold()
    digit_payload = dict(signature_payload, digest=int("1" * 64))
    parsed_sig = load_release_signature(
        f"{TRUSTED_RELEASE_PREFIX}{tag}/{ASSET}.sig",
        opener=lambda request, timeout=0: Response(json.dumps(digit_payload).encode()),
    )
    assert parsed_sig["digest"] == "1" * 64

    if os.name != "nt":
        # A symlinked destination must update the real AppImage, not the link.
        with mock.patch("updates._release_public_key", return_value=public_key):
            with tempfile.TemporaryDirectory() as directory:
                real = Path(directory) / "real.AppImage"
                real.write_bytes(b"old appimage")
                link = Path(directory) / "link.AppImage"
                link.symlink_to(real)
                install_update(update, link, opener)
                assert link.is_symlink()
                assert real.read_bytes() == payload

        # '%' in the AppImage path must survive desktop-entry field codes.
        from updates import install_desktop_entry
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Open Box%25.AppImage"
            app.write_bytes(b"x")
            with mock.patch("updates.Path.home", return_value=Path(directory)):
                desktop = Path(install_desktop_entry(app))
            exec_line = next(line for line in desktop.read_text(encoding="utf-8").splitlines() if line.startswith("Exec="))
            assert "%%25" in exec_line and '\\"' not in exec_line

    # verify_artifact is what the installers call, so it must work standalone.
    with tempfile.TemporaryDirectory() as directory:
        artifact = Path(directory) / ASSET
        artifact.write_bytes(payload)
        signature_file = Path(directory) / f"{ASSET}.sig"
        signature_file.write_text(json.dumps(signature_payload), encoding="utf-8")
        key_file = Path(directory) / "openbox-release.pub"
        key_file.write_bytes(public_key)
        assert verify_artifact(artifact, signature_file, key_file) == digest
        # The CLI surface the installers use reports success by exit code.
        assert updates.main(["verify", str(artifact), str(signature_file), str(key_file)]) == 0
        assert updates.main(["verify", str(artifact)]) == 2

        artifact.write_bytes(payload + b"tampered")
        try:
            verify_artifact(artifact, signature_file, key_file)
            raise AssertionError("tampered artifact should not verify")
        except ValueError as error:
            assert "digest" in str(error).casefold()
        assert updates.main(["verify", str(artifact), str(signature_file), str(key_file)]) == 1

        artifact.write_bytes(payload)
        key_file.write_bytes(updates.PLACEHOLDER_PUBLIC_KEY)
        try:
            verify_artifact(artifact, signature_file, key_file)
            raise AssertionError("placeholder release key should not verify")
        except ValueError as error:
            assert "placeholder" in str(error).casefold()

    if os.name == "nt":
        import zipfile

        import winreg

        from updates import _applier_script, _install_update_windows

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "OpenBox"
            share = root / "share" / "openbox"
            share.mkdir(parents=True)
            (share / "web_app.py").write_text("# old\n", encoding="utf-8")

            archive = Path(directory) / "payload.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("payload/web_app.py", "# new\n")
                bundle.writestr("payload/pkg/__init__.py", "")
            zip_bytes = archive.read_bytes()
            windows_update = dict(
                update,
                artifact=f"{TRUSTED_RELEASE_PREFIX}{tag}/{ASSET}",
                checksum=hashlib.sha256(zip_bytes).hexdigest(),
            )
            launched = []
            with mock.patch.object(updates, "windows_install_dir", return_value=root), \
                 mock.patch.object(updates, "verify_release_signature", return_value=True), \
                 mock.patch.object(
                     updates, "_powershell",
                     side_effect=lambda script, detached=False: launched.append((script, detached)),
                 ):
                result = _install_update_windows(
                    windows_update, lambda request, timeout=0: Response(zip_bytes)
                )
            assert result["restart_required"] is True
            assert result["backup"] == f"{share}.previous"
            assert launched and launched[0][1] is True, "the applier must run detached"
            assert str(share) in launched[0][0]

            # A checkout is never moved: updates require an installed copy.
            with mock.patch.object(updates, "windows_install_dir", return_value=Path(directory) / "absent"):
                try:
                    _install_update_windows(windows_update, lambda request, timeout=0: Response(zip_bytes))
                    raise AssertionError("an uninstalled copy must refuse to self-update")
                except ValueError as error:
                    assert "installed copy" in str(error).casefold()

        # The applier genuinely swaps the tree once the old process is gone.
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "openbox"
            target.mkdir()
            (target / "web_app.py").write_text("# old\n", encoding="utf-8")
            staged = Path(directory) / "staged"
            staged.mkdir()
            (staged / "web_app.py").write_text("# new\n", encoding="utf-8")
            scratch = Path(directory) / "scratch"
            scratch.mkdir()
            reaped = subprocess.Popen([sys.executable, "-c", "pass"])
            reaped.wait(timeout=30)
            result = updates._powershell(_applier_script(reaped.pid, target, staged, scratch))
            assert result.returncode == 0, result.stderr
            assert (target / "web_app.py").read_text(encoding="utf-8") == "# new\n"
            assert (Path(f"{target}.previous") / "web_app.py").read_text(encoding="utf-8") == "# old\n"
            assert not staged.exists() and not scratch.exists()

        # Desktop integration writes a real Start Menu shortcut and protocol.
        with tempfile.TemporaryDirectory() as directory:
            programs = Path(directory) / "Programs"
            launcher = Path(directory) / "openbox.cmd"
            launcher.write_text("@echo off\r\n", encoding="utf-8")
            with mock.patch.object(updates, "start_menu_programs_dir", return_value=programs), \
                 mock.patch.object(updates, "_install_root", return_value=Path(directory)):
                link = Path(updates.install_desktop_entry(str(launcher)))
            try:
                assert link.is_file()
                resolved = subprocess.run(
                    [
                        "powershell.exe", "-NoProfile", "-Command",
                        f"(New-Object -ComObject WScript.Shell).CreateShortcut('{link}').TargetPath",
                    ],
                    capture_output=True, text=True, check=False,
                )
                assert resolved.stdout.strip() == str(launcher), resolved.stdout
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Software\Classes\openbox\shell\open\command"
                ) as key:
                    assert str(launcher) in winreg.QueryValueEx(key, "")[0]
            finally:
                for subkey in (
                    r"Software\Classes\openbox\shell\open\command",
                    r"Software\Classes\openbox\shell\open",
                    r"Software\Classes\openbox\shell",
                    r"Software\Classes\openbox",
                ):
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
                    except FileNotFoundError:
                        pass

    print("update self-test: ok")


if __name__ == "__main__":
    main()
