import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from env_config import bootstrap_env, load_dotenv, retroachievements_from_env


class EnvConfigTests(unittest.TestCase):
    def test_load_dotenv(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text('RETROACHIEVEMENTS_USERNAME="player"\nRETROACHIEVEMENTS_API_KEY=secret\n')
            values = load_dotenv(path)
            self.assertEqual(values["RETROACHIEVEMENTS_USERNAME"], "player")
            self.assertEqual(os.environ["RETROACHIEVEMENTS_API_KEY"], "secret")

    def test_github_token_from_env(self):
        os.environ["GITHUB_TOKEN"] = "fake-github-token-for-test"
        from env_config import github_token_from_env
        self.assertEqual(github_token_from_env(), "fake-github-token-for-test")
        del os.environ["GITHUB_TOKEN"]

    def test_bootstrap_env_finds_home_env(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            env = home / ".env"
            env.write_text("RA_USERNAME=boot\nRA_API_KEY=strap\n")
            with mock.patch("env_config.Path.home", return_value=home):
                bootstrap_env(None)
            self.assertEqual(os.environ.get("RA_USERNAME"), "boot")
            del os.environ["RA_USERNAME"]
            del os.environ["RA_API_KEY"]


if __name__ == "__main__":
    unittest.main()
