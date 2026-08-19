import os
import socket
import subprocess
import tempfile
import time
import unittest

class TestNativeHost(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
