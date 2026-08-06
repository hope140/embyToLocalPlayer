import io
import os
import tempfile
import unittest
from configparser import ConfigParser
from types import SimpleNamespace
from unittest import mock

import utils.clouddrive2_gateway as gateway_module
import utils.data_parser as data_parser
import utils.http_server as http_server


class _FakeHandler:
    def __init__(self, *, range_header=None, command='GET'):
        self.command = command
        self.parse_range_header = http_server.UserScriptRequestHandler.parse_range_header
        self.headers = {} if range_header is None else {'Range': range_header}
        self.wfile = io.BytesIO()
        self.responses = []
        self.headers_sent = []
        self.ended = False

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, name, value):
        self.headers_sent.append((name, value))

    def end_headers(self):
        self.ended = True


class CloudDrive2GatewayTests(unittest.TestCase):
    def test_config_requires_explicit_path_map(self):
        raw = ConfigParser()
        raw.read_dict({'clouddrive2': {
            'enable': 'yes',
            'api_token': 'secret',
            'origin': 'http://127.0.0.1:19798',
        }})
        with mock.patch.object(gateway_module.configs, 'raw', raw):
            gateway = gateway_module.CloudDrive2Gateway()
            self.assertIsNone(gateway._client_from_config())

    def test_register_pop_and_expiry(self):
        now = [100.0]
        tokens = iter(('first nonce', 'second nonce'))
        gateway = gateway_module.CloudDrive2Gateway(
            clock=lambda: now[0], token_urlsafe=lambda _: next(tokens),
            ttl_seconds=60,
        )
        gateway.configure('http://127.0.0.1:58000/')

        url = gateway.register(r'C:\Media\movie.mkv')
        self.assertEqual(url, 'http://127.0.0.1:58000/cd2/first%20nonce')
        entry = gateway.pop_entry('first nonce')
        self.assertEqual(entry.local_path, r'C:\Media\movie.mkv')

        now[0] = 160.0
        self.assertIsNone(gateway.pop_entry('first nonce'))

    def test_maybe_register_requires_configured_client_mapping(self):
        gateway = gateway_module.CloudDrive2Gateway(token_urlsafe=lambda _: 'nonce')
        gateway.configure('http://localhost:1')
        mapped = mock.Mock()
        mapped.map_local_path_to_cloud_path.return_value = '/Movies/movie.mkv'
        gateway._client_from_config = mock.Mock(return_value=mapped)

        self.assertEqual(
            gateway.maybe_register(r'C:\Media\movie.mkv'),
            'http://localhost:1/cd2/nonce',
        )
        mapped.map_local_path_to_cloud_path.assert_called_once_with(
            r'C:\Media\movie.mkv')

        mapped.map_local_path_to_cloud_path.return_value = None
        self.assertIsNone(gateway.maybe_register(r'C:\Media\missing.mkv'))

        mapped._path_map = []
        mapped.map_local_path_to_cloud_path.return_value = '/Media/unmapped.mkv'
        self.assertIsNone(gateway.maybe_register(r'C:\Media\unmapped.mkv'))

    def test_max_entries_prunes_oldest(self):
        now = [0.0]
        tokens = iter(('one', 'two'))
        gateway = gateway_module.CloudDrive2Gateway(
            clock=lambda: now[0], token_urlsafe=lambda _: next(tokens),
            max_entries=1, ttl_seconds=60,
        )
        gateway.configure('http://localhost:1')
        gateway.register('one.mkv')
        now[0] = 1.0
        gateway.register('two.mkv')
        self.assertIsNone(gateway.pop_entry('one'))
        self.assertEqual(gateway.pop_entry('two').local_path, 'two.mkv')


