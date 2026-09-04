"""Compatibility shim for parity_picker."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("pkg.parity.parity_picker")
