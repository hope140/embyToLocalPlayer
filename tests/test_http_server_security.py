import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from configparser import ConfigParser
from types import SimpleNamespace
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

    def test_cd2_non_loopback_auth_allows_same_host_player_but_gates_remote_client(self):
        raw = ConfigParser()
        token = 't' * 32
        raw.read_dict({'dev': {'http_server_token': token}})

        def route(client_ip, authorization=None):
            handler = object.__new__(http_server.UserScriptRequestHandler)
            handler.command = 'GET'
            handler.path = '/cd2/opaque'
            handler.client_address = (client_ip, 12345)
            handler.server = SimpleNamespace(server_address=('192.0.2.10', 58000))
            handler.headers = {}
            if authorization is not None:
                handler.headers['Authorization'] = authorization
            handler.send_cd2_file = mock.Mock()
            handler._send_error = mock.Mock()
            return handler

        with mock.patch.object(http_server.configs, 'raw', raw):
            local_player = route('192.0.2.10')
            http_server.UserScriptRequestHandler.do_GET(local_player)
            local_player.send_cd2_file.assert_called_once_with()
            local_player._send_error.assert_not_called()

            for authorization in (None, 'Bearer wrong'):
                with self.subTest(authorization=authorization):
                    remote = route('192.0.2.11', authorization)
                    http_server.UserScriptRequestHandler.do_GET(remote)
                    remote.send_cd2_file.assert_not_called()
                    remote._send_error.assert_called_once()

            remote = route('192.0.2.11', f'Bearer {token}')
            http_server.UserScriptRequestHandler.do_GET(remote)
            remote.send_cd2_file.assert_called_once_with()

    def test_cd2_head_uses_the_same_authorization_boundary(self):
        raw = ConfigParser()
        token = 't' * 32
        raw.read_dict({'dev': {'http_server_token': token}})
        handler = object.__new__(http_server.UserScriptRequestHandler)
        handler.command = 'HEAD'
        handler.path = '/cd2/opaque'
        handler.client_address = ('192.0.2.11', 12345)
        handler.server = SimpleNamespace(server_address=('192.0.2.10', 58000))
        handler.headers = {'Authorization': f'Bearer {token}'}
        handler.send_cd2_file = mock.Mock()
        handler._send_error = mock.Mock()
        with mock.patch.object(http_server.configs, 'raw', raw):
            http_server.UserScriptRequestHandler.do_HEAD(handler)
        handler.send_cd2_file.assert_called_once_with()
        handler._send_error.assert_not_called()

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

    def test_emby_and_plex_threads_use_parsed_payload(self):
        raw = ConfigParser()
        raw.read_dict({'gui': {'enable_path': '', 'without_confirm': 'no'}})
        original_payload = {'server': 'raw payload'}
        for route, parser_name in (
                ('/embyToLocalPlayer/', 'parse_received_data_emby'),
                ('/plexToLocalPlayer/', 'parse_received_data_plex')):
            with self.subTest(route=route):
                parsed_payload = {
                    'server': 'parsed server',
                    'server_version': '1',
                    'mount_disk_mode': False,
                    'netloc': 'media.example',
                    'file_path': r'C:\Media\movie.mp4',
                }
                handler = object.__new__(http_server.UserScriptRequestHandler)
                with mock.patch.object(http_server.configs, 'raw', raw), \
                        mock.patch.object(http_server.configs, 'gui_is_enable', False), \
                        mock.patch.object(http_server.configs, 'check_str_match', return_value=False), \
                        mock.patch.object(http_server, parser_name, return_value=parsed_payload), \
                        mock.patch.object(http_server.threading, 'Thread') as thread_cls:
                    handler._dispatch_post(route, original_payload)

                play_calls = [call for call in thread_cls.call_args_list
                              if call.kwargs.get('target') is http_server.start_play]
                self.assertEqual(len(play_calls), 1)
                self.assertIs(play_calls[0].kwargs['args'][0], parsed_payload)

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

    def _start_play_data(self):
        return {
            'file_path': 'movie.mkv',
            'media_path': 'movie.mkv',
            'start_sec': 0,
            'sub_file': None,
            'media_title': 'movie',
            'mount_disk_mode': False,
            'use_strm_cd2_url': False,
            'netloc': 'media.example',
        }

    def test_start_play_resets_player_is_running_when_get_player_cmd_fails(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'one_instance_mode': 'yes'}})
        with mock.patch.object(http_server, 'player_is_running', False), \
                mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server, 'ThreadWithReturnValue') as thread_cls, \
                mock.patch.object(http_server, 'get_player_cmd', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                http_server.start_play(self._start_play_data())
            self.assertFalse(http_server.player_is_running)
        thread_cls.return_value.start.assert_called_once_with()

    def test_start_play_resets_player_is_running_when_player_start_fails(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'one_instance_mode': 'yes'}})
        manager = mock.Mock()
        manager.start_player.side_effect = RuntimeError('start failed')
        with mock.patch.object(http_server, 'player_is_running', False), \
                mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server, 'ThreadWithReturnValue'), \
                mock.patch.object(http_server, 'get_player_cmd', return_value=['mpv.exe', 'movie.mkv']), \
                mock.patch.object(http_server, 'PlayerManager', return_value=manager), \
                mock.patch.object(http_server.configs, 'check_str_match', return_value=True):
            with self.assertRaises(RuntimeError):
                http_server.start_play(self._start_play_data())
            self.assertFalse(http_server.player_is_running)

    def test_start_play_resets_player_is_running_when_stop_sec_fails(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'one_instance_mode': 'yes'}})
        start_player = mock.Mock(return_value={'handle': object()})
        stop_sec = mock.Mock(side_effect=RuntimeError('stop failed'))
        with mock.patch.object(http_server, 'player_is_running', False), \
                mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server, 'ThreadWithReturnValue'), \
                mock.patch.object(http_server, 'get_player_cmd', return_value=['vlc.exe', 'movie.mkv']), \
                mock.patch.object(http_server, 'start_player_func_dict', {'vlc': start_player}), \
                mock.patch.object(http_server, 'stop_sec_func_dict', {'vlc': stop_sec}), \
                mock.patch.object(http_server.configs, 'check_str_match', return_value=False):
            with self.assertRaises(RuntimeError):
                http_server.start_play(self._start_play_data())
            self.assertFalse(http_server.player_is_running)

    def test_start_play_resets_player_is_running_when_playlist_join_fails(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'one_instance_mode': 'yes'}})
        manager = mock.Mock()
        thread = mock.Mock()
        thread.join.side_effect = RuntimeError('list failed')
        with mock.patch.object(http_server, 'player_is_running', False), \
                mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server, 'ThreadWithReturnValue', return_value=thread), \
                mock.patch.object(http_server, 'get_player_cmd', return_value=['mpv.exe', 'movie.mkv']), \
                mock.patch.object(http_server, 'PlayerManager', return_value=manager), \
                mock.patch.object(http_server.configs, 'check_str_match', return_value=True):
            with self.assertRaises(RuntimeError):
                http_server.start_play(self._start_play_data())
            self.assertFalse(http_server.player_is_running)

    def test_start_play_failed_realtime_stop_uses_full_fallback_for_short_unknown_watch(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'one_instance_mode': 'yes'}})
        thread = mock.Mock()
        thread.join.return_value = [{'file_path': 'movie.mkv'}]
        feedback_manager = mock.Mock()
        start_player = mock.Mock(return_value={'mpv': object()})
        stop_sec = mock.Mock(return_value=10)
        data = self._start_play_data()
        data.update({
            'server': 'emby',
            'scheme': 'https',
            'total_sec': 86400,
            '_playing_feedback_started': True,
        })
        with mock.patch.object(http_server, 'player_is_running', False), \
                mock.patch.object(http_server.configs, 'raw', raw), \
                mock.patch.object(http_server.configs, 'gui_is_enable', False), \
                mock.patch.object(http_server, 'ThreadWithReturnValue', return_value=thread), \
                mock.patch.object(http_server, 'get_player_cmd', return_value=['mpv.exe', 'movie.mkv']), \
                mock.patch.object(http_server, 'start_player_func_dict', {'mpv': start_player}), \
                mock.patch.object(http_server, 'stop_sec_func_dict', {'mpv': stop_sec}), \
                mock.patch.object(http_server, 'PlayerManager', return_value=feedback_manager), \
                mock.patch.object(http_server.configs, 'check_str_match', return_value=False), \
                mock.patch.object(http_server, 'realtime_playing_request_sender', return_value=False), \
                mock.patch.object(http_server, 'update_server_playback_progress') as update:
            http_server.start_play(data)

        update.assert_called_once_with(stop_sec=10, data=data)
        self.assertNotIn('update_success', data)
        self.assertFalse(http_server.player_is_running)


if __name__ == '__main__':
    unittest.main()
