"""Installer replacement behavior: one AppImage, one symlink, no nested tree."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
PUBLIC_KEY = ROOT / "openbox-release.pub"


class InstallerTests(unittest.TestCase):
    def test_posix_installer_replaces_an_existing_install_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            version = "1.18.1"
            asset = "OpenBox-x86_64.AppImage"
            payload = b"first install\n"
            (fixtures / asset).write_bytes(payload)
            (fixtures / f"{asset}.sha256").write_text(
                f"{hashlib.sha256(payload).hexdigest()}  {asset}\n", encoding="utf-8"
            )
            (fixtures / f"{asset}.sig").write_text(json.dumps({
                "algorithm": "ed25519",
                "digest_algorithm": "sha256",
                "digest": hashlib.sha256(payload).hexdigest(),
                "signature": base64.b64encode(b"x" * 64).decode(),
            }), encoding="utf-8")
            (fixtures / "openbox-release.pub").write_bytes(PUBLIC_KEY.read_bytes())

            binaries = root / "bin"
            binaries.mkdir()
            curl = binaries / "curl"
            curl.write_text(
                "#!/bin/sh\n"
                "out=''; url=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --output) out=\"$2\"; shift 2;;\n"
                "    http*) url=\"$1\"; shift;;\n"
                "    *) shift;;\n"
                "  esac\n"
                "done\n"
                "cp \"$OPENBOX_TEST_FIXTURES/${url##*/}\" \"$out\"\n",
                encoding="utf-8",
            )
            curl.chmod(0o755)
            openssl = binaries / "openssl"
            openssl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            openssl.chmod(0o755)

            install_dir = root / "installed"
            env = {
                **os.environ,
                "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
                "OPENBOX_INSTALL_DIR": str(install_dir),
                "OPENBOX_RELEASE_TAG": f"v{version}",
                "OPENBOX_TEST_FIXTURES": str(fixtures),
            }
            for _ in range(2):
                result = subprocess.run(
                    ["bash", str(INSTALLER)], env=env, capture_output=True, text=True, timeout=30, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            installed = install_dir / asset
            self.assertTrue(installed.is_file())
            self.assertEqual(installed.read_bytes(), payload)
            self.assertEqual(sorted(path.name for path in install_dir.iterdir()), sorted([asset, "openbox"]))
            link = install_dir / "openbox"
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), installed.resolve())
            self.assertFalse((install_dir / "openbox").is_dir())

    def test_powershell_installer_swaps_an_existing_share_tree(self):
        source = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("$target = Join-Path $shareRoot 'openbox'", source)
        self.assertIn("$previous", source)
        self.assertIn("Restored the previous install", source)


if __name__ == "__main__":
    unittest.main()
