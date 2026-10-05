#!/usr/bin/env python3
"""S17 -- a concurrent registry build published a half-empty index.

``_registry()`` was a bare check-then-set on a module global::

    if _REGISTRY_CACHE is None:
        _REGISTRY_CACHE = {"adapters": load_adapters(), "by_adapter_id": {}, ...}
        for adapter in _REGISTRY_CACHE["adapters"]:
            _REGISTRY_CACHE["by_adapter_id"][...] = adapter
            ...

Two things went wrong, and the first is the severe one:

1. **Published before filled.** ``_REGISTRY_CACHE`` is assigned with only
   ``adapters`` populated; the four ``by_*`` index maps are filled afterwards, in
   place. A second thread entering ``_registry()`` in that window found a
   non-``None`` cache and returned it immediately -- so ``find_adapter``,
   ``find_by_platform`` and ``find_by_extension`` all answered "nothing found"
   for every game, with no error anywhere. The window is the whole index loop
   over every bundled definition.

2. **Built twice.** The check-then-set had no lock, so two threads could both
   see ``None``, both parse every YAML file, and one result was discarded.

``_reset_registry_cache()`` added a third: it set the global to ``None`` and then
called ``_refresh_import_snapshots()``, whose builders call straight back into
``_registry()`` -- during the window where the cache was ``None``.

The invariant: **whoever gets a registry gets a complete one, and the parse
happens once.**
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pkg.parity import parity_emulator_defs as defs


class RegistryPublicationTests(unittest.TestCase):
    def setUp(self):
        defs._reset_registry_cache()

    def test_a_reader_never_sees_a_partially_indexed_registry(self):
        """The severe one: publish-after-fill, observed through find_adapter."""
        real_load = defs.load_adapters
        observed = []
        stop = threading.Event()

        def slow_load():
            adapters = real_load()
            # Hold the index loop open by making the *first* mapping slow,
            # which is exactly the window the old code published in.
            if not stop.is_set():
                time.sleep(0.05)
            return adapters

        errors = []

        def reader():
            try:
                while not stop.is_set():
                    registry = defs._registry()
                    adapters = registry["adapters"]
                    by_id = registry["by_adapter_id"]
                    if adapters and not by_id:
                        observed.append(("empty index", len(adapters)))
                    if adapters and len(by_id) < len(adapters):
                        observed.append(("partial index", len(by_id), len(adapters)))
                    found = defs.find_adapter(adapter_id=registry["adapters"][0]["adapter_id"])
                    if not found:
                        observed.append(("find_adapter missed", len(adapters)))
            except Exception as error:  # noqa: BLE001 - recorded for the assert
                errors.append(error)

        with mock.patch.object(defs, "load_adapters", side_effect=slow_load):
            threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            time.sleep(0.6)
            stop.set()
            for thread in threads:
                thread.join(timeout=5.0)

        self.assertEqual(errors, [], f"a reader raised: {errors[:3]}")
        self.assertEqual(
            observed, [],
            f"a reader saw an incomplete registry {len(observed)} time(s): {observed[:5]}",
        )

    def test_the_definitions_are_parsed_exactly_once(self):
        """The check-then-set had no lock, so two threads both loaded."""
        calls = {"n": 0}
        real_load = defs.load_adapters
        start = threading.Barrier(8)

        def counting_load():
            calls["n"] += 1
            time.sleep(0.01)
            return real_load()

        defs._REGISTRY_CACHE = None
        with mock.patch.object(defs, "load_adapters", side_effect=counting_load):
            def worker():
                start.wait()
                defs._registry()

            threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10.0)

        self.assertEqual(
            calls["n"], 1,
            f"eight concurrent readers parsed the definitions {calls['n']} times",
        )

    def test_a_reset_does_not_expose_a_none_cache_to_a_concurrent_reader(self):
        """_reset re-enters _registry through every snapshot builder."""
        real_reset = defs._reset_registry_cache
        problems = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    registry = defs._registry()
                    if registry is None or "adapters" not in registry:
                        problems.append(registry)
            except Exception as error:  # noqa: BLE001 - recorded for the assert
                problems.append(error)

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        try:
            for _ in range(12):
                real_reset()
            stop.set()
            reader_thread.join(timeout=5.0)
        finally:
            stop.set()
        self.assertEqual(problems, [], f"a reader saw a broken registry: {problems[:3]}")

    def test_the_registry_is_usable_after_a_reset(self):
        defs._reset_registry_cache()
        registry = defs._registry()
        self.assertTrue(registry["adapters"], "the registry is empty after a reset")
        self.assertEqual(
            len(registry["by_adapter_id"]), len(registry["adapters"]),
            "the index does not cover every adapter",
        )


class RegistryCorrectnessTests(unittest.TestCase):
    def setUp(self):
        defs._reset_registry_cache()

    def test_every_adapter_is_reachable_from_every_index(self):
        registry = defs._registry()
        adapters = registry["adapters"]
        self.assertTrue(adapters)
        self.assertEqual(len(registry["by_adapter_id"]), len(adapters))
        for adapter in adapters:
            self.assertIn(
                adapter, registry["by_emulator_id"][adapter["emulator_id"]],
                f"{adapter['adapter_id']} is missing from by_emulator_id",
            )
            self.assertIn(adapter, registry["by_platform"][adapter["platform"]])
            for extension in adapter["extensions"]:
                self.assertIn(adapter, registry["by_extension"][extension])

    def test_the_indexes_are_sorted_recommended_first(self):
        registry = defs._registry()
        for key in ("by_emulator_id", "by_platform", "by_extension"):
            for value in registry[key].values():
                self.assertEqual(value, self._sorted(value), key)

    @staticmethod
    def _sorted(value):
        return sorted(
            value,
            key=lambda item: (not item["recommended"], item["priority"], item["adapter_id"]),
        )

    def test_find_adapter_still_works(self):
        registry = defs._registry()
        first = registry["adapters"][0]
        found = defs.find_adapter(adapter_id=first["adapter_id"])
        self.assertIsNotNone(found, "find_adapter missed an adapter that is in the registry")
        self.assertEqual(found["adapter_id"], first["adapter_id"])
        by_platform = registry["by_platform"]
        self.assertTrue(any(by_platform.values()), "no platform index has any adapter")

    def test_a_second_call_returns_the_same_object(self):
        self.assertIs(defs._registry(), defs._registry())


if __name__ == "__main__":
    unittest.main()
