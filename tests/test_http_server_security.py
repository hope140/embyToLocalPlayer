import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from configparser import ConfigParser
from unittest import mock

import utils.http_server as http_server
import utils.net_tools as net_tools
import utils.tools as tools
from utils.http_security import media_signature, media_url_signature_valid


class HttpSecurityUnitTests(unittest.TestCase):
    def test_media_signature_window_and_tamper_checks(self):
        token = 't' * 32
        path = r'C:\Media\movie.mkv'
        expires = '1100'
        signature = media_signature(token, path, expires)
        self.assertTrue(media_url_signature_valid(token, path, expires, signature, now=1000))
        self.assertFalse(media_url_signature_valid('', path, expires, signature, now=1000))
        self.assertFalse(media_url_signature_valid(token, path + 'x', expires, signature, now=1000))
        self.assertFalse(media_url_signature_valid(token, path, '900', signature, now=1000))
        self.assertFalse(media_url_signature_valid(token, path, '1201', signature, now=1000))

    def test_local_request_wrapper_adds_protocol_header(self):
        for host in ('127.0.0.1', 'localhost', '[::1]'):
            with self.subTest(host=host):
                request = net_tools.requests_urllib(
                    f'http://{host}:58000/gui', _json={}, req_only=True)
                self.assertEqual(request.get_header('X-etlp-protocol'), '1')

    def test_non_loopback_bind_requires_strong_token_even_when_configured_local(self):
        for token in ('', 'short'):
            with self.subTest(token=token):
                raw = ConfigParser()
                raw.read_dict({'dev': {'listen_on_localhost': 'yes', 'http_server_token': token}})
                with mock.patch.object(http_server.configs, 'raw', raw), \
                        mock.patch.object(http_server, 'ThreadingHTTPServer') as server_cls:
                    with self.assertRaises(ValueError):
                        http_server.run_server(ip='0.0.0.0', port=0)
                server_cls.assert_not_called()

    def test_explicit_localhost_bind_does_not_require_token(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'listen_on_localhost': 'yes', 'http_server_token': ''}})
        with mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server, 'ThreadingHTTPServer') as server_cls, \
                mock.patch.object(http_server, 'configure_gateway'):
            server_cls.return_value.server_address = ('localhost', 0)
            http_server.run_server(ip='localhost', port=0)
        server_cls.assert_called_once()
        server_cls.return_value.serve_forever.assert_called_once()

    def test_open_local_folder_uses_argument_vector(self):
        raw_platform = tools.configs.platform
        try:
            tools.configs.platform = 'Windows'
            with mock.patch.object(tools, 'translate_path_by_ini', return_value=r'C:\Media\evil & file.mkv'), \
                    mock.patch.object(tools.subprocess, 'Popen') as popen, \
                    mock.patch.object(tools, '_logger') as logger:
                tools.open_local_folder({'full_path': r'C:\Media\evil & file.mkv'})
            popen.assert_called_once_with(['explorer.exe', '/select,', r'C:\Media\evil & file.mkv'])
            logger.info.assert_called_once_with('open local folder')
        finally:
            tools.configs.platform = raw_platform


class HttpServerRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http_server.ThreadingHTTPServer(('127.0.0.1', 0), http_server.UserScriptRequestHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, body, *, headers=None):
        encoded = json.dumps(body).encode('utf-8')
        request = urllib.request.Request(
            self.base_url + path, data=encoded, method='POST',
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_old_post_without_protocol_is_rejected(self):
        status, _ = self._post('/openFolder', {'full_path': r'C:\Media\movie.mkv'})
        self.assertEqual(status, 403)

    def test_new_post_is_accepted_and_responds_once(self):
        with mock.patch.object(http_server, 'open_local_folder') as open_folder:
            status, body = self._post(
                '/openFolder/', {'full_path': r'C:\Media\movie.mkv'},
                headers={'X-ETLP-Protocol': '1'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'msg': 'default'})
        open_folder.assert_called_once()

    def test_content_type_and_exact_route_validation(self):
        encoded = b'{}'
        request = urllib.request.Request(
            self.base_url + '/openFolder', data=encoded, method='POST',
            headers={'Content-Type': 'text/plain', 'X-ETLP-Protocol': '1'})
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 400)

        status, _ = self._post('/openFolder.evil', {}, headers={'X-ETLP-Protocol': '1'})
        self.assertEqual(status, 404)

    def _post_sparse(self, payload):
        cache_dir = tempfile.mkdtemp()
        raw = ConfigParser()
        raw.read_dict({'dev': {'listen_on_localhost': 'yes'},
                       'gui': {'server_cache_path': cache_dir}})
        try:
            with mock.patch.object(http_server.configs, 'raw', raw), \
                    mock.patch.object(http_server.configs, 'update'), \
                    mock.patch.object(http_server, 'create_sparse_file') as create:
                status, body = self._post(
                    '/action/sparse_file', payload,
                    headers={'X-ETLP-Protocol': '1'})
                return status, body, create.call_args
        finally:
            os.rmdir(cache_dir)

    def test_sparse_file_rejects_traversal_and_invalid_size(self):
        for payload in (
                {'name': '../escape.bin', 'size': 1},
                {'name': r'..\escape.bin', 'size': 1},
                {'name': '/absolute/escape.bin', 'size': 1},
                {'name': 'valid.bin', 'size': -1},
                {'name': 'valid.bin', 'size': True},
        ):
            with self.subTest(payload=payload):
                status, _, create_args = self._post_sparse(payload)
                self.assertEqual(status, 400)
                self.assertIsNone(create_args)

    def test_sparse_file_accepts_plain_name_and_positive_size(self):
        status, body, create_args = self._post_sparse({'name': 'valid.bin', 'size': 8})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'sparse_file': True})
        target, size = create_args.args
        self.assertEqual(size, 8)
        self.assertEqual(os.path.basename(target), 'valid.bin')

    def test_media_extension_requires_dot_suffix(self):
        allowed = http_server.UserScriptRequestHandler._is_allowed_media_extension
        self.assertTrue(allowed('movie.mp4'))
        self.assertFalse(allowed('notmp4'))
        self.assertFalse(allowed('movie.mp4.bak'))

    def test_options_does_not_enable_cors(self):
        request = urllib.request.Request(self.base_url + '/openFolder', method='OPTIONS')
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', context.exception.headers)


if __name__ == '__main__':
    unittest.main()
