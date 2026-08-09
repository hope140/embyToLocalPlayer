import json
import threading
import unittest
import urllib.error
import urllib.request
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
        request = net_tools.requests_urllib(
            'http://127.0.0.1:58000/gui', _json={}, req_only=True)
        self.assertEqual(request.get_header('X-etlp-protocol'), '1')

    def test_open_local_folder_uses_argument_vector(self):
        raw_platform = tools.configs.platform
        try:
            tools.configs.platform = 'Windows'
            with mock.patch.object(tools, 'translate_path_by_ini', return_value=r'C:\Media\evil & file.mkv'), \
                    mock.patch.object(tools.subprocess, 'Popen') as popen:
                tools.open_local_folder({'full_path': r'C:\Media\evil & file.mkv'})
            popen.assert_called_once_with(['explorer.exe', '/select,', r'C:\Media\evil & file.mkv'])
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

    def test_options_does_not_enable_cors(self):
        request = urllib.request.Request(self.base_url + '/openFolder', method='OPTIONS')
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(context.exception.code, 403)
        self.assertNotIn('Access-Control-Allow-Origin', context.exception.headers)


if __name__ == '__main__':
    unittest.main()
