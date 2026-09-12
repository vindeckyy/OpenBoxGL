"""openbox:// URI parsing and local launcher helpers."""

import json
import sys
import time
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
            bare = authority.casefold()
            if bare not in {"", "localhost", "openbox"} and ("." in authority or ":" in authority):
                return {"action": "unknown"}
            # A word authority is the action itself (openbox://resume/<id>); only
            # dotted/port-bearing authorities are rejected as foreign hosts.
            text = rest if bare not in {"", "localhost", "openbox"} else (path if sep else authority)
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
        payload["id"] = urllib.parse.unquote(remainder).strip()
    elif action in {"resume", "moment", "clip"}:
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
    if action in {"resume", "moment", "clip"}:
        game_id = urllib.parse.quote(str(params.get("id", "")))
        return f"{base}/?deeplink={action}&id={game_id}"
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
    if "--help" in args or "-h" in args:
        print("Usage: openbox [OPTIONS]")
        print("       python3 web_app.py [OPTIONS]\n")
        print("Options:")
        print("  -h, --help                 Show this help message and exit")
        print("  --web                      Launch loopback web server and open in browser")
        print("  --no-browser               Start server without opening a browser window")
        print("  --app-window               Open UI in a standalone app window (web mode)")
        print("  --no-app-window            Open UI in a normal browser tab (web mode)")
        print("  --game-mode                Force Steam Game Mode / Gamescope guest mode")
        print("  --fullscreen-width <W>     Custom viewport width in kiosk/app mode")
        print("  --fullscreen-height <H>    Custom viewport height in kiosk/app mode")
        print("  --resolution <WxH>         Custom viewport resolution (e.g. 1920x1080)")
        print("  --play <id>                Launch a game by stable id or numeric id")
        print("  --uri <uri>                Dispatch an openbox:// deep link URI")
        print("  --launcher                 Run the keyboard quick launcher (rofi/wofi)")
        print("  --backup [--items <list>]  Create a backup archive")
        print("  --restore-backup <file>    Restore data from a backup archive")
        return 0
    if "--uri" in args:
        index = args.index("--uri")
        if index + 1 >= len(args):
            print("Usage: openbox --uri openbox://search/quake", file=sys.stderr)
            return 2
        return dispatch_uri(args[index + 1], data_dir)
    if "--play" in args:
        index = args.index("--play")
        if index + 1 >= len(args):
            print("Usage: openbox --play <game-id>", file=sys.stderr)
            return 2
        game_id = str(args[index + 1]).strip()
        if not game_id:
            print("Usage: openbox --play <game-id>", file=sys.stderr)
            return 2
        return dispatch_uri(
            f"openbox://launch/{urllib.parse.quote(game_id, safe='')}",
            data_dir,
        )
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
    if port is None:
        port = read_port_file(data_dir)
    parsed = parse_uri(uri)
    action = parsed.get("action", "start")
    if action == "start":
        # No server yet: fall through so web_app.main() boots normally.
        if not token_path.is_file() or not port:
            return None
        if open_browser:
            try:
                import webbrowser

                webbrowser.open(build_launch_url(f"http://{host}:{port}/?token={token or ''}", "start"))
            except Exception:
                pass
        return 0
    needs_token = action in {"launch", "resume"}
    if not port or (needs_token and not token):
        port, token = _start_server_and_wait(data_dir, host=host, token=token)
        if not port:
            print("OpenBox could not start its local server.", file=sys.stderr)
            return 1
    try:
        if action in {"showgame", "game", "launch"}:
            game_id = str(parsed.get("id", "")).strip()
            if not game_id:
                raise ValueError("Game id is required.")
            if action == "launch":
                body = {"id": int(game_id)} if game_id.isdigit() else {"game_id": game_id}
                api_request(host, port, token, "/api/launch", "POST", body)
            else:
                url = build_launch_url(f"http://{host}:{port}", "showgame", id=game_id)
                if open_browser:
                    import webbrowser
                    webbrowser.open(url)
                else:
                    print(url)
            return 0
        if action == "resume":
            game_id = str(parsed.get("id", "")).strip()
            if not game_id:
                raise ValueError("Game id is required.")
            body = {"id": int(game_id)} if game_id.isdigit() else {"game_id": game_id}
            api_request(host, port, token, "/api/v2/resume", "POST", body)
            return 0
        if action in {"moment", "clip"}:
            # Dispatch shells: the in-app consumers land with T1-ui (moment) and
            # T3-obs (clip); open the deeplink URL so the SPA handles it.
            url = build_launch_url(
                f"http://{host}:{port}", action, id=str(parsed.get("id", "")).strip()
            )
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


def _server_token(data_dir):
    try:
        return (Path(data_dir) / "server.token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _server_responds(host, port, token):
    if not port or not token:
        return False
    try:
        api_request(host, port, token, "/api/health")
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return True


def _start_server_and_wait(data_dir, *, host="127.0.0.1", token=""):
    """Boot a detached loopback server for deep links issued before startup."""
    data_dir = Path(data_dir)
    current_port = read_port_file(data_dir)
    current_token = token or _server_token(data_dir)
    if _server_responds(host, current_port, current_token):
        return current_port, current_token

    # These are ephemeral readiness markers, not user data.  Remove only
    # regular files so a stale launch cannot be mistaken for a fresh server.
    for name in ("server.port", "server.token"):
        marker = data_dir / name
        try:
            if marker.is_file() and not marker.is_symlink():
                marker.unlink()
        except OSError:
            pass
    root = Path(__file__).resolve().parents[2]
    web_app = root / "web_app.py"
    if not web_app.is_file():
        return 0, ""
    try:
        import subprocess

        subprocess.Popen(
            [sys.executable, str(web_app), "--no-browser"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return 0, ""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        current_port = read_port_file(data_dir)
        current_token = _server_token(data_dir)
        if _server_responds(host, current_port, current_token):
            return current_port, current_token
        time.sleep(0.05)
    return 0, ""


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
