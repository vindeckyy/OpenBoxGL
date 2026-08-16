"""Load optional .env files for local credentials without extra dependencies."""

import os
import stat
from pathlib import Path


ENV_FILE_ENV = "OPENBOX_ENV_FILE"
MAX_ENV_FILE_BYTES = 1024 * 1024
ENV_KEYS = frozenset({
    "GITHUB_TOKEN", "GH_TOKEN", "OPENBOX_GITHUB_TOKEN",
    "IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET",
    "RETROACHIEVEMENTS_USERNAME", "RA_USERNAME", "OPENBOX_RA_USERNAME",
    "RETROACHIEVEMENTS_API_KEY", "RA_API_KEY", "RETROACHIEVEMENTS_KEY",
    "OPENBOX_RA_API_KEY",
    "EMUMOVIES_USERNAME", "OPENBOX_EMUMOVIES_USERNAME",
    "EMUMOVIES_PASSWORD", "OPENBOX_EMUMOVIES_PASSWORD",
    "STRIPE_SECRET_KEY",
    "OPENBOX_ALLOW_HTTP_WEBHOOKS", "OPENBOX_ALLOW_HTTP_GAMEYFIN",
})


def _secure_env_file(path):
    """Return whether *path* is an owner-only regular env file.

    Environment files can contain credentials and are read during startup.
    Keep discovery limited to files owned by this user and reject symlinks
    before opening them; ``load_dotenv`` repeats the checks on the opened FD
    to close the replacement race between discovery and read.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & 0o077
        and info.st_size <= MAX_ENV_FILE_BYTES
    )


def _parse_env_line(line):
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    # A '#' preceded by whitespace is a comment; inside an unquoted value it is preserved.
    value = value.strip()
    quote = ""
    comment_at = -1
    for index, char in enumerate(value):
        if char in ("'", '"'):
            if quote == "":
                quote = char
            elif quote == char:
                quote = ""
        elif char == "#" and not quote and index > 0 and value[index - 1].isspace():
            comment_at = index
            break
    if comment_at != -1:
        value = value[:comment_at].rstrip()
    value = value.strip().strip('"').strip("'")
    if not key or not key.replace("_", "a").isalnum() or key[0].isdigit() or "\x00" in value:
        return None
    return key, value


def load_dotenv(path):
    path = Path(path).expanduser()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_ENV_FILE_BYTES
        ):
            os.close(descriptor)
            return {}
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            contents = source.read(MAX_ENV_FILE_BYTES + 1)
        if len(contents) > MAX_ENV_FILE_BYTES:
            return {}
        lines = contents.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        # Optional .env files must never abort startup; skip anything unreadable.
        if descriptor >= 0:
            os.close(descriptor)
        return {}
    values = {}
    for line in lines:
        parsed = _parse_env_line(line)
        if parsed:
            key, value = parsed
            if key not in ENV_KEYS:
                continue
            values[key] = value
            if key not in os.environ:
                os.environ[key] = value
    return values


def discover_env_files(*extra_roots):
    roots = []
    explicit = os.environ.get(ENV_FILE_ENV, "").strip()
    files = []
    if explicit:
        candidate = Path(explicit).expanduser()
        if _secure_env_file(candidate):
            files.append(candidate)
    for root in extra_roots:
        if root:
            roots.append(Path(root).expanduser())
    roots.extend([
        Path.home(),
        Path.home() / ".config/openbox-game-launcher",
    ])
    seen = set()
    for path in files:
        seen.add(str(path))
    for root in roots:
        path = root / ".env"
        key = str(path)
        if key not in seen and _secure_env_file(path):
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
