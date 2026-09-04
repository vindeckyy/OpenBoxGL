"""Central dependency registry for pkg.state modules (ADR 0009).

Replaces the per-module ``_ns(name, default)`` pattern that looked up
``webapp_state`` module attributes at call time.  ``webapp_state.py``
populates this registry at import time via ``_populate_deps()``; each
``pkg/state/*.py`` module calls ``_deps.get("name", default)`` instead.
"""

_REGISTRY: dict[str, object] = {}


def register(name: str, value: object) -> None:
    """Register or overwrite a dependency by name."""
    _REGISTRY[name] = value


def get(name: str, default: object | None = None) -> object:
    """Look up a registered dependency, falling back to *default*."""
    return _REGISTRY.get(name, default)


def registered_names() -> list[str]:
    """Return sorted list of registered names (for diagnostics/tests)."""
    return sorted(_REGISTRY)