class HttpRangeTests(unittest.TestCase):
    def test_parse_range_supports_suffix_and_clips_end(self):
        parse = http_server.UserScriptRequestHandler.parse_range_header
        self.assertEqual(parse('bytes=-3', 10), (7, 9))
        self.assertEqual(parse('bytes=-99', 10), (0, 9))
        self.assertEqual(parse('bytes=2-99', 10), (2, 9))
        self.assertEqual(parse('bytes=2-', 10), (2, 9))

    def test_parse_range_rejects_malformed_and_unsatisfiable_ranges(self):
        parse = http_server.UserScriptRequestHandler.parse_range_header
        for value in ('bytes=', 'bytes=-0', 'bytes=8-2', 'bytes=10-',
                      'bytes=abc-def', 'bytes=1-2,3-4', 'items=1-2'):
            self.assertEqual(parse(value, 10), (None, None), value)
        self.assertEqual(parse('bytes=-1', 0), (None, None))

    def test_send_local_file_returns_suffix_payload(self):
        handler = _FakeHandler(range_header='bytes=-3')
        handler.parse_range_header = http_server.UserScriptRequestHandler.parse_range_header
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(b'0123456789')
            path = stream.name
        try:
            http_server.UserScriptRequestHandler._send_local_file(handler, path)
        finally:
            os.unlink(path)

        self.assertEqual(handler.responses, [206])
        self.assertEqual(handler.wfile.getvalue(), b'789')
        self.assertIn(('Content-Range', 'bytes 7-9/10'), handler.headers_sent)
        self.assertIn(('Content-Length', '3'), handler.headers_sent)

    def test_send_local_file_returns_416_for_invalid_range(self):
        handler = _FakeHandler(range_header='bytes=bogus')
        handler.parse_range_header = http_server.UserScriptRequestHandler.parse_range_header
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(b'0123456789')
            path = stream.name
        try:
            http_server.UserScriptRequestHandler._send_local_file(handler, path)
        finally:
            os.unlink(path)

        self.assertEqual(handler.responses, [416])
        self.assertIn(('Content-Range', 'bytes */10'), handler.headers_sent)
        self.assertEqual(handler.wfile.getvalue(), b'')



class HttpGatewayRouteTests(unittest.TestCase):
    def test_send_cd2_file_redirects_to_resolved_url(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        fake_gateway = mock.Mock()
        fake_gateway.pop_entry.return_value = SimpleNamespace(local_path='movie.mkv')
        fake_gateway.resolve_entry.return_value = 'https://cd2.example/video.mkv'
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [307])
        self.assertIn(('Location', 'https://cd2.example/video.mkv'), handler.headers_sent)
        self.assertIn(('Cache-Control', 'no-store'), handler.headers_sent)
        fake_gateway.pop_entry.assert_called_once_with('opaque-nonce')

    def test_send_cd2_file_falls_back_to_local_file_when_resolution_fails(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.pop_entry.return_value = SimpleNamespace(local_path='movie.mkv')
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        handler._send_local_file.assert_called_once_with('movie.mkv')
        self.assertEqual(handler.responses, [])

    def test_head_range_sets_headers_without_writing_body(self):
        handler = _FakeHandler(range_header='bytes=2-4', command='HEAD')
        handler.parse_range_header = http_server.UserScriptRequestHandler.parse_range_header
        with tempfile.NamedTemporaryFile(delete=False) as stream:
            stream.write(b'0123456789')
            path = stream.name
        try:
            http_server.UserScriptRequestHandler._send_local_file(handler, path)
        finally:
            os.unlink(path)

        self.assertEqual(handler.responses, [206])
        self.assertEqual(handler.wfile.getvalue(), b'')
        self.assertIn(('Content-Length', '3'), handler.headers_sent)

class DataParserGatewayTests(unittest.TestCase):
    def test_emby_parser_replaces_local_strm_path_with_gateway_url(self):
        received_data = {
            'extraData': {
                'mainEpInfo': {'Path': r'C:\Media\Movie.strm', 'Type': 'Movie'},
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.8.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-1/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [{
                    'Id': 'source',
                    'Path': 'https://media.example/Movie.mkv',
                    'Container': 'strm',
                    'MediaStreams': [],
                    'RunTimeTicks': 10 ** 7,
                    'Size': 10,
                }],
            },
            'mountDiskEnable': 'true',
        }

        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Movie'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'force_disk_mode_by_path', return_value=False), \
                mock.patch.object(data_parser, 'translate_path_by_ini', side_effect=lambda value: value), \
                mock.patch.object(data_parser, 'strm_local_media_path', return_value=r'C:\Media\Movie.mkv'), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url', return_value='http://127.0.0.1:58000/cd2/nonce') as register:
            result = data_parser.parse_received_data_emby(received_data)

        self.assertEqual(result['media_path'], 'http://127.0.0.1:58000/cd2/nonce')
        self.assertTrue(result['use_strm_cd2_url'])
        self.assertEqual(result['strm_cd2_local_path'], r'C:\Media\Movie.mkv')
        self.assertEqual(result['media_basename'], 'Movie.mkv')
        self.assertIn('Movie.mkv', result['media_title'])
        self.assertNotIn('nonce', result['media_title'])
        self.assertNotIn('.strm', result['media_title'])
        register.assert_called_once_with(r'C:\Media\Movie.mkv')


if __name__ == '__main__':
    unittest.main()
