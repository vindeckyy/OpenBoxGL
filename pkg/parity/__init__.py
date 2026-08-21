"""Package initialization for OpenBox parity modules."""

import importlib.abc
import importlib.util
import sys


class _ParityFlatFinder(importlib.abc.MetaPathFinder):
    """Resolve `import parity_*` to `pkg.parity.parity_*` when the flat shim is absent.

    Kept for one release alongside the 22 root `parity_*.py` shims per ADR 0003.
    When the shims are removed next release this finder becomes the sole flat
    import bridge. Registering it now proves the path without breaking the
    current shim layout.
    """

    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("parity_"):
            return None
        # Only top-level parity_* names, not submodules
        if "." in fullname:
            return None
        # If a real file shim exists, let the normal PathFinder handle it
        # by returning None here. This keeps current behavior unchanged.
        # Only synthesize a spec when the canonical module exists.
        canonical = f"pkg.parity.{fullname}"
        try:
            spec = importlib.util.find_spec(canonical)
        except (ImportError, ValueError):
            return None
        if spec is None or spec.loader is None:
            return None
        # Create a spec that loads the canonical module but aliases it
        loader = _ParityAliasLoader(canonical)
        return importlib.util.spec_from_loader(fullname, loader)


class _ParityAliasLoader(importlib.abc.Loader):
    def __init__(self, canonical: str):
        self.canonical = canonical

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        import importlib
        canonical_mod = importlib.import_module(self.canonical)
        # Alias sys.modules for both names
        sys.modules[module.__name__] = canonical_mod
        sys.modules[self.canonical] = canonical_mod
        module.__dict__.update(canonical_mod.__dict__)


# Register once, low priority (end of meta_path) so filesystem shims win
if not any(isinstance(f, _ParityFlatFinder) for f in sys.meta_path):
    sys.meta_path.append(_ParityFlatFinder())

