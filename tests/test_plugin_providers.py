#!/usr/bin/env python3
"""Plugin metadata/media provider tests (P10.2)."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins import set_plugin_enabled, set_plugin_permissions  # noqa: E402
from pkg.parity import parity_plugin_providers as providers  # noqa: E402
from pkg.parity.parity_media import load_media_queue  # noqa: E402


def _make_plugin(root, *, plugin_id="vendor.meta", permissions=("network",)):
    package = Path(root) / plugin_id
    package.mkdir(parents=True)
    (package / "plugin.py").write_text("pass\n", encoding="utf-8")
    manifest = {
        "id": plugin_id,
        "name": "Vendor Meta",
        "version": "1.0.0",
        "api_version": 1,
        "entry": "plugin.py",
        "hooks": ["metadata", "media"],
        "permissions": list(permissions),
        "capabilities": {
            "metadata": {"id": "vendor.metadata", "name": "Vendor Meta"},
            "media": {"id": "vendor.media", "name": "Vendor Media", "media_kinds": ["cover", "screenshots"]},
        },
    }
    (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return Path(root)


def _hook(result, calls=None):
    def runner(directory, plugin_id, hook, payload, *, timeout=None):
        if calls is not None:
            calls.append((hook, payload))
        if isinstance(result, Exception):
            raise result
        return result, ""
    return runner


def test_safe_media_url():
    def public(host, port):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    def private(host, port):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]
    assert providers.safe_media_url("https://cdn.example/a.jpg", resolver=public) == "https://cdn.example/a.jpg"
    assert providers.safe_media_url("https://cdn.example/a.jpg", resolver=False) == "https://cdn.example/a.jpg"
    for url, resolver in (
        ("http://cdn.example/a.jpg", public),
        ("https://user:pw@cdn.example/a.jpg", public),
        ("https://localhost/a.jpg", public),
        ("https://internal.example/a.jpg", private),
        ("https://10.0.0.1/a.jpg", False),
        ("https://127.0.0.1/a.jpg", False),
        ("", public),
    ):
        try:
            providers.safe_media_url(url, resolver=resolver)
            raise AssertionError(f"{url!r} must be rejected")
        except ValueError:
            pass


def test_metadata_search_validates_candidates():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        candidates = [
            {
                "id": "m1",
                "name": "Candidate One",
                "platform": "PC",
                "fields": {"genre": "Action", "path": "/etc/passwd", "rating": 4.5},
                "media": {
                    "cover": [
                        "https://img.example/a.jpg",
                        "http://img.example/b.jpg",
                        "https://10.0.0.1/c.jpg",
                    ],
                },
            },
            {"id": "no-name"},
            "bad",
        ]
        calls = []
        with mock.patch.object(providers, "run_plugin_hook", _hook({"candidates": candidates}, calls)):
            result = providers.plugin_metadata_search(
                "vendor.meta", {"game_id": "g1", "name": "Game", "path": "/secret"}, limit=5,
                plugins_dir=root)
        assert len(result) == 1
        assert result[0]["id"] == "m1"
        assert result[0]["source"] == "Vendor Meta"
        assert result[0]["fields"] == {"genre": "Action", "rating": 4.5}
        assert result[0]["media"]["cover"] == ["https://img.example/a.jpg"]
        assert calls[0][0] == "metadata"
        assert calls[0][1]["game"]["game_id"] == "g1"
        assert "path" not in calls[0][1]["game"]
        assert calls[0][1]["limit"] == 5


def test_metadata_search_caps_candidates_and_media():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        candidates = [
            {
                "id": f"m{index}",
                "name": f"Candidate {index}",
                "media": {"cover": [f"https://img.example/{index}-{n}.jpg" for n in range(20)]},
            }
            for index in range(40)
        ]
        with mock.patch.object(providers, "run_plugin_hook", _hook({"candidates": candidates})):
            result = providers.plugin_metadata_search("vendor.meta", {"game_id": "g1"}, plugins_dir=root)
        assert len(result) == 20
        assert all(len(candidate["media"]["cover"]) <= 10 for candidate in result)


def test_metadata_search_hook_error_returns_empty():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        with mock.patch.object(providers, "run_plugin_hook", _hook(ValueError("boom"))):
            assert providers.plugin_metadata_search(
                "vendor.meta", {"game_id": "g1"}, plugins_dir=root) == []
        with mock.patch.object(providers, "run_plugin_hook", lambda *a, **k: (None, "boom")):
            assert providers.plugin_metadata_search("vendor.meta", {"game_id": "g1"}, plugins_dir=root) == []


def test_metadata_apply_preserves_fields_and_queues_media():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        queue = Path(directory) / "media-queue.json"
        response = {
            "fields": {"genre": "New Genre", "path": "/nope", "developer": "Studio"},
            "media": {"cover": ["https://img.example/cover.jpg", "http://img.example/bad.jpg"]},
        }
        calls = []
        game = {"game_id": "g1", "name": "Game", "genre": "Old Genre"}
        with mock.patch.object(providers, "run_plugin_hook", _hook(response, calls)):
            providers.plugin_metadata_apply("vendor.meta", game, "m1", plugins_dir=root, queue_path=queue)
        assert game["genre"] == "Old Genre"
        assert game["developer"] == "Studio"
        assert game["metadata_provider"] == "plugin:vendor.meta"
        assert "path" not in game
        assert "cover" not in game
        assert calls[0][1]["candidate_id"] == "m1"
        items = load_media_queue(queue)
        assert len(items) == 1
        assert items[0]["game_id"] == "g1"
        assert items[0]["source"] == "plugin:vendor.meta"
        assert items[0]["kind"] == "cover"
        assert items[0]["url"] == "https://img.example/cover.jpg"

        with mock.patch.object(providers, "run_plugin_hook", _hook(response)):
            providers.plugin_metadata_apply(
                "vendor.meta", game, "m1", overwrite=True, plugins_dir=root,
                queue_path=Path(directory) / "second-queue.json")
        assert game["genre"] == "New Genre"


def test_metadata_apply_hook_error_raises():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        with mock.patch.object(providers, "run_plugin_hook", lambda *a, **k: (None, "boom")):
            try:
                providers.plugin_metadata_apply("vendor.meta", {"game_id": "g1"}, "m1", plugins_dir=root)
                raise AssertionError("apply errors must be visible")
            except ValueError as error:
                assert "boom" in str(error)


def test_media_search_filters_and_caps():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        response = {
            "candidates": [
                {"url": "https://img.example/a.jpg", "kind": "cover", "title": "A", "width": 600},
                {"url": "http://img.example/b.jpg", "kind": "cover"},
                {"url": "https://10.0.0.1/c.jpg", "kind": "cover"},
                "bad",
            ],
        }
        with mock.patch.object(providers, "run_plugin_hook", _hook(response)):
            result = providers.plugin_media_search("vendor.meta", {"game_id": "g1"}, "cover", plugins_dir=root)
        assert len(result) == 1
        assert result[0]["url"] == "https://img.example/a.jpg"
        assert result[0]["width"] == 600

        with mock.patch.object(providers, "run_plugin_hook", lambda *a, **k: (None, "boom")):
            assert providers.plugin_media_search("vendor.meta", {"game_id": "g1"}, "cover", plugins_dir=root) == []

        try:
            providers.plugin_media_search("vendor.meta", {"game_id": "g1"}, "background", plugins_dir=root)
            raise AssertionError("undeclared media kind must be refused")
        except ValueError:
            pass


def test_queued_plugin_media_url_skips_provider_search():
    from pkg.parity.parity_media import download_media_for_item

    with TemporaryDirectory() as directory:
        item = {
            "source": "plugin:vendor.meta",
            "kind": "cover",
            "url": "https://img.example/queued.jpg",
        }
        with mock.patch.object(providers, "plugin_media_search", side_effect=AssertionError("must not search")), \
             mock.patch.object(providers, "download_plugin_media", return_value="/tmp/cover.jpg") as download:
            result = download_media_for_item(
                {"game_id": "g1", "name": "Game"},
                "cover",
                item,
                {},
                data_dir=directory,
            )
        assert result == "/tmp/cover.jpg"
        download.assert_called_once()
        assert download.call_args.args[3] == {"url": "https://img.example/queued.jpg"}


def test_metadata_candidate_inherits_capability_platform():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        package = root / "vendor.meta" / "plugin.json"
        manifest = json.loads(package.read_text(encoding="utf-8"))
        manifest["capabilities"]["metadata"]["platform"] = "Switch"
        package.write_text(json.dumps(manifest), encoding="utf-8")
        response = {"candidates": [{"id": "m1", "name": "Game"}]}
        with mock.patch.object(providers, "run_plugin_hook", _hook(response)):
            candidates = providers.plugin_metadata_search(
                "vendor.meta", {"game_id": "g1", "name": "Game"}, plugins_dir=root,
            )
        assert candidates[0]["platform"] == "Switch"


def test_network_grant_required():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        for call in (
            lambda: providers.plugin_metadata_search("vendor.meta", {"game_id": "g1"}, plugins_dir=root),
            lambda: providers.plugin_metadata_apply("vendor.meta", {"game_id": "g1"}, "m1", plugins_dir=root),
            lambda: providers.plugin_media_search("vendor.meta", {"game_id": "g1"}, "cover", plugins_dir=root),
        ):
            try:
                call()
                raise AssertionError("missing network grant must be refused")
            except ValueError as error:
                assert "grant network" in str(error).lower()


def test_disabled_plugin_rejected():
    with TemporaryDirectory() as directory:
        root = _make_plugin(Path(directory) / "plugins")
        set_plugin_permissions(root, "vendor.meta", ["network"])
        set_plugin_enabled(root, "vendor.meta", False)
        try:
            providers.plugin_metadata_search("vendor.meta", {"game_id": "g1"}, plugins_dir=root)
            raise AssertionError("disabled plugin must be refused")
        except ValueError as error:
            assert "disabled" in str(error).lower()


class _Response:
    status = 200
    headers = {"Content-Type": "image/png", "Content-Length": "4"}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        body, self.body = self.body, b""
        return body


def test_download_plugin_media_uses_download_file():
    with TemporaryDirectory() as directory:
        destination = Path(directory) / "out"
        seen = {}

        def opener(request, timeout=None):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return _Response(b"PNG!")

        path = providers.download_plugin_media(
            "vendor.meta", {"name": "Some Game"}, "cover",
            {"url": "https://img.example/cover.png"}, destination,
            opener=opener, timeout=7)
        stored = Path(path)
        assert stored.exists()
        assert stored.read_bytes() == b"PNG!"
        assert stored.parent == destination
        assert seen["url"] == "https://img.example/cover.png"
        assert seen["timeout"] == 7


if __name__ == "__main__":
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"ok {name}")
    print("test_plugin_providers.py OK")
