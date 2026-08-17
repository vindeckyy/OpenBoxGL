"""Shim: canonical source is pkg/parity/parity_filter_presets.py - kept for backwards compat."""

import importlib as _importlib
import sys as _sys

_mod = _importlib.import_module("pkg.parity.parity_filter_presets")
_sys.modules[__name__] = _mod
