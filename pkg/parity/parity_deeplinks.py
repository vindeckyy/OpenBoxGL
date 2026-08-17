"""openbox:// URI parsing and local launcher helpers."""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from backend_io import read_limited


SCHEME = "openbox"


def parse_uri(uri):
    text = str(uri).strip()
    if text.startswith(f"{SCHEME}://"):
        # Allow an empty/localhost authority or a bare action segment (openbox://search/foo), never a foreign host.
        rest = text[len(f"{SCHEME}://") :]
        authority, sep, path = rest.partition("/")
        if authority and not sep:
            # No path: either a bare action (openbox://bigbox) or a host.
            if "." in authority or ":" in authority:
                return {"action": "unknown"}
            if authority.casefold() in {"", "localhost", "openbox"}:
                text = path
            else:
                text = authority
        else:
            known = {"start", "search", "showgame", "game", "launch", "bigbox", "fullscreen", "settings"}
            if authority.casefold() not in {"", "localhost", "openbox"} and authority.casefold() not in known:
                return {"action": "unknown"}
            # A known action as authority is the bare form; keep the remainder as the action path.
            text = rest if authority.casefold() in known else (path if sep else authority)
    elif text.startswith(f"{SCHEME}:"):
        text = text[len(f"{SCHEME}:") :]
    text = text.lstrip("/")
    if not text:
        return {"action": "start"}
    parts = text.split("/", 1)
    action = parts[0].casefold()
    remainder = parts[1] if len(parts) > 1 else ""
    payload = {"action": action}
    if action in {"showgame", "game"}:
        payload["id"] = remainder.strip()
    elif action == "search":
        payload["query"] = urllib.parse.unquote(remainder)
    elif action == "launch":
        payload["id"] = remainder.strip()
    elif action in {"bigbox", "fullscreen"}:
        payload["mode"] = "bigbox"
    elif action == "settings":
        payload["panel"] = remainder.strip() or "general"
    return payload


def build_launch_url(base_url, action, **params):
    base = base_url.rstrip("/")
    if action == "start":
        return base
    if action in {"showgame", "launch", "game"}:
        game_id = params.get("id", "")
        return f"{base}/?deeplink=showgame&id={urllib.parse.quote(str(game_id))}"
    if action == "search":
        query = urllib.parse.quote(str(params.get("query", "")))
        return f"{base}/?deeplink=search&q={query}"
    if action in {"bigbox", "fullscreen"}:
        return f"{base}/?deeplink=bigbox"
    if action == "settings":
        return f"{base}/?deeplink=settings"
    return base


def api_request(host, port, token, path, method="GET", body=None):
    data = None
    headers = {"X-OpenBox-Token": token}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(read_limited(response, 4 * 1024 * 1024).decode())


def handle_cli(argv, data_dir):
    """Return exit code when argv handled; None to continue normal startup."""
    args = list(argv)
    if "--uri" in args:
        index = args.index("--uri")
        if index + 1 >= len(args):
            print("Usage: openbox --uri openbox://search/quake", file=sys.stderr)
            return 2
        return dispatch_uri(args[index + 1], data_dir)
    for arg in args:
        if str(arg).startswith(f"{SCHEME}:"):
            return dispatch_uri(arg, data_dir, open_browser=True)
    if "--launcher" in args:
        return run_keyboard_launcher(data_dir)
    return None


