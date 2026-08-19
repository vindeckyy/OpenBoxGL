"""Shim: canonical source is pkg/parity/parity_wine.py - kept for backwards compat."""

import importlib as _importlib
import sys as _sys

_mod = _importlib.import_module("pkg.parity.parity_wine")
_sys.modules[__name__] = _mod
