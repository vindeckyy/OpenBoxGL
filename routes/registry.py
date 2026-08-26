"""Route registry and decorator definitions for OpenBox HTTP routes."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable, Sequence


@dataclass
class Route:
    method: str
    path: str
    spec: str
    func: Any = None
    public: bool = False
    v1: bool = False

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "spec": self.spec,
            "public": self.public,
            "v1": self.v1,
        }


# Central registry mapping (method, path) -> Route
_REGISTRY: dict[tuple[str, str], Route] = {}


def register(
    method: str,
    path: str,
    spec: str | None = None,
    func: Any = None,
    public: bool = False,
    v1: bool = False,
) -> Route:
    """Register a route in the central _REGISTRY."""
    method_upper = method.upper()
    if spec is None:
        if func is not None:
            if getattr(func, "__module__", "") == "handlers.native":
                spec = f"{func.__module__}.{func.__name__}"
            else:
                spec = func.__name__
        else:
            raise ValueError("Either spec or func must be provided.")
    route_obj = Route(
        method=method_upper,
        path=path,
        spec=spec,
        func=func,
        public=public,
        v1=v1,
    )
    key = (method_upper, path)
    if key in _REGISTRY:
        raise ValueError(f"Duplicate route registration: {method_upper} {path}")
    _REGISTRY[key] = route_obj
    return route_obj


def route(
    method: str,
    path: str | Sequence[str],
    spec: str | None = None,
    public: bool = False,
    v1: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator to register a handler method or function for an HTTP route."""
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        resolved_spec = spec
        if resolved_spec is None:
            if getattr(func, "__module__", "") == "handlers.native":
                resolved_spec = f"{func.__module__}.{func.__name__}"
            else:
                resolved_spec = func.__name__

        paths = [path] if isinstance(path, str) else list(path)
        for p in paths:
            register(
                method=method,
                path=p,
                spec=resolved_spec,
                func=func,
                public=public,
                v1=v1,
            )
        return func

    return decorator


def _ensure_handlers_loaded() -> None:
    """Load all handler modules and web_app to trigger @route registrations."""
    modules = (
        "handlers.library",
        "handlers.imports",
        "handlers.media",
        "handlers.metadata",
        "handlers.sessions",
        "handlers.settings",
        "handlers.extensions",
        "handlers.health",
        "handlers.emulators",
        "handlers.launch",
        "handlers.jobs",
        "handlers.setup",
        "handlers.data",
        "handlers.wine",
        "handlers.faugus",
        "handlers.native",
        "web_app",
    )
    for mod in modules:
        importlib.import_module(mod)


def all_routes() -> list[Route]:
    """Return all routes registered in the registry."""
    _ensure_handlers_loaded()
    return list(_REGISTRY.values())


def get_routes_for_method(method: str) -> dict[str, str]:
    """Return path -> spec mapping for a given HTTP method from registry."""
    _ensure_handlers_loaded()
    method_upper = method.upper()
    return {
        r.path: r.spec
        for (m, _), r in _REGISTRY.items()
        if m == method_upper
    }
