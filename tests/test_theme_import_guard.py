#!/usr/bin/env python3
"""P1-20: theme ``@import`` may only reference local, same-origin stylesheets.

The previous ``"@import" in text and "http" in text`` guard missed
protocol-relative URLs such as ``//evil.example/x.css``.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from handlers import extensions  # noqa: E402
from handlers.extensions import (  # noqa: E402
    ExtensionsHandlers,
    _import_url_is_local,
    _validate_theme_imports,
)


class ThemeImportValidationTests(unittest.TestCase):
    def test_protocol_relative_imports_are_rejected(self):
        for template in (
            '@import url("//evil.example/x.css");',
            '@import "//evil.example/x.css";',
            "@import url(//evil.example/x.css);",
            "@import url( //evil.example/x.css );",
        ):
            with self.subTest(template=template):
                with self.assertRaises(ValueError):
                    _validate_theme_imports(template)

    def test_absolute_and_scheme_imports_are_rejected(self):
        for template in (
            '@import url("https://evil.example/x.css");',
            "@import url(http://evil.example/x.css);",
            '@import "http://evil.example/x.css";',
            '@import "/absolute/local.css";',
            "@import url(/absolute/local.css);",
            '@import "data:text/css,body{}";',
            "@import url(javascript:alert(1));",
            '@import "\\\\evil\\share\\theme.css";',
            '@import "../../outside.css";',
        ):
            with self.subTest(template=template):
                with self.assertRaises(ValueError):
                    _validate_theme_imports(template)

    def test_relative_imports_are_allowed(self):
        for template in (
            '@import "base.css";',
            "@import url(./theme/base.css);",
            '@import url("nested/part.css") screen;',
            "body { color: black; }",
        ):
            with self.subTest(template=template):
                _validate_theme_imports(template)

    def test_helper_classifies_urls(self):
        self.assertTrue(_import_url_is_local("base.css"))
        self.assertTrue(_import_url_is_local("nested/base.css"))
        self.assertFalse(_import_url_is_local("//evil.example/x.css"))
        self.assertFalse(_import_url_is_local("https://evil.example/x.css"))
        self.assertFalse(_import_url_is_local("/absolute.css"))
        self.assertFalse(_import_url_is_local("nested\\part.css"))
        self.assertFalse(_import_url_is_local(""))
        self.assertFalse(_import_url_is_local(None))


class ImportThemeHandlerTests(unittest.TestCase):
    def _handler(self):
        handler = object.__new__(ExtensionsHandlers)
        handler.send_json = mock.Mock()
        return handler

    def test_handler_rejects_protocol_relative_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "evil.css"
            source.write_text('@import url("//evil.example/x.css");\n', encoding="utf-8")
            handler = self._handler()
            with mock.patch.object(extensions, "DATA", root / "library.json"):
                with self.assertRaises(ValueError):
                    handler.import_theme({"path": str(source)})
            handler.send_json.assert_not_called()
            self.assertFalse((root / "themes" / "evil.css").exists())

    def test_handler_accepts_relative_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            themes = root / "themes"
            themes.mkdir()
            source = root / "local.css"
            source.write_text('@import "base.css";\nbody { color: #fff; }\n', encoding="utf-8")
            handler = self._handler()
            with mock.patch.object(extensions, "DATA", root / "library.json"):
                handler.import_theme({"path": str(source)})
            self.assertTrue((themes / "local.css").is_file())
            handler.send_json.assert_called_once_with(200, {"theme": "local"})


if __name__ == "__main__":
    unittest.main()
