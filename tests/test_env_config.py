import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from env_config import bootstrap_env, load_dotenv


class EnvConfigTests(unittest.TestCase):
    def test_load_dotenv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('RETROACHIEVEMENTS_USERNAME="player"\nRETROACHIEVEMENTS_API_KEY=secret\n')
            path.chmod(0o600)
            values = load_dotenv(path)
            self.assertEqual(values["RETROACHIEVEMENTS_USERNAME"], "player")
            self.assertEqual(os.environ["RETROACHIEVEMENTS_API_KEY"], "secret")

    def test_load_dotenv_requires_owner_only_regular_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / ".env"
            path.write_text("TOKEN=secret\n")
            path.chmod(0o644)
            self.assertEqual(load_dotenv(path), {})
            target = root / "target.env"
            target.write_text("TOKEN=secret\n")
            target.chmod(0o600)
            link = root / "linked.env"
            link.symlink_to(target)
            self.assertEqual(load_dotenv(link), {})

    def test_load_dotenv_skips_unreadable_and_binary_files(self):
        if os.geteuid() == 0:
            self.skipTest("permission bits are bypassed when running as root")
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / ".env"
            binary.write_bytes(b"TOKEN=\xff\xfe\x00broken")
            self.assertEqual(load_dotenv(binary), {})
            unreadable = Path(folder) / "locked.env"
            unreadable.write_text("TOKEN=secret\n")
            unreadable.chmod(0)
            try:
                self.assertEqual(load_dotenv(unreadable), {})
            finally:
                unreadable.chmod(0o600)
            # bootstrap_env with a binary .env in scope must not raise.
            with mock.patch("env_config.discover_env_files", return_value=[binary]):
                bootstrap_env(None)
            self.assertNotIn("TOKEN", os.environ)

    def test_load_dotenv_strips_inline_comments(self):
        from env_config import _parse_env_line
        self.assertEqual(_parse_env_line("TOKEN=abc # note"), ("TOKEN", "abc"))
        self.assertEqual(_parse_env_line("PASSWORD=p#ss"), ("PASSWORD", "p#ss"))
        self.assertEqual(_parse_env_line('KEY="a b" # note'), ("KEY", "a b"))
        self.assertEqual(_parse_env_line('KEY="a # b"'), ("KEY", "a # b"))

    def test_load_dotenv_rejects_unknown_environment_keys(self):
        previous = os.environ.pop("LD_PRELOAD", None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ".env"
                path.write_text("LD_PRELOAD=/tmp/evil.so\nUNKNOWN_SETTING=bad\n")
                path.chmod(0o600)
                self.assertEqual(load_dotenv(path), {})
                self.assertNotIn("LD_PRELOAD", os.environ)
                self.assertNotIn("UNKNOWN_SETTING", os.environ)
        finally:
            if previous is not None:
                os.environ["LD_PRELOAD"] = previous

    def test_github_token_from_env(self):
        os.environ["GITHUB_TOKEN"] = "fake-github-token-for-test"
        try:
            from env_config import github_token_from_env
            self.assertEqual(github_token_from_env(), "fake-github-token-for-test")
        finally:
            os.environ.pop("GITHUB_TOKEN", None)

    def test_bootstrap_env_finds_home_env(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            env = home / ".env"
            env.write_text("RA_USERNAME=boot\nRA_API_KEY=strap\n")
            env.chmod(0o600)
            with mock.patch("env_config.Path.home", return_value=home):
                bootstrap_env(None)
            try:
                self.assertEqual(os.environ.get("RA_USERNAME"), "boot")
            finally:
                os.environ.pop("RA_USERNAME", None)
                os.environ.pop("RA_API_KEY", None)

    def test_discovery_ignores_current_working_directory(self):
        from env_config import discover_env_files

        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder)
            env = current / ".env"
            env.write_text("TOKEN=unsafe\n")
            env.chmod(0o600)
            with mock.patch("env_config.Path.cwd", return_value=current):
                self.assertNotIn(env, discover_env_files())

    def test_load_dotenv_loads_screenscraper_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("SCREENSCRAPER_USER=u\nSCREENSCRAPER_PASSWORD=p\n")
            env.chmod(0o600)
            load_dotenv(env)
            try:
                self.assertEqual(os.environ.get("SCREENSCRAPER_USER"), "u")
                self.assertEqual(os.environ.get("SCREENSCRAPER_PASSWORD"), "p")
            finally:
                os.environ.pop("SCREENSCRAPER_USER", None)
                os.environ.pop("SCREENSCRAPER_PASSWORD", None)


if __name__ == "__main__":
    unittest.main()
