"""Load optional .env files for local credentials without extra dependencies."""

import os
from pathlib import Path


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    if not key:
        return None
    return key, value


def load_dotenv(path):
    path = Path(path)
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        parsed = _parse_env_line(line)
        if parsed:
            key, value = parsed
            values[key] = value
            if key not in os.environ:
                os.environ[key] = value
    return values


def discover_env_files(*extra_roots):
    roots = []
    for root in extra_roots:
        if root:
            roots.append(Path(root).expanduser())
    roots.extend([
        Path.cwd(),
        Path(__file__).resolve().parent,
        Path.home(),
        Path.home() / ".config/openbox-game-launcher",
    ])
    seen = set()
    files = []
    for root in roots:
        path = root / ".env"
        key = str(path)
        if key not in seen and path.is_file():
            seen.add(key)
            files.append(path)
    return files


_env_bootstrapped = False


def bootstrap_env(data_dir=None):
    global _env_bootstrapped
    for path in discover_env_files(data_dir, Path(data_dir).parent if data_dir else None):
        load_dotenv(path)
    _env_bootstrapped = True


def ensure_env_loaded(data_dir=None):
    if not _env_bootstrapped:
        bootstrap_env(data_dir)


def env_value(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def retroachievements_from_env():
    username = env_value("RETROACHIEVEMENTS_USERNAME", "RA_USERNAME", "OPENBOX_RA_USERNAME")
    api_key = env_value(
        "RETROACHIEVEMENTS_API_KEY",
        "RA_API_KEY",
        "RETROACHIEVEMENTS_KEY",
        "OPENBOX_RA_API_KEY",
    )
    if username and api_key:
        return {"username": username, "api_key": api_key}
    return {}


def emumovies_from_env():
    username = env_value("EMUMOVIES_USERNAME", "OPENBOX_EMUMOVIES_USERNAME")
    password = env_value("EMUMOVIES_PASSWORD", "OPENBOX_EMUMOVIES_PASSWORD")
    if username and password:
        return {"username": username, "password": password}
    return {}


def github_token_from_env():
    ensure_env_loaded()
    return env_value("GITHUB_TOKEN", "GH_TOKEN", "OPENBOX_GITHUB_TOKEN")