def dispatch_uri(uri, data_dir, host="127.0.0.1", port=None, token=None, open_browser=False):
    token_path = Path(data_dir) / "server.token"
    if token is None and token_path.is_file():
        token = token_path.read_text().strip()
    parsed = parse_uri(uri)
    action = parsed.get("action", "start")
    if action == "start":
        # No server yet: fall through so web_app.main() boots normally.
        if not token_path.is_file() or not read_port_file(data_dir):
            return None
        if open_browser:
            try:
                import webbrowser

                webbrowser.open(build_launch_url(f"http://{host}:{port}/?token={token}", "start"))
            except Exception:
                pass
        return 0
    if port is None:
        port = read_port_file(data_dir)
    if not port:
        print("OpenBox is not running (no server port found). Start OpenBox first.", file=sys.stderr)
        return 1
    try:
        if action in {"showgame", "game", "launch"}:
            game_id = parsed.get("id", "")
            if not str(game_id).isdigit():
                raise ValueError("Game id is required.")
            if action == "launch":
                api_request(host, port, token, "/api/launch", "POST", {"id": int(game_id)})
            else:
                url = build_launch_url(f"http://{host}:{port}", "showgame", id=game_id)
                if open_browser:
                    import webbrowser
                    webbrowser.open(url)
                else:
                    print(url)
            return 0
        if action == "search":
            query = parsed.get("query", "")
            url = build_launch_url(f"http://{host}:{port}", "search", query=query)
            if open_browser:
                import webbrowser
                webbrowser.open(url)
            else:
                print(url)
            return 0
        if action in {"bigbox", "fullscreen"}:
            url = build_launch_url(f"http://{host}:{port}", "bigbox")
            if open_browser:
                import webbrowser
                webbrowser.open(url)
            else:
                print(url)
            return 0
        if action == "settings":
            url = build_launch_url(f"http://{host}:{port}", "settings")
            if open_browser:
                import webbrowser
                webbrowser.open(url)
            else:
                print(url)
            return 0
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Unknown deeplink action: {action}", file=sys.stderr)
    return 1


def run_keyboard_launcher(data_dir):
    """Query rofi/wofi for a command and dispatch it."""
    picker = None
    for candidate in ("rofi", "wofi", "dmenu"):
        if Path(f"/usr/bin/{candidate}").exists() or __import__("shutil").which(candidate):
            picker = candidate
            break
    if not picker:
        print("Install rofi, wofi, or dmenu for the keyboard launcher.", file=sys.stderr)
        return 1
    lines = [
        "#bigbox\tOpen Big Box",
        "#settings\tOpen Settings",
        "/search\tSearch library",
        "/refresh\tRefresh library imports",
    ]
    root = Path(__file__).resolve().parent
    scripts = (root / "scripts" / "openbox-launcher.sh", root / "openbox-launcher.sh")
    script = next((candidate for candidate in scripts if candidate.is_file()), None)
    if script:
        import subprocess
        try:
            return subprocess.call([str(script), picker], timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(str(error), file=sys.stderr)
            return 1
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as handle:
        handle.write("\n".join(lines))
        menu_file = handle.name
    try:
        if picker == "rofi":
            selection = subprocess.check_output(
                ["rofi", "-dmenu", "-i", "-p", "OpenBox", "-kb-custom-1", "Control-Return"],
                input="\n".join(lines).encode(),
                stderr=subprocess.DEVNULL, timeout=30,
            ).decode().strip()
        elif picker == "wofi":
            selection = subprocess.check_output(
                ["wofi", "--dmenu", "--prompt", "OpenBox"],
                input="\n".join(lines).encode(),
                stderr=subprocess.DEVNULL, timeout=30,
            ).decode().strip()
        else:
            selection = subprocess.check_output(
                ["dmenu", "-i", "-p", "OpenBox"],
                input="\n".join(lines).encode(),
                stderr=subprocess.DEVNULL, timeout=30,
            ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    finally:
        Path(menu_file).unlink(missing_ok=True)
    if not selection:
        return 0
    if selection.startswith("#bigbox"):
        print("openbox://bigbox")
    elif selection.startswith("#settings"):
        print("openbox://settings")
    elif selection.startswith("/search"):
        query = input("Search: ").strip()
        if query:
            print(f"openbox://search/{urllib.parse.quote(query)}")
    elif selection.startswith("/refresh"):
        print("openbox://start")
    else:
        print(selection)
    return 0


def read_port_file(data_dir):
    port_file = Path(data_dir) / "server.port"
    if port_file.is_file():
        try:
            return int(port_file.read_text().strip())
        except ValueError:
            pass
    return 0


def launcher_menu_items(games):
    items = [
        {"id": "bigbox", "label": "Open Big Box"},
        {"id": "settings", "label": "Open Settings"},
        {"id": "search", "label": "Search library"},
    ]
    for game in games[:40]:
        if isinstance(game, dict) and game.get("name"):
            items.append({"id": f"launch:{game.get('id', 0)}", "label": game.get("name", "Game")})
    return items
