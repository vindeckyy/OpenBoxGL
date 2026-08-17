"""Ensure local secrets are gitignored and not present in tracked files."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "web_app.py").exists() and (ROOT.parent / "web_app.py").exists():
    ROOT = ROOT.parent
if not (ROOT / ".gitignore").exists() and (ROOT.parent / ".gitignore").exists():
    ROOT = ROOT.parent
SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9_]{20,}"),
)


class SecretSafetyTests(unittest.TestCase):
    def test_env_is_gitignored(self):
        gitignore = (ROOT / ".gitignore").read_text()
        self.assertIn(".env", gitignore)
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, ".env must be ignored by git")

    def test_env_example_has_no_real_secrets(self):
        example = ROOT / ".env.example"
        self.assertTrue(example.is_file())
        for pattern in SECRET_PATTERNS:
            self.assertIsNone(pattern.search(example.read_text()))

    def test_tracked_files_contain_no_github_tokens(self):
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        for relative in tracked:
            path = ROOT / relative
            if not path.is_file() or path.suffix in {".AppImage", ".png", ".jpg", ".svg"}:
                continue
            try:
                content = path.read_text(errors="ignore")
            except OSError:
                continue
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(
                    pattern.search(content),
                    f"Possible GitHub token leaked in tracked file: {relative}",
                )


if __name__ == "__main__":
    unittest.main()
