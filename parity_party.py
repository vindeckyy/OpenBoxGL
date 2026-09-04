"""Compatibility shim for parity_party."""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("pkg.parity.parity_party")
