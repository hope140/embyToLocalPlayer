import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from configparser import ConfigParser
from pathlib import Path
from unittest import mock

import utils.http_server as http_server
import utils.stop_instance as stop_instance
from utils.instance_lock import InstanceLock
from utils.update import _is_runtime_member


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'utils' / 'others' / 'embyToLocalPlayer_debug.bat'
SCRIPT = ROOT / 'utils' / 'stop_instance.py'


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class StopInstanceTests(unittest.TestCase):
    URL = 'http://127.0.0.1:58000/shutdown/'

    def _run(self, response=None, side_effect=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(stop_instance.urllib.request, 'urlopen',
                               return_value=response, side_effect=side_effect) as urlopen, \
                mock.patch.object(stop_instance, 'SHUTDOWN_URL', self.URL), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = stop_instance.main()
        return result, stdout.getvalue(), stderr.getvalue(), urlopen

    def test_success_posts_local_json_request_and_accepts_contract(self):
        response = _Response(200, b'{"shutdown": true}')
        result, stdout, stderr, urlopen = self._run(response=response)

        self.assertEqual(result, 0)
        self.assertIn('ETLP shutdown requested', stdout)
        self.assertEqual(stderr, '')
        self.assertTrue(response.closed)
        request, = urlopen.call_args.args
        self.assertEqual(request.full_url, self.URL)
        self.assertEqual(request.method, 'POST')
        self.assertEqual(json.loads(request.data.decode('utf-8')), {})
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(request.get_header('Accept'), 'application/json')
        self.assertEqual(request.get_header('X-etlp-protocol'), '1')
        self.assertEqual(urlopen.call_args.kwargs, {'timeout': 3})

    def test_connection_refused_is_nonzero_and_actionable(self):
        result, stdout, stderr, urlopen = self._run(
            side_effect=urllib.error.URLError(ConnectionRefusedError('refused')))

        self.assertEqual(result, 1)
        self.assertEqual(stdout, '')
        self.assertIn('not running', stderr)
        self.assertIn('58000', stderr)
        urlopen.assert_called_once()

    def test_http_403_and_409_are_nonzero_and_actionable(self):
        for status in (403, 409):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    self.URL, status, 'failure', {}, io.BytesIO(b'{}'))
                result, stdout, stderr, _ = self._run(side_effect=error)

                self.assertEqual(result, 1)
                self.assertEqual(stdout, '')
                self.assertIn(f'HTTP {status}', stderr)
                self.assertIn('port', stderr)

    def test_unexpected_status_and_json_are_nonzero(self):
        for response in (
                _Response(204, b''),
                _Response(200, b'{"shutdown": false}'),
                _Response(200, b'{"msg": "default"}'),
                _Response(200, b'not-json')):
            with self.subTest(status=response.status, body=response.body):
                result, stdout, stderr, _ = self._run(response=response)

                self.assertEqual(result, 1)
                self.assertEqual(stdout, '')
                if response.status == 200:
                    self.assertIn('unexpected response', stderr)
                else:
                    self.assertIn(f'HTTP {response.status}', stderr)
                self.assertIn('port 58000', stderr)

    def test_script_is_runtime_member_without_process_control(self):
        self.assertTrue(_is_runtime_member('utils/stop_instance.py'))
        self.assertFalse(_is_runtime_member('utils/others/stop_instance.py'))
        source = SCRIPT.read_text(encoding='utf-8')
        for forbidden in ('subprocess', 'psutil', 'os.kill', 'taskkill', 'TerminateProcess'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_launcher_adds_seventh_graceful_shutdown_choice(self):
        launcher = LAUNCHER.read_text(encoding='utf-8-sig')
        self.assertIn('choice /N /C:1234567', launcher)
        self.assertIn('7: close ETLP gracefully', launcher)
        self.assertIn('if errorlevel 7 goto seven', launcher.lower())
        self.assertIn('"%pythonPath%" "%~dp0utils\\stop_instance.py"', launcher)
        for choice in range(1, 7):
            self.assertIn(f'if errorlevel {choice} goto', launcher.lower())

    def test_shutdown_returns_to_launcher_lock_lifecycle(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'listen_on_localhost': 'yes'}})
        real_server_class = http_server.ThreadingHTTPServer
        server_ready = threading.Event()
        created_servers = []

        def make_server(*args, **kwargs):
            server = real_server_class(*args, **kwargs)
            created_servers.append(server)
            server_ready.set()
            return server

        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'etlp.lock'
            owner = InstanceLock(lock_path)
            self.assertTrue(owner.acquire())
            server_thread = None
            server = None
            try:
                with mock.patch.object(http_server.configs, 'raw', raw), \
                        mock.patch.object(http_server.configs, 'update'), \
                        mock.patch.object(http_server, 'configure_gateway'), \
                        mock.patch.object(http_server, 'ThreadingHTTPServer',
                                           side_effect=make_server):
                    server_thread = threading.Thread(
                        target=http_server.run_server,
                        kwargs={'ip': '127.0.0.1', 'port': 0},
                        daemon=True)
                    server_thread.start()
                    self.assertTrue(server_ready.wait(timeout=2))
                    server = created_servers[0]
                    port = server.server_address[1]

                    deadline = time.monotonic() + 2
                    while True:
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                                break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail('HTTP server did not start listening')
                            time.sleep(0.01)

                    result = stop_instance.stop_instance(
                        url=f'http://127.0.0.1:{port}/shutdown/', timeout=2)
                    self.assertEqual(result, 0)
                    server_thread.join(timeout=2)
                    self.assertFalse(server_thread.is_alive())
            finally:
                if server_thread is not None and server_thread.is_alive():
                    server.shutdown()
                    server_thread.join(timeout=2)
                if server is not None:
                    server.server_close()
                owner.release()

            replacement = InstanceLock(lock_path)
            try:
                self.assertTrue(replacement.acquire())
            finally:
                replacement.release()


if __name__ == '__main__':
    unittest.main()
