"""Per-provider API tokens for optional integrations.

Tokens never pass through the public settings projection. The settings API
returns a masked view; the full value is only read by the importing provider.
"""
from __future__ import annotations

import re

PROVIDERS = ("igdb", "screenscraper", "steamgriddb", "gameyfin", "retroachievements")
MAX_TOKEN_LENGTH = 4096


def validate_provider(provider):
    value = str(provider or "").strip().lower()
    if value not in PROVIDERS:
        raise ValueError("Unknown integration provider.")
    return value


def validate_token(token):
    value = str(token or "").strip()
    if not value or len(value) > MAX_TOKEN_LENGTH or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("Integration token is invalid.")
    return value


def clean_tokens(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Integration tokens must be an object.")
    clean = {}
    for provider, token in value.items():
        try:
            key = validate_provider(provider)
            clean[key] = validate_token(token)
        except ValueError:
            continue
    return clean


def masked_tokens(tokens):
    masked = {}
    for provider, token in (tokens or {}).items():
        masked[provider] = f"{token[:4]}…{token[-2:]}" if len(token) > 8 else "••••"
    return masked


def public_tokens(tokens):
    return {provider: bool((tokens or {}).get(provider)) for provider in PROVIDERS}
