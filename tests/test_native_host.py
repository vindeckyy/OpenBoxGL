import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

class TestNativeHost(unittest.TestCase):
    def _compile_argument_harness(self, directory):
        """Compile the native argument/dispatch helpers without opening GTK."""
        root_dir = Path(__file__).resolve().parent.parent
        harness = Path(directory) / "native_argument_harness.c"
        harness.write_text(
            r'''
#define main native_host_program_main
#include "@@NATIVE_SOURCE@@"
#undef main

static void emit_request(const char *name, int argc, char **argv) {
    NativeRequest request = {NATIVE_REQUEST_NONE, NULL};
    NativeArgsResult result = native_request_from_argv(argc, argv, &request);
    printf("%s_result=%d\n", name, result);
    if (result == NATIVE_ARGS_REQUEST) {
        char *url = native_authenticated_url(&request);
        char *ipc = native_request_to_uri(&request);
        printf("%s_url=%s\n", name, url ? url : "");
        printf("%s_ipc=%s\n", name, ipc ? ipc : "");
        g_free(url);
        g_free(ipc);
    }
    native_request_clear(&request);
}

int main(int argc, char **argv) {
    origin = g_strdup("http://127.0.0.1:4567");
    token = g_strdup("token-1234567890");
    if (argc > 1 && strcmp(argv[1], "api") == 0) {
        server_port = (guint16)strtoul(argv[2], NULL, 10);
        char *play[] = {"openbox", "--play", "game/id", NULL};
        NativeRequest request = {NATIVE_REQUEST_NONE, NULL};
        NativeArgsResult result = native_request_from_argv(3, play, &request);
        printf("api_ok=%d\n", result == NATIVE_ARGS_REQUEST &&
               dispatch_native_api_request(&request));
        native_request_clear(&request);
    } else {
        char *play[] = {"openbox", "--play", "game/id", NULL};
        char *search[] = {"openbox", "--uri", "openbox://search/a;echo pwned", NULL};
        char *bad[] = {"openbox", "--uri", "https://evil.example/launch/1", NULL};
        emit_request("play", 3, play);
        emit_request("search", 3, search);
        emit_request("bad", 3, bad);
    }
    g_free(origin);
    g_free(token);
    return 0;
}
'''.replace("@@NATIVE_SOURCE@@", str(root_dir / "native_host.c")),
            encoding="utf-8",
        )
        cflags = subprocess.check_output(
            ['pkg-config', '--cflags', 'webkit2gtk-4.1', 'gtk+-3.0'],
            text=True,
        ).strip()
        libs = subprocess.check_output(
            ['pkg-config', '--libs', 'webkit2gtk-4.1', 'gtk+-3.0'],
            text=True,
        ).strip()
        binary = Path(directory) / "native_argument_harness"
        result = subprocess.run(
            ['gcc', '-Wall', '-Wextra'] + cflags.split() +
            ['-o', str(binary), str(harness)] + libs.split(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"Harness compilation failed:\n{result.stderr}")
        return binary

    def test_compiles(self):
        """native_host.c compiles without errors."""
        result = subprocess.run(
            ['pkg-config', '--exists', 'webkit2gtk-4.1'],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest('webkit2gtk-4.1 dev headers not available')
        
        cflags = subprocess.check_output(
            ['pkg-config', '--cflags', 'webkit2gtk-4.1', 'gtk+-3.0'],
            text=True
        ).strip()
        libs = subprocess.check_output(
            ['pkg-config', '--libs', 'webkit2gtk-4.1', 'gtk+-3.0'],
            text=True
        ).strip()
        
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        # Compile
        compile_cmd = ['gcc', '-Wall', '-Wextra'] + cflags.split() + ['-o', '/tmp/test_native_host', 'native_host.c'] + libs.split()
        result = subprocess.run(
            compile_cmd,
            capture_output=True, text=True,
            cwd=root_dir,
            check=False
        )
        self.assertEqual(result.returncode, 0, f'Compilation failed:\n{result.stderr}')
    def test_boot_wait_uses_kernel_notification(self):
        """Boot wait should not add a fixed 100ms delay after every check."""
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root_dir, 'native_host.c'), encoding='utf-8') as handle:
            source = handle.read()
        parent_wait = source[source.index('wait_for_boot_files'):source.index('static gboolean\nboot_server')]
        self.assertIn('inotify_init1', parent_wait)
        self.assertIn('poll(&pfd', parent_wait)
        self.assertIn('waitpid(server_pid', parent_wait)
        self.assertNotIn('100 * 1000', parent_wait)

    def test_single_instance_dispatches_forwarded_requests(self):
        """A running owner receives deeplinks, not only the legacy focus ping."""
        root_dir = Path(__file__).resolve().parent.parent
        source = (root_dir / 'native_host.c').read_text(encoding='utf-8')
        self.assertIn('send_single_instance_request(sock_fd, request)', source)
        self.assertIn('g_str_has_prefix(message, "deeplink ")', source)
        self.assertIn('dispatch_native_request(&request)', source)
        self.assertNotIn('write(sock_fd, "focus\\n"', source)


    def test_socket_behavior(self):
        """Test the stale socket logic by simulating it."""
        result = subprocess.run(
            ['pkg-config', '--exists', 'webkit2gtk-4.1'],
            capture_output=True,
            check=False
        )
        if result.returncode != 0:
            self.skipTest('webkit2gtk-4.1 dev headers not available')

        # Run the compiled binary briefly to test socket creation
        with tempfile.TemporaryDirectory() as d:
            env = os.environ.copy()
            env["OPENBOX_DATA_DIR"] = d
            fake_app = os.path.join(d, "fake_web_app.py")
            with open(fake_app, "w", encoding="utf-8") as handle:
                handle.write("import time\\ntime.sleep(10)\\n")
            env["OPENBOX_WEB_APP"] = fake_app
            
            sock_path = os.path.join(d, "openbox.sock")
            # Create a stale socket manually
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(sock_path)
            sock.close()
            
            # The instance should detect stale socket, unlink it, and bind its own.
            p1 = subprocess.Popen(['/tmp/test_native_host'], env=env, stderr=subprocess.PIPE)
            
            deadline = time.time() + 3
            connected = False
            while time.time() < deadline and not connected:
                test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    test_sock.connect(sock_path)
                    test_sock.sendall(b"focus\n")
                    connected = True
                except Exception:
                    time.sleep(0.05)
                finally:
                    test_sock.close()
                if p1.poll() is not None:
                    break

            stderr = p1.stderr.read().decode(errors="replace") if p1.poll() is not None else ""
            self.assertTrue(connected, f"Failed to connect to the new socket: {stderr}")

            p1.terminate()
            p1.wait()
            if p1.stderr:
                p1.stderr.close()

    def test_native_args_build_authenticated_deeplink_and_safe_ipc(self):
        """Validated argv becomes the same authenticated UI/IPC request."""
        result = subprocess.run(
            ['pkg-config', '--exists', 'webkit2gtk-4.1'],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest('webkit2gtk-4.1 dev headers not available')

        with tempfile.TemporaryDirectory() as directory:
            binary = self._compile_argument_harness(directory)
            output = subprocess.check_output([str(binary)], text=True)

        values = dict(line.split('=', 1) for line in output.splitlines())
        self.assertEqual(values['play_result'], '1')
        self.assertEqual(
            values['play_url'],
            'http://127.0.0.1:4567/?token=token-1234567890&deeplink=showgame&id=game%2Fid',
        )
        self.assertEqual(values['play_ipc'], 'openbox://launch/game%2Fid')
        self.assertEqual(values['search_result'], '1')
        self.assertIn('deeplink=search&q=a%3Becho%20pwned', values['search_url'])
        self.assertEqual(values['search_ipc'], 'openbox://search/a%3Becho%20pwned')
        self.assertEqual(values['bad_result'], '2')

    def test_launch_request_posts_authenticated_api_payload(self):
        """The launch request uses parity_deeplinks' authenticated API path."""
        result = subprocess.run(
            ['pkg-config', '--exists', 'webkit2gtk-4.1'],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            self.skipTest('webkit2gtk-4.1 dev headers not available')

        captured = []

        class CaptureHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = int(self.headers['Content-Length'])
                captured.append((self.path, self.headers['X-OpenBox-Token'], self.rfile.read(length)))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

        server = ThreadingHTTPServer(('127.0.0.1', 0), CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                binary = self._compile_argument_harness(directory)
                result = subprocess.run(
                    [str(binary), 'api', str(server.server_address[1])],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
            self.assertIn('api_ok=1', result.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], '/api/launch')
        self.assertEqual(captured[0][1], 'token-1234567890')
        self.assertEqual(captured[0][2], b'{"game_id":"game/id"}')

    def test_native_launcher_forwards_argv_verbatim(self):
        """The shell boundary preserves argv without evaluating its values."""
        root_dir = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            marker = directory / 'argv.txt'
            fake_host = directory / 'native-host'
            fake_host.write_text(
                '#!/bin/sh\n'
                ': > "$OPENBOX_ARGV_MARKER"\n'
                'for arg do printf \'%s\\n\' "$arg" >> "$OPENBOX_ARGV_MARKER"; done\n',
                encoding='utf-8',
            )
            fake_host.chmod(0o755)
            env = os.environ.copy()
            env['OPENBOX_NATIVE_HOST'] = str(fake_host)
            env['OPENBOX_ARGV_MARKER'] = str(marker)
            subprocess.run(
                [
                    str(root_dir / 'openbox-native.sh'),
                    '--play', 'game;$(touch should-not-exist)',
                    '--uri', 'openbox://search/a b',
                ],
                env=env,
                check=True,
                timeout=5,
            )
            self.assertEqual(
                marker.read_text(encoding='utf-8').splitlines(),
                ['--play', 'game;$(touch should-not-exist)', '--uri', 'openbox://search/a b'],
            )
            self.assertFalse((directory / 'should-not-exist').exists())

if __name__ == "__main__":
    unittest.main()
