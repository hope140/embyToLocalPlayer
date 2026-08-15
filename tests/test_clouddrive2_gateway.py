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
        self.client_ip = '127.0.0.1'
        self.wfile = io.BytesIO()
        self.responses = []
        self.headers_sent = []
        self.ended = False

    def _client_ip(self):
        return self.client_ip

    def _header(self, name, default=None):
        return self.headers.get(name, default)

    def _cd2_client_key(self):
        return f"{self.client_ip}\n{self._header('User-Agent', '').strip().casefold()}"

    def _is_allowed_media_extension(self, path):
        return http_server.UserScriptRequestHandler._is_allowed_media_extension(path)

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

    def test_configured_refresh_parent_levels_and_invalid_default(self):
        for configured, expected in (('4', 4), ('not-a-number', 3), (None, 3)):
            with self.subTest(configured=configured):
                raw = ConfigParser()
                raw.read_dict({'clouddrive2': {
                    'enable': 'yes',
                    'api_token': 'secret',
                    'origin': 'http://127.0.0.1:19798',
                    'path_map': r'C:\Media=/115open/115',
                }})
                if configured is not None:
                    raw.set('clouddrive2', 'refresh_parent_levels', configured)
                with mock.patch.object(gateway_module.configs, 'raw', raw), \
                        mock.patch.object(gateway_module, 'CloudDrive2Client') as client_type:
                    gateway = gateway_module.CloudDrive2Gateway()
                    self.assertIsNotNone(gateway._client_from_config())

                self.assertEqual(
                    client_type.call_args.kwargs['refresh_parent_levels'],
                    expected,
                )

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

    def test_lookup_allows_user_agent_changes_for_same_loopback_ip(self):
        now = [100.0]
        gateway = gateway_module.CloudDrive2Gateway(
            clock=lambda: now[0], token_urlsafe=lambda _: 'nonce-one', ttl_seconds=60)
        gateway.configure('http://127.0.0.1:58000')
        gateway.register(r'C:\Media\movie.mkv')

        first = gateway.lookup_or_claim('nonce-one', '127.0.0.1\nmpv')
        self.assertEqual(first.first_client_key, '127.0.0.1\nmpv')
        self.assertEqual(
            gateway.lookup_or_claim('nonce-one', '127.0.0.1\nmpv').local_path,
            r'C:\Media\movie.mkv')
        self.assertEqual(
            gateway.lookup_or_claim('nonce-one', '127.0.0.1\nother').local_path,
            r'C:\Media\movie.mkv')
        self.assertIsNone(
            gateway.lookup_or_claim('nonce-one', '127.0.0.2\nother'))

        ipv6_gateway = gateway_module.CloudDrive2Gateway(
            clock=lambda: now[0], token_urlsafe=lambda _: 'nonce-ipv6', ttl_seconds=60)
        ipv6_gateway.configure('http://[::1]:58000')
        ipv6_gateway.register(r'C:\Media\movie.mkv')
        self.assertIsNotNone(ipv6_gateway.lookup_or_claim('nonce-ipv6', '::1\nmpv'))
        self.assertIsNotNone(ipv6_gateway.lookup_or_claim('nonce-ipv6', '::1\nother'))

        now[0] = 161.0
        self.assertIsNone(gateway.lookup_or_claim('nonce-one', '127.0.0.1\nmpv'))

    def test_lookup_keeps_ip_and_user_agent_binding_for_non_loopback_clients(self):
        gateway = gateway_module.CloudDrive2Gateway(token_urlsafe=lambda _: 'nonce-lan')
        gateway.configure('http://127.0.0.1:58000')
        gateway.register(r'C:\Media\movie.mkv')

        self.assertIsNotNone(
            gateway.lookup_or_claim('nonce-lan', '192.168.1.10\nmpv'))
        self.assertIsNone(
            gateway.lookup_or_claim('nonce-lan', '192.168.1.10\nother'))
        self.assertIsNone(
            gateway.lookup_or_claim('nonce-lan', '192.168.1.11\nmpv'))

    def test_lookup_logs_redacted_missing_expired_and_client_mismatch(self):
        now = [100.0]
        gateway = gateway_module.CloudDrive2Gateway(
            clock=lambda: now[0], token_urlsafe=lambda _: 'nonce-expired', ttl_seconds=60)
        gateway.configure('http://127.0.0.1:58000')
        gateway.register(r'C:\secret\movie.mkv')
        with mock.patch.object(gateway_module.logger, 'info') as log:
            self.assertIsNone(gateway.lookup_or_claim(
                'missing-secret-nonce', '192.168.1.10\nsecret-user-agent'))

            now[0] = 160.0
            self.assertIsNone(gateway.lookup_or_claim(
                'nonce-expired', '192.168.1.10\nsecret-user-agent'))

            now[0] = 200.0
            gateway.register(r'C:\secret\movie.mkv')
            self.assertIsNotNone(gateway.lookup_or_claim(
                'nonce-expired', '192.168.1.10\nsecret-user-agent'))
            self.assertIsNone(gateway.lookup_or_claim(
                'nonce-expired', '192.168.1.10\nanother-secret-user-agent'))

        messages = '\n'.join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn('cd2 gateway lookup status=missing', messages)
        self.assertIn('cd2 gateway lookup status=expired', messages)
        self.assertIn('cd2 gateway lookup status=client_mismatch', messages)
        for secret in (
                'missing-secret-nonce', 'secret-user-agent',
                'another-secret-user-agent', r'C:\secret\movie.mkv'):
            self.assertNotIn(secret, messages)

    def test_default_ttl_is_shorter_than_legacy_day(self):
        gateway = gateway_module.CloudDrive2Gateway()
        self.assertEqual(gateway.ttl_seconds, 6 * 60 * 60)

    def test_maybe_register_requires_configured_client_mapping(self):
        gateway = gateway_module.CloudDrive2Gateway(token_urlsafe=lambda _: 'nonce')
        gateway.configure('http://localhost:1')
        mapped = mock.Mock()
        mapped.map_local_path_to_cloud_path.return_value = '/Movies/movie.mkv'
        gateway._client_from_config = mock.Mock(return_value=mapped)

        fallback_url = 'https://emby.example/videos/item-1/original.mkv?token=secret'
        self.assertEqual(
            gateway.maybe_register(r'C:\Media\movie.mkv', fallback_url=fallback_url),
            'http://localhost:1/cd2/nonce',
        )
        self.assertTrue(gateway._entries['nonce'].allow_local_fallback)
        self.assertEqual(gateway._entries['nonce'].fallback_url, fallback_url)
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
    def test_client_disconnect_error_is_limited_to_expected_socket_errors(self):
        is_disconnect = http_server.UserScriptRequestHandler._is_client_disconnect_error
        self.assertTrue(is_disconnect(ConnectionResetError('peer reset')))
        self.assertTrue(is_disconnect(ConnectionAbortedError('peer aborted')))
        self.assertTrue(is_disconnect(BrokenPipeError('pipe closed')))

        unrelated = OSError('unrelated I/O failure')
        unrelated.winerror = 5
        self.assertFalse(is_disconnect(unrelated))

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
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.mkv', allow_local_fallback=False)
        fake_gateway.resolve_entry.return_value = 'https://cd2.example/video.mkv'
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [307])
        self.assertIn(('Location', 'https://cd2.example/video.mkv'), handler.headers_sent)
        self.assertIn(('Cache-Control', 'no-store'), handler.headers_sent)
        fake_gateway.lookup_or_claim.assert_called_once_with(
            'opaque-nonce', '127.0.0.1\n')

    def test_send_cd2_file_falls_back_to_local_file_when_resolution_fails(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.mkv', allow_local_fallback=True)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway), \
                mock.patch.object(http_server.logger, 'info') as log, \
                mock.patch.object(http_server.os.path, 'isfile', return_value=True):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        handler._send_local_file.assert_called_once_with('movie.mkv')
        self.assertEqual(handler.responses, [])
        messages = '\n'.join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn('cd2 local media exists=yes', messages)

    def test_send_cd2_file_prefers_nonlocal_fallback_when_media_missing(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fallback_url = 'https://emby.example/videos/item-1/original.mkv?token=secret'
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.mkv', allow_local_fallback=True,
            fallback_url=fallback_url)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway), \
                mock.patch.object(http_server.logger, 'info') as log, \
                mock.patch.object(http_server.UserScriptRequestHandler,
                                  '_send_cd2_strm_fallback') as pointer:
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [307])
        self.assertIn(('Location', fallback_url), handler.headers_sent)
        handler._send_local_file.assert_not_called()
        pointer.assert_not_called()
        messages = '\n'.join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn('cd2 nonlocal fallback status=success', messages)
        self.assertNotIn(fallback_url, messages)

    def test_send_cd2_file_sibling_strm_fallback_when_media_missing(self):
        with tempfile.NamedTemporaryFile('w', suffix='.strm', delete=False,
                                         encoding='utf-8') as stream:
            stream.write('https://cdn.example.com/real-media?id=9\n')
            strm_path = stream.name
        media_path = os.path.splitext(strm_path)[0] + '.mkv'  # does not exist
        try:
            handler = _FakeHandler()
            handler.path = '/cd2/opaque-nonce'
            handler._send_local_file = mock.Mock()
            fake_gateway = mock.Mock()
            fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
                local_path=media_path, allow_local_fallback=True)
            fake_gateway.resolve_entry.return_value = None
            with mock.patch.object(http_server, 'gateway', fake_gateway), \
                    mock.patch.object(http_server.logger, 'info') as log:
                http_server.UserScriptRequestHandler.send_cd2_file(handler)
        finally:
            os.unlink(strm_path)

        self.assertEqual(handler.responses, [307])
        self.assertIn(('Location', 'https://cdn.example.com/real-media?id=9'),
                      handler.headers_sent)
        handler._send_local_file.assert_not_called()
        messages = '\n'.join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn('cd2 gateway entry kind=media', messages)
        self.assertIn('cd2 local media exists=no', messages)
        self.assertIn('cd2 pointer fallback source=sibling status=success', messages)

    def test_send_cd2_file_does_not_fallback_for_direct_register_entry(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.mkv', allow_local_fallback=False)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [404])
        handler._send_local_file.assert_not_called()

    def test_send_cd2_file_rejects_non_media_extension_on_mapped_fallback(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.exe', allow_local_fallback=True)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [404])
        handler._send_local_file.assert_not_called()

    def test_send_cd2_file_strm_fallback_redirects_to_inner_url(self):
        with tempfile.NamedTemporaryFile('w', suffix='.strm', delete=False,
                                         encoding='utf-8') as stream:
            stream.write('https://cdn.example.com/video?id=123\n')
            strm_path = stream.name
        try:
            handler = _FakeHandler()
            handler.path = '/cd2/opaque-nonce'
            handler._send_local_file = mock.Mock()
            fake_gateway = mock.Mock()
            fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
                local_path=strm_path, allow_local_fallback=True)
            fake_gateway.resolve_entry.return_value = 'https://cd2.example/video.mkv'
            with mock.patch.object(http_server, 'gateway', fake_gateway), \
                    mock.patch.object(http_server.logger, 'info') as log:
                http_server.UserScriptRequestHandler.send_cd2_file(handler)
        finally:
            os.unlink(strm_path)

        self.assertEqual(handler.responses, [307])
        self.assertIn(('Location', 'https://cdn.example.com/video?id=123'),
                      handler.headers_sent)
        self.assertIn(('Cache-Control', 'no-store'), handler.headers_sent)
        # A .strm pointer is never resolved through CD2: its cloud counterpart
        # would be the same pointer text, not the media.
        fake_gateway.resolve_entry.assert_not_called()
        messages = '\n'.join(str(call.args[0]) for call in log.call_args_list)
        self.assertIn('cd2 gateway entry kind=strm', messages)
        self.assertIn('cd2 pointer fallback source=direct status=success', messages)
        self.assertNotIn('https://', messages)

    def test_send_cd2_file_strm_fallback_404_when_content_not_usable(self):
        for content in ('not a url\n', '', '   \n', 'file:///C:/Windows/win.ini\n',
                        'rtsp://host/video\n', 'http://user:pass@example.com/video.mkv\n',
                        'http://' + 'a' * 9000 + '/video.mkv\n'):
            with self.subTest(content=content):
                with tempfile.NamedTemporaryFile('w', suffix='.strm', delete=False,
                                                 encoding='utf-8') as stream:
                    stream.write(content)
                    strm_path = stream.name
                try:
                    handler = _FakeHandler()
                    handler.path = '/cd2/opaque-nonce'
                    handler._send_local_file = mock.Mock()
                    fake_gateway = mock.Mock()
                    fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
                        local_path=strm_path, allow_local_fallback=True)
                    fake_gateway.resolve_entry.return_value = None
                    with mock.patch.object(http_server, 'gateway', fake_gateway):
                        http_server.UserScriptRequestHandler.send_cd2_file(handler)
                finally:
                    os.unlink(strm_path)

                self.assertEqual(handler.responses, [404])
                handler._send_local_file.assert_not_called()

    def test_send_cd2_file_strm_fallback_requires_mapped_entry(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path='movie.strm', allow_local_fallback=False)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [404])
        handler._send_local_file.assert_not_called()
        fake_gateway.resolve_entry.assert_not_called()

    def test_send_cd2_file_strm_fallback_404_when_pointer_unreadable(self):
        handler = _FakeHandler()
        handler.path = '/cd2/opaque-nonce'
        handler._send_local_file = mock.Mock()
        fake_gateway = mock.Mock()
        fake_gateway.lookup_or_claim.return_value = SimpleNamespace(
            local_path=r'Z:\missing\movie.strm', allow_local_fallback=True)
        fake_gateway.resolve_entry.return_value = None
        with mock.patch.object(http_server, 'gateway', fake_gateway):
            http_server.UserScriptRequestHandler.send_cd2_file(handler)

        self.assertEqual(handler.responses, [404])
        handler._send_local_file.assert_not_called()


