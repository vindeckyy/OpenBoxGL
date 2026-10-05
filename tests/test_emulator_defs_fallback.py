#!/usr/bin/env python3
"""The stdlib YAML fallback must load the bundled definitions completely.

``parity_emulator_defs._parse_yaml`` uses PyYAML when it is importable and a
hand-rolled parser when it is not. That is a legitimate shape for an optional
accelerator -- but it is also a *silent divergence* risk, which is why
``scripts/check_dependencies.py`` lists ``yaml`` as a reviewed optional import
rather than allowing it freely: the two parsers do not have to agree, and
nothing else in the suite would notice if they stopped agreeing.

The fallback cannot handle everything YAML can. It deliberately does not
support a list nested under a mapping key, it treats an unquoted scalar as a
string, and it ignores block scalars. So the invariant is not "the fallback is a
YAML implementation" -- it is narrower and testable:

**every definition bundled with the app must load, through the fallback, with
the same required keys and value types the emulator-defs gate enforces.**

That is the property the app actually depends on, because the AppImage and the
Windows zip ship without PyYAML, so the fallback is the parser users get.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from pkg.parity import parity_emulator_defs as defs

ROOT = Path(__file__).resolve().parent.parent
DEFS_DIR = ROOT / "emulator_defs"

# Kept in step with scripts/check_emulator_defs.py::REQUIRED_KEYS. Duplicated
# deliberately: a test that imported the gate's constant could not detect the
# gate and the loader drifting apart, which is the failure being guarded.
REQUIRED_KEYS = (
    "emulator_id",
    "adapter_id",
    "label",
    "platform",
    "extensions",
    "executable_patterns",
)
# Keys that must be a list of strings, not a string or a dict.
LIST_KEYS = ("extensions", "executable_patterns")
# Keys that must be a non-empty string.
TEXT_KEYS = ("emulator_id", "adapter_id", "label", "platform")


def _load_with_fallback():
    """Force the stdlib fallback and load every bundled definition."""
    with mock.patch.object(defs, "yaml", None):
        adapters = defs.load_adapters()
    return adapters


class FallbackParserTests(unittest.TestCase):
    def test_pyyaml_is_optional(self):
        """The fallback must exist and be reachable, or the app needs PyYAML."""
        self.assertTrue(
            hasattr(defs, "_parse_yaml"),
            "parity_emulator_defs must keep a stdlib fallback parser",
        )
        parsed = defs._parse_yaml("key: value\nlist:\n  - a\n  - b\n")
        self.assertEqual(parsed["key"], "value")
        self.assertEqual(parsed["list"], ["a", "b"])

    def test_fallback_strips_quotes_and_skips_comments(self):
        text = '# a comment\n\nquoted: "hello"\nsingle: \'world\'\n'
        with mock.patch.object(defs, "yaml", None):
            parsed = defs._parse_yaml(text)
        self.assertEqual(parsed["quoted"], "hello")
        self.assertEqual(parsed["single"], "world")

    def test_fallback_handles_one_level_of_nested_mapping(self):
        text = "state:\n  kind: bios\n  template: \"{EmulatorDir}\"\n"
        with mock.patch.object(defs, "yaml", None):
            parsed = defs._parse_yaml(text)
        self.assertEqual(parsed["state"], {"kind": "bios", "template": "{EmulatorDir}"})


class BundledDefinitionsLoadThroughTheFallbackTests(unittest.TestCase):
    def test_every_bundled_definition_loads_with_required_keys(self):
        adapters = _load_with_fallback()
        self.assertTrue(adapters, "no adapters loaded through the stdlib fallback")

        broken = []
        for adapter in adapters:
            label = adapter.get("adapter_id") or adapter.get("emulator_id") or "?"
            for key in REQUIRED_KEYS:
                if key not in adapter or adapter[key] in (None, "", [], {}):
                    broken.append(f"{label}: missing or empty {key!r}")
            for key in TEXT_KEYS:
                if key in adapter and not isinstance(adapter[key], str):
                    broken.append(f"{label}: {key!r} should be a string, got {type(adapter[key]).__name__}")
            for key in LIST_KEYS:
                value = adapter.get(key)
                if key in adapter:
                    # A list of strings, or a bare string the loader splits.
                    if isinstance(value, str):
                        broken.append(f"{label}: {key!r} stayed a string ({value!r}); the fallback did not split it")
                    elif not isinstance(value, list):
                        broken.append(f"{label}: {key!r} should be a list, got {type(value).__name__}")
                    elif not all(isinstance(item, str) for item in value):
                        broken.append(f"{label}: {key!r} contains a non-string item")

        self.assertEqual(
            broken,
            [],
            "the stdlib YAML fallback lost data for the bundled definitions:\n  " + "\n  ".join(broken),
        )

    def test_fallback_loads_the_same_adapter_count_as_pyyaml(self):
        """If PyYAML is present, the two parsers must agree on the file set.

        Skipped when PyYAML is absent -- which is the case in CI and on every
        shipped build, and precisely why the assertions above exist.
        """
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed; the fallback-only assertions cover this build")
        with_fallback = _load_with_fallback()
        with_pyyaml = defs.load_adapters()
        self.assertEqual(
            sorted(item["adapter_id"] for item in with_fallback),
            sorted(item["adapter_id"] for item in with_pyyaml),
            "the stdlib fallback and PyYAML disagree on which adapters exist",
        )

    def test_fallback_populates_the_extension_index(self):
        """Launch resolution keys off extensions; an empty index means no game
        can ever be matched, which is the practical consequence of a parser
        that returns the right keys with the wrong values."""
        with mock.patch.object(defs, "yaml", None):
            with mock.patch.object(defs, "_REGISTRY_CACHE", None):
                registry = defs._registry()
        self.assertTrue(registry["by_extension"], "the extension index is empty under the fallback")
        self.assertEqual(len(registry["adapters"]), len(defs.load_adapters()))


if __name__ == "__main__":
    unittest.main()
