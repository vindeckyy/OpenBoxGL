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
    visibility: str = "internal"
    synthetic: bool = False
    alias_of: str = ""

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
            "visibility": self.visibility,
            "synthetic": self.synthetic,
            "alias_of": self.alias_of,
        }


# Central registry mapping (method, path) -> Route
_REGISTRY: dict[tuple[str, str], Route] = {}


VISIBILITIES = ("public", "host", "internal")


def default_visibility(path: str) -> str:
    """Public operations live under /api/v2/, the native host channel under /api/host/, the rest is internal."""
    if path == "/api/v2" or path.startswith("/api/v2/"):
        return "public"
    if path.startswith("/api/host/"):
        return "host"
    return "internal"


def _check_visibility(path: str, visibility: str) -> None:
    if visibility not in VISIBILITIES:
        raise ValueError(f"Unknown route visibility {visibility!r} for {path}.")
    if visibility == "public" and not path.startswith("/api/v2/"):
        raise ValueError(f"Public route {path} must live under /api/v2/.")
    if visibility == "host" and not path.startswith("/api/host/"):
        raise ValueError(f"Host route {path} must live under /api/host/.")


def register(
    method: str,
    path: str,
    spec: str | None = None,
    func: Any = None,
    public: bool = False,
    visibility: str | None = None,
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
    resolved_visibility = visibility or default_visibility(path)
    _check_visibility(path, resolved_visibility)
    route_obj = Route(
        method=method_upper,
        path=path,
        spec=spec,
        func=func,
        public=public,
        visibility=resolved_visibility,
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
    visibility: str | None = None,
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
                visibility=visibility,
            )
        return func

    return decorator


def _ensure_handlers_loaded() -> None:
    """Load all handler modules and web_app to trigger @route registrations.

    When the app is started as ``python web_app.py``, the module is ``__main__``,
    not ``web_app``. Importing ``web_app`` again would execute a second copy and
    can fail partway through the handler list; skip it in that case, because its
    decorators are already registered.
    """
    import sys

    main = sys.modules.get("__main__")
    main_file = str(getattr(main, "__file__", "") or "")
    running_web_app = main_file.replace("\\", "/").endswith("/web_app.py") or main_file == "web_app.py"
    modules = (
        "handlers.library",
        "handlers.library_storage",
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
        "handlers.insights",
        "handlers.picker",
        "handlers.constellation",
        "handlers.party",
        "handlers.export",
        "handlers.screenscraper",
        "handlers.resume",
        "handlers.moments",
        "handlers.steamgrid",
        "handlers.library_health",
        "handlers.timemachine",
        "handlers.household",
        "handlers.clips",
        "handlers.steambridge",
        "handlers.arcade",
        "handlers.collections",
        "handlers.defs",
        "web_app",
    )
    for mod in modules:
        if mod == "web_app" and running_web_app:
            continue
        importlib.import_module(mod)


def _public_alias(path: str) -> str | None:
    """Return the public /api/v2 name for a private /api base path, or None.

    Base paths remain the implementation names used by handlers. The public
    contract is the synthesized alias, which is what gates and docs describe.
    """
    if not path.startswith("/api/"):
        return None
    if path.startswith(("/api/v1/", "/api/v2/", "/api/host/")):
        return None
    return "/api/v2" + path[len("/api"):]


def all_routes() -> list[Route]:
    """Return every registered route plus synthesized public /api/v2 aliases.

    Explicit registrations always win. The synthesized entries exist so the
    public surface can be ratcheted and documented without pretending the
    private base path is public.
    """
    _ensure_handlers_loaded()
    explicit = list(_REGISTRY.values())
    keys = {(route.method, route.path) for route in explicit}
    synthesized = []
    for route in explicit:
        alias = _public_alias(route.path)
        if alias is None or (route.method, alias) in keys:
            continue
        synthesized.append(
            Route(
                method=route.method,
                path=alias,
                spec=route.spec,
                func=route.func,
                public=True,
                visibility="public",
                synthetic=True,
                alias_of=route.path,
            )
        )
    return explicit + synthesized


def get_routes_for_method(method: str) -> dict[str, str]:
    """Return path -> spec mapping for a given HTTP method, including aliases."""
    method_upper = method.upper()
    return {
        route.path: route.spec
        for route in all_routes()
        if route.method == method_upper
    }