class StrmContentParseTests(unittest.TestCase):
    """Unit coverage for the .strm pointer parsing helper."""

    parse = staticmethod(http_server.UserScriptRequestHandler._parse_strm_url)

    def test_accepts_http_and_https_first_line(self):
        self.assertEqual(
            self.parse(b'https://cdn.example/video.mkv\n'), 'https://cdn.example/video.mkv')
        self.assertEqual(
            self.parse('http://a.example/x?id=1#frag\n'), 'http://a.example/x?id=1#frag')
        self.assertEqual(
            self.parse('\ufeffhttps://a.example/x\n'), 'https://a.example/x')
        self.assertEqual(
            self.parse(' \nhttps://a.example/x\n'), 'https://a.example/x')

    def test_rejects_non_http_and_embedded_credentials(self):
        for content in (
                '', '   ', 'not a url', 'file:///C:/x.mkv', 'rtsp://host/x',
                '//host/path', 'http://', 'https://',
                'http://user:pass@host/x',
                'http://[::1',
                'http://' + 'a' * 9000, 'line1\nline2'):
            with self.subTest(content=content):
                self.assertIsNone(self.parse(content))

    def test_rejects_non_text_content(self):
        self.assertIsNone(self.parse(b'\xff\xfe\x00garbage'))
        self.assertIsNone(self.parse(123))

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
        register.assert_called_once_with(
            r'C:\Media\Movie.mkv', fallback_url=result['stream_url'])

    def test_emby_parser_does_not_register_undetermined_strm_path(self):
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
                mock.patch.object(data_parser, 'strm_local_media_path',
                                  return_value=r'C:\Media\Movie.strm'), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url') as register:
            result = data_parser.parse_received_data_emby(received_data)

        self.assertEqual(result['media_path'], result['stream_url'])
        self.assertFalse(result['mount_disk_mode'])
        self.assertFalse(result['use_strm_local_path'])
        self.assertFalse(result['use_strm_cd2_url'])
        self.assertIsNone(result['strm_cd2_local_path'])
        register.assert_not_called()

    def test_list_episodes_uses_item_mount_mode_for_strm_fallback(self):
        data = {
            'server': 'emby',
            'scheme': 'http',
            'netloc': 'emby.example',
            'api_key': 'token',
            'user_id': 'user',
            'mount_disk_mode': True,
            'headers': {},
            'playlist_info': [{'Id': 'ep-1'}],
            'main_ep_info': {'SeasonId': 'season-1', 'SeriesId': 'series-1'},
            'server_version': '4.8.0.40',
            'basename': 'Movie.strm',
            'is_strm': True,
            'is_http_source': True,
            'strm_direct': False,
            'is_http_direct_strm': False,
            'strm_local_by_file_path': True,
            'item_id': 'item-1',
            'media_source_id': 'source-1',
            'file_path': r'C:\Media\Movie.strm',
            'media_title': 'Movie',
            'episodes_info': [],
            'sub_inner_idx': 0,
            'device_id': 'device',
            'play_session_id': 'session',
        }
        episode = {
            'Id': 'ep-1',
            'Type': 'Episode',
            'ProviderIds': {},
            'SeriesId': 'series-1',
            'Path': r'C:\Media\Movie.strm',
            'ParentIndexNumber': 1,
            'IndexNumber': 1,
            'Name': 'Movie',
            'RunTimeTicks': 10 ** 7,
            'MediaSources': [{
                'Id': 'source-ep-1',
                'Path': 'https://media.example/Movie.mkv',
                'Container': 'strm',
                'MediaStreams': [],
                'RunTimeTicks': 10 ** 7,
                'Size': 10,
            }],
        }

        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [episode]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'strm_local_media_path',
                                  return_value=r'C:\Media\Movie.strm'), \
                mock.patch.object(data_parser, 'translate_path_by_ini',
                                  side_effect=lambda value: value), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url') as register, \
                mock.patch.object(data_parser.configs, 'check_str_match',
                                  return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate',
                                  return_value={}):
            result = data_parser.list_episodes(data)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['media_path'], result[0]['stream_url'])
        self.assertFalse(result[0]['mount_disk_mode'])
        self.assertFalse(result[0]['use_strm_local_path'])
        self.assertFalse(result[0]['use_strm_cd2_url'])
        register.assert_not_called()


if __name__ == '__main__':
    unittest.main()
