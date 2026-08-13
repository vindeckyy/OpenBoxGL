"""Release signature verification tests (stdlib Ed25519)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent


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
