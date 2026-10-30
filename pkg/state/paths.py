"""Portable library paths: encode, decode, and containment for ``@library/...`` tokens.

Pure functions. Nothing here reads the filesystem or the environment; callers
pass the platform rules they need (``windows``, ``case_insensitive``), so the
same rules are testable on every host. Symlink checks happen in the callers,
which may call ``os.path.realpath`` themselves.

Rules (ADR 0090):

- A stored game path is an absolute POSIX path, a Windows drive path (stored
  with forward slashes), a UNC path (never tokenized), a relative path (only
  tokens are relative), or ``@library`` / ``@library/<relative>``.
- Containment is lexical, after ``.`` and ``..`` resolution and Unicode NFC
  normalization. Case is folded only when ``case_insensitive`` is set.
- Stored text is never rewritten: encoding keeps the user's spelling and
  separators, and only the containment comparison normalizes.
- Decoding rejects NUL bytes, drive prefixes, UNC prefixes, leading separators,
  and any ``..`` segment, then checks containment of the joined result again.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

LIBRARY_TOKEN = "@library"

CODE_TOKEN_ESCAPE = "PATH_TOKEN_ESCAPE"
CODE_TOKEN_INVALID = "PATH_TOKEN_INVALID"
CODE_ROOT_MISSING = "LIBRARY_ROOT_MISSING"
CODE_ROOT_INVALID = "LIBRARY_ROOT_INVALID"


class PathTokenError(ValueError):
    """A token or library root cannot be resolved safely. ``code`` is stable and machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Lexical:
    prefix: str  # "/" for POSIX, "C:/" for drive paths
    parts: tuple[str, ...]


def _fold(text: str, case_insensitive: bool) -> str:
    text = unicodedata.normalize("NFC", text)
    return text.casefold() if case_insensitive else text


def _has_drive(value: str) -> bool:
    return len(value) >= 3 and value[0].isascii() and value[0].isalpha() and value[1] == ":" and value[2] in "/\\"


def is_absolute(value: str, *, windows: bool) -> bool:
    if value.startswith("/"):
        return True
    return windows and (_has_drive(value) or value.startswith("\\\\"))


def is_unc(value: str, *, windows: bool) -> bool:
    return windows and (value.startswith("\\\\") or value.startswith("//"))


def is_token(value: str) -> bool:
    return value == LIBRARY_TOKEN or value.startswith((LIBRARY_TOKEN + "/", LIBRARY_TOKEN + "\\"))


def _lexical(value: str, *, windows: bool) -> _Lexical | None:
    """Split an absolute path into prefix and parts. None when ``..`` climbs past the root."""
    text = value.replace("\\", "/") if windows else value
    if windows and _has_drive(text):
        prefix, rest = text[:2] + "/", text[3:]
    else:
        prefix, rest = "/", text[1:]
    parts: list[str] = []
    for segment in rest.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(segment)
    return _Lexical(prefix, tuple(parts))


def _render(lex: _Lexical, *, windows: bool) -> str:
    body = "/".join(lex.parts)
    text = lex.prefix + body if body else lex.prefix
    return text.replace("/", "\\") if windows else text


def is_within(path: str, root: str, *, windows: bool, case_insensitive: bool = False) -> bool:
    """True when ``path`` is ``root`` or lies beneath it, after lexical normalization."""
    if not is_absolute(path, windows=windows) or not is_absolute(root, windows=windows):
        return False
    if is_unc(path, windows=windows) or is_unc(root, windows=windows):
        return False
    left = _lexical(path, windows=windows)
    right = _lexical(root, windows=windows)
    if left is None or right is None:
        return False
    if _fold(left.prefix, windows) != _fold(right.prefix, windows):
        return False
    left_parts = [_fold(part, case_insensitive) for part in left.parts]
    right_parts = [_fold(part, case_insensitive) for part in right.parts]
    return left_parts[: len(right_parts)] == right_parts


