"""Release signature verification tests (stdlib Ed25519)."""

import subprocess
import sys
import tempfile
import unittest
import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "web_app.py").exists() and (ROOT.parent / "web_app.py").exists():
    ROOT = ROOT.parent
if not (ROOT / ".gitignore").exists() and (ROOT.parent / ".gitignore").exists():
    ROOT = ROOT.parent


class VerifyReleaseTests(unittest.TestCase):
    def test_round_trip_and_tamper(self):
        # Sign a tiny artifact with the real signer (dev-only cryptography),
        # verify with the stdlib verifier, then tamper and expect failure.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "OpenBox-test.AppImage"
            artifact.write_bytes(b"fake release bytes\n")
            key_path = root / "ed25519.key"
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            except ImportError:
                self.skipTest("cryptography not installed")
            key = Ed25519PrivateKey.generate()
            key_path.write_bytes(key.private_bytes_raw())

            sign = subprocess.run(
                [sys.executable, str(ROOT / "scripts/sign_release.py"),
                 "--key", str(key_path), "--out", str(root / "artifact.sig"),
                 "--public-key-out", str(root / "release.pub"), str(artifact)],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(sign.returncode, 0, sign.stderr)
            pub = root / "release.pub"
            self.assertTrue(pub.is_file(), "signer must emit the public key")

            # Text key encodings are accepted for CI secrets, and signing
            # without --public-key-out must not overwrite a working-tree key.
            encoded_key = root / "encoded.key"
            encoded_key.write_text(base64.b64encode(key.private_bytes_raw()).decode("ascii"))
            encoded_sig = root / "encoded.sig"
            encoded = subprocess.run(
                [sys.executable, str(ROOT / "scripts/sign_release.py"),
                 "--key", str(encoded_key), "--out", str(encoded_sig), str(artifact)],
                cwd=root, capture_output=True, text=True, check=False,
            )
            self.assertEqual(encoded.returncode, 0, encoded.stderr)
            self.assertFalse((root / "openbox-release.pub").exists())

            verify = subprocess.run(
                [sys.executable, str(ROOT / "scripts/verify_release.py"),
                 "--key", str(pub), str(artifact), str(root / "artifact.sig")],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)

            with artifact.open("ab") as handle:
                handle.write(b"tampered")
            tamper = subprocess.run(
                [sys.executable, str(ROOT / "scripts/verify_release.py"),
                 "--key", str(pub), str(artifact), str(root / "artifact.sig")],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(tamper.returncode, 0)
            self.assertIn("digest mismatch", tamper.stderr)


if __name__ == "__main__":
    unittest.main()
