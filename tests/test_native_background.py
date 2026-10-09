#!/usr/bin/env python3
"""The native windows' first-paint background (handlers/extensions.write_native_background).

Both native hosts read the file this writes before the page loads, so a light theme does
not flash the dark default. These tests run against a temporary data folder only.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pkg.parity  # noqa: F401,E402  (flat-import finder)
from handlers import extensions as ext  # noqa: E402


class NativeBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.themes.mkdir()
        patcher = mock.patch.object(ext, "DATA", self.root / "library.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def written(self):
        return (self.root / ext.NATIVE_BACKGROUND_FILE).read_text(encoding="utf-8").strip()

    def test_no_theme_writes_the_built_in_background(self):
        self.assertEqual(ext.write_native_background(""), ext.DEFAULT_NATIVE_BACKGROUND)
        self.assertEqual(self.written(), ext.DEFAULT_NATIVE_BACKGROUND)

    def test_a_theme_background_is_recorded_in_lower_case(self):
        (self.themes / "paper.css").write_text(":root {\n  --bg: #F5F1E8;\n}\n", encoding="utf-8")
        self.assertEqual(ext.write_native_background("paper"), "#f5f1e8")
        self.assertEqual(self.written(), "#f5f1e8")

    def test_a_theme_without_a_background_keeps_the_default(self):
        (self.themes / "plain.css").write_text(":root { --brand: #123456; }", encoding="utf-8")
        self.assertEqual(ext.write_native_background("plain"), ext.DEFAULT_NATIVE_BACKGROUND)

    def test_a_background_token_with_a_longer_name_is_not_mistaken_for_it(self):
        (self.themes / "tricky.css").write_text(":root { --bg-deep: #000000; }", encoding="utf-8")
        self.assertEqual(ext.write_native_background("tricky"), ext.DEFAULT_NATIVE_BACKGROUND)

    def test_a_path_like_name_never_leaves_the_themes_folder(self):
        outside = self.root / "outside.css"
        outside.write_text(":root { --bg: #ffffff; }", encoding="utf-8")
        self.assertEqual(ext.write_native_background("../outside"), ext.DEFAULT_NATIVE_BACKGROUND)

    def test_a_missing_theme_keeps_the_default(self):
        self.assertEqual(ext.write_native_background("nope"), ext.DEFAULT_NATIVE_BACKGROUND)


def _helper_source(path):
    """The apply_saved_background() definition, copied out of the real host source."""
    text = (ROOT / path).read_text(encoding="utf-8")
    start = text.index("static void\napply_saved_background(")
    start = text.rfind("\n/*", 0, start) + 1
    return text[start:text.index("\n}\n", start) + 3]


class NativeHostFirstPaintTests(unittest.TestCase):
    """Each host's helper, compiled from its own source and run against a saved file.

    The Linux helper needs GLib; the Windows helper is compiled against stand-in WebView2 types
    (so it runs anywhere gcc does), which checks the parsing and fallbacks, not the Win32 calls.
    """

    def _compile_and_run(self, name, source, prelude, driver, data):
        import shutil
        import subprocess

        if not shutil.which("gcc"):
            self.skipTest("gcc is not available")
        with tempfile.TemporaryDirectory() as build:
            src = Path(build) / f"{name}.c"
            src.write_text(prelude + source + driver, encoding="utf-8")
            exe = Path(build) / name
            command = ["gcc", "-Wall", "-Wextra", "-Werror", str(src), "-o", str(exe)]
            if name == "linux_helper":
                if not shutil.which("pkg-config"):
                    self.skipTest("pkg-config is not available")
                if subprocess.run(["pkg-config", "--exists", "glib-2.0"], check=False).returncode:
                    self.skipTest("GLib development files are not installed")
                flags = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0"], capture_output=True, text=True, check=True).stdout
                command += flags.split()
            build_result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(build_result.returncode, 0, build_result.stderr)
            run = subprocess.run([str(exe), data], capture_output=True, text=True, check=True)
            return run.stdout.split()

    def test_linux_host_paints_the_saved_background_and_keeps_the_default_otherwise(self):
        prelude = (
            "#include <glib.h>\n#include <stdio.h>\n"
            "typedef struct { double red, green, blue, alpha; } GdkRGBA;\n"
            "static const char *data_dir = NULL;\n"
        )
        driver = (
            "\nint main(int argc, char **argv) { (void)argc;\n"
            "  GdkRGBA bg = {0.067, 0.063, 0.055, 1.0};\n"
            "  data_dir = argv[1];\n  apply_saved_background(&bg);\n"
            "  printf(\"%d %d %d\\n\", (int)(bg.red * 255 + 0.5), (int)(bg.green * 255 + 0.5), (int)(bg.blue * 255 + 0.5));\n"
            "  return 0;\n}\n"
        )
        source = _helper_source("native_host.c")
        with tempfile.TemporaryDirectory() as data:
            (Path(data) / "native-background").write_text("#f5f1e8\n", encoding="utf-8")
            self.assertEqual(self._compile_and_run("linux_helper", source, prelude, driver, data), ["245", "241", "232"])
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(self._compile_and_run("linux_helper", source, prelude, driver, empty), ["17", "16", "14"])

    def test_windows_host_paints_the_saved_background_and_keeps_the_default_otherwise(self):
        prelude = (
            "#include <stdio.h>\n#include <stdlib.h>\n#include <string.h>\n#include <wchar.h>\n"
            "typedef unsigned char BYTE;\n"
            "typedef struct { BYTE A; BYTE R; BYTE G; BYTE B; } COREWEBVIEW2_COLOR;\n"
            "static wchar_t *g_data_dir_wide;\n"
            "static FILE *test_wfopen(const wchar_t *path, const wchar_t *mode) {\n"
            "  char narrow[1024]; char m[4] = {0};\n"
            "  wcstombs(narrow, path, sizeof(narrow)); wcstombs(m, mode, sizeof(m));\n"
            "  for (char *c = narrow; *c; c++) if (*c == '\\\\') *c = '/';\n"
            "  return fopen(narrow, m);\n}\n"
            "#define _wfopen(p, m) test_wfopen(p, m)\n"
        )
        driver = (
            "\nint main(int argc, char **argv) { (void)argc;\n"
            "  COREWEBVIEW2_COLOR c = { 255, 17, 16, 14 };\n"
            "  static wchar_t dir[512];\n  mbstowcs(dir, argv[1], 512);\n  g_data_dir_wide = dir;\n"
            "  apply_saved_background(&c);\n"
            "  printf(\"%u %u %u\\n\", c.R, c.G, c.B);\n  return 0;\n}\n"
        )
        source = _helper_source("native_host_win.c")
        with tempfile.TemporaryDirectory() as data:
            (Path(data) / "native-background").write_text("#f5f1e8\n", encoding="utf-8")
            self.assertEqual(self._compile_and_run("win_helper", source, prelude, driver, data), ["245", "241", "232"])
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(self._compile_and_run("win_helper", source, prelude, driver, empty), ["17", "16", "14"])


if __name__ == "__main__":
    unittest.main()