def classify(value: str, root: str, *, windows: bool, case_insensitive: bool = False) -> str:
    """Return one of ``token``, ``unc``, ``relative``, ``inside_root``, or ``absolute``."""
    if is_token(value):
        return "token"
    if is_unc(value, windows=windows):
        return "unc"
    if not is_absolute(value, windows=windows):
        return "relative"
    if root and is_within(value, root, windows=windows, case_insensitive=case_insensitive):
        return "inside_root"
    return "absolute"


def encode_path(value: str, root: str, *, windows: bool, case_insensitive: bool = False) -> str:
    """Store ``value`` as ``@library/<relative>`` when it lies under ``root``.

    Anything else comes back unchanged, or with forward slashes when it is a
    drive path, so the stored form stays the same on every platform.
    """
    if not root or not is_absolute(root, windows=windows) or is_unc(root, windows=windows):
        return value
    if not is_absolute(value, windows=windows) or is_unc(value, windows=windows):
        return value
    lex = _lexical(value, windows=windows)
    if lex is None:
        return value
    if not is_within(value, root, windows=windows, case_insensitive=case_insensitive):
        return _render(lex, windows=False)
    root_lex = _lexical(root, windows=windows)
    relative = lex.parts[len(root_lex.parts):]
    return LIBRARY_TOKEN + "/" + "/".join(relative) if relative else LIBRARY_TOKEN


def decode_path(value: str, root: str, *, windows: bool, case_insensitive: bool = False) -> str:
    """Resolve a stored path against ``root``. Non-token values come back unchanged.

    Raises :class:`PathTokenError` for escapes, malformed tokens, and a missing
    or invalid library root.
    """
    if not is_token(value):
        return value
    if "\x00" in value:
        raise PathTokenError(CODE_TOKEN_INVALID, "Path tokens cannot contain NUL bytes.")
    rel = value[len(LIBRARY_TOKEN) + 1:] if len(value) > len(LIBRARY_TOKEN) else ""
    if not root:
        raise PathTokenError(CODE_ROOT_MISSING, "This path is relative to the library root, but no library root is set.")
    if not is_absolute(root, windows=windows) or is_unc(root, windows=windows):
        raise PathTokenError(CODE_ROOT_INVALID, "The library root must be an absolute local path.")
    if rel and (rel[0] in "/\\" or is_absolute(rel, windows=windows) or (windows and _has_drive(rel))):
        raise PathTokenError(CODE_TOKEN_ESCAPE, "A library token cannot start with a separator or a drive.")
    segments = (rel.replace("\\", "/") if windows else rel).split("/") if rel else []
    if any(segment == ".." for segment in segments):
        raise PathTokenError(CODE_TOKEN_ESCAPE, "A library token cannot contain '..'.")
    if windows and any(":" in segment for segment in segments):
        # A colon in a Windows segment is drive or alternate-data-stream syntax.
        raise PathTokenError(CODE_TOKEN_ESCAPE, "A library token cannot contain ':' on Windows.")
    base = _lexical(root, windows=windows)
    if base is None:
        raise PathTokenError(CODE_ROOT_INVALID, "The library root cannot be normalized.")
    lex = _Lexical(base.prefix, base.parts + tuple(segment for segment in segments if segment not in ("", ".")))
    resolved = _render(lex, windows=windows)
    if not is_within(resolved, root, windows=windows, case_insensitive=case_insensitive):
        raise PathTokenError(CODE_TOKEN_ESCAPE, "The resolved path leaves the library root.")
    return resolved


def normalize_root(root: str, *, windows: bool) -> str:
    """Validate a library root for storage. An empty value means no root.

    Returns the root with separators normalized to forward slashes. Relative
    paths and UNC shares are refused; they cannot anchor portable tokens.
    """
    text = str(root or "").strip()
    if not text:
        return ""
    if not is_absolute(text, windows=windows) or is_unc(text, windows=windows):
        raise PathTokenError(CODE_ROOT_INVALID, "The library root must be an absolute local path.")
    lex = _lexical(text, windows=windows)
    if lex is None:
        raise PathTokenError(CODE_ROOT_INVALID, "The library root cannot be normalized.")
    return _render(lex, windows=False)
