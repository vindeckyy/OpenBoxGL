"""Handlers package for OpenBox capability modules.

``web_app.Handler`` inherits the mixin classes here (``LibraryHandlers``,
``ImportsHandlers``, ``MediaHandlers``, and the rest), so a route in
``routes.py`` keeps naming a bare ``Handler`` method. ``handlers/native.py`` is
the exception: its module-level functions are wired directly by dotted route
specs and take the request handler as their first argument:

    def capabilities(handler, parsed): ...
    def dialog(handler, payload): ...

Every mixin method body references DATA, load_state, transact_state, and other
names from the live ``web_app`` namespace. ``rebind_methods`` repoints each
method's ``__globals__`` at that namespace, so the bodies run verbatim without
circular imports or snapshotting process-global state.
"""
import sys
import types


def _live_web_app():
    """The running web_app namespace, whether imported or executed as __main__."""
    return sys.modules.get("web_app") or sys.modules.get("__main__")


def rebind_methods(cls):
    """Repoint every method on ``cls`` at the live web_app namespace.

    web_app.py runs as ``__main__`` in production but is imported as ``web_app``
    in tests. Mixin method bodies reference DATA, TOKEN, JOB_MANAGER, load_state,
    transact_state, and dozens of other names that live on web_app. Importing
    those names would re-execute web_app under ``__main__`` and fork every
    process-global; a plain module ``__getattr__`` does not cover bare names
    inside function bodies. Rebinding ``__globals__`` makes each method resolve
    those names from the live namespace at call time, so both modes, nested
    worker functions, mock.patch("web_app.X"), and runtime reassignment all work.
    """
    app = _live_web_app()
    if app is None:
        raise RuntimeError("rebind_methods: web_app namespace not found")
    app_dict = app.__dict__
    for name, value in list(cls.__dict__.items()):
        if isinstance(value, staticmethod):
            fn = value.__func__
            is_static = True
        elif isinstance(value, types.FunctionType):
            fn = value
            is_static = False
        else:
            continue
        new = types.FunctionType(
            fn.__code__, app_dict, fn.__name__, fn.__defaults__, fn.__closure__
        )
        new.__kwdefaults__ = fn.__kwdefaults__
        new.__qualname__ = fn.__qualname__
        new.__doc__ = fn.__doc__
        new.__module__ = fn.__module__
        setattr(cls, name, staticmethod(new) if is_static else new)
    return cls
