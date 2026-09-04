#!/usr/bin/env python3
"""Lint POST handlers: parsed payload arg + no re-read of consumed body.

M2-hang class: web_app._do_POST reads rfile exactly once and dispatches the
already-parsed JSON body. A POST handler that calls self.body() (or reads
self.rfile) re-reads an exhausted stream and blocks until socket timeout.

Rules (stdlib only, AST-based):
  - every function decorated with @route("POST", ...) must accept a parsed
    payload argument (at least (self, payload) for methods, (payload) for
    plain functions);
  - its body must not contain self.body() or self.rfile reads.

Usage: python3 scripts/check_handlers_payload.py [handlers_dir]
Exits 0 when clean, 1 with file:line messages otherwise.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _is_post_route(decorator: ast.AST) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    func = decorator.func
    name = ""
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    if name != "route":
        return False
    if not decorator.args:
        return False
    first = decorator.args[0]
    return isinstance(first, ast.Constant) and str(first.value).upper() == "POST"


def check_source(filename: str, source: str) -> list[str]:
    """Return lint error strings for one module's source."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        return [f"{filename}:{error.lineno}: syntax error: {error.msg}"]
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_post_route(d) for d in node.decorator_list):
            continue
        args = [a.arg for a in node.args.args]
        # Drop self/cls for methods; require one parsed-payload param.
        params = args[1:] if args and args[0] in {"self", "cls"} else list(args)
        # Allow *args-free single payload param; bound methods need >=1.
        if not params and not node.args.vararg:
            errors.append(
                f"{filename}:{node.lineno}: POST handler "
                f"'{node.name}' must take a parsed payload arg "
                f"(e.g. def {node.name}(self, payload))"
            )
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "body"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "self"
                ):
                    errors.append(
                        f"{filename}:{child.lineno}: POST handler "
                        f"'{node.name}' must not call self.body() "
                        f"(body already parsed by web_app._do_POST)"
                    )
            if isinstance(child, ast.Attribute):
                if (
                    child.attr == "rfile"
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "self"
                ):
                    errors.append(
                        f"{filename}:{child.lineno}: POST handler "
                        f"'{node.name}' must not read self.rfile "
                        f"(body already parsed by web_app._do_POST)"
                    )
                    break
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for error in errors:
        if error not in seen:
            seen.add(error)
            unique.append(error)
    return unique


def check_tree(handlers_dir: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted(handlers_dir.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as error:
            errors.append(f"{path}: unreadable: {error}")
            continue
        rel = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        errors.extend(check_source(rel, source))
    return errors


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "handlers"
    if not target.is_dir():
        print(f"missing handlers dir: {target}", file=sys.stderr)
        return 1
    errors = check_tree(target)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: POST handlers take parsed payload, no body re-read ({target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
