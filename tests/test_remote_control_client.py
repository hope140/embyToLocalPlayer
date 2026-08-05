import hashlib
import json
import subprocess
import sys
import threading
import time
import unittest
import types
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from utils.emby_session_api import (
    EmbySessionError,
    EmbySessionApi,
    derive_control_device_id,
    remote_control_enabled,
)
from utils.data_parser import _extract_auth_identity
from utils.players import get_mpv_snapshot
from utils.player_manager import PrefetchManager
import utils.remote_control_client as remote_control_client_module
from utils.remote_control_client import RemoteControlClient


ROOT = Path(__file__).resolve().parents[1]


class FakePlayer:
    def __init__(self):
        self.position = 10.0
        self.paused = False
        self.duration = 120.0
        self.speed = 1.0
        self.commands = []

    def command(self, *args):
        self.commands.append(args)
        if args[:2] == ('get_property', 'time-pos'):
            return self.position
        if args[:2] == ('get_property', 'pause'):
            return self.paused
        if args[:2] == ('get_property', 'duration'):
            return self.duration
        if args[:2] == ('get_property', 'media-title'):
            return 'episode.mkv'
        if args[:2] == ('get_property', 'speed'):
            return self.speed
        if args[:2] == ('set_property', 'pause'):
            self.paused = bool(args[2])
        elif args and args[0] == 'seek':
            self.position = float(args[1])
        return None


class DelayedPausePlayer(FakePlayer):
    """Fake mpv whose pause property settles after several reads."""

    def __init__(self, pause_apply_after_reads):
        super().__init__()
        self.pause_apply_after_reads = pause_apply_after_reads
        self._pending_pause = None
        self._pause_reads = 0

    def command(self, *args):
        if args[:2] == ('set_property', 'pause'):
            self.commands.append(args)
            self._pending_pause = bool(args[2])
            self._pause_reads = 0
            return None
        if args[:2] == ('get_property', 'pause'):
            if self._pending_pause is not None:
                self._pause_reads += 1
                if self._pause_reads >= self.pause_apply_after_reads:
                    self.paused = self._pending_pause
                    self._pending_pause = None
            return self.paused
        return super().command(*args)


class FakeSessionApi:
    client_name = 'embyToLocalPlayer'
    device_name = 'remote-control'
    client_version = '1.0'
    control_device_id = derive_control_device_id('browser', 'session')
    play_session_id = 'session'
    websocket_url = 'ws://example.test/embywebsocket'
    websocket_headers = []
    session_id = 'server-session'

    def __init__(self):
        self.reports = []
        self.capabilities = []
        self.sessions = 0
        self.calls = []
        self.find_results = []

    def find_session(self, play_session_id=None):
        self.sessions += 1
        self.calls.append(('find_session', play_session_id))
        if self.find_results:
            return self.find_results.pop(0)
        return {'Id': self.session_id}

    def declare_capabilities(self, session_id=None, full=True):
        self.calls.append(('declare_capabilities', session_id, full))
        self.capabilities.append((session_id, full))

    def report_playing(self, **kwargs):
        self.reports.append(('playing', kwargs))

    def report_progress(self, **kwargs):
        self.reports.append(('progress', kwargs))

    def report_stopped(self, **kwargs):
        self.reports.append(('stopped', kwargs))


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.messages = []

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        raise TimeoutError()

    def ping(self):
        return None

    def close(self):
        self.closed = True


class EmbySessionApiTests(unittest.TestCase):
    def test_remote_control_switch_is_independent(self):
        self.assertTrue(remote_control_enabled({"server": "emby"}))
        self.assertFalse(remote_control_enabled({"remote_control_enabled": False}))
        self.assertTrue(remote_control_enabled({"remote_control_enabled": True}))

    def test_remote_control_constructor_override_wins(self):
        self.assertFalse(remote_control_enabled(
            {"remote_control_enabled": True}, override=False,
        ))

    def test_device_id_is_stable_and_session_specific(self):
        first = derive_control_device_id('browser', 'one')
        self.assertEqual(first, derive_control_device_id('browser', 'one'))
        self.assertNotEqual(first, derive_control_device_id('browser', 'two'))
        self.assertNotEqual(first, derive_control_device_id('other-browser', 'one'))
        self.assertLessEqual(len(first), 32)

    def test_headers_do_not_forward_browser_authorization(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {'Items': []}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'device_id': 'browser', 'play_session_id': 'one',
            'item_id': 'item', 'media_source_id': 'source',
        }, request_func=request)
        api.get_sessions()
        headers = requests[0][1]['headers']
        self.assertEqual(headers['X-Emby-Device-Id'], api.control_device_id)
        self.assertNotIn('Authorization', headers)
        self.assertNotIn('Cookie', headers)

    def test_headers_bind_explicit_user_context(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {'Items': []}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'user_id': 'user-1', 'play_session_id': 'one',
        }, request_func=request)
        api.get_sessions()
        headers = requests[0][1]['headers']
        self.assertEqual(headers['X-Emby-User-Id'], 'user-1')
        self.assertIn('UserId="user-1"', headers['X-Emby-Authorization'])
        self.assertIn('X-Emby-User-Id: user-1', api.websocket_headers)
        api.report_playing(position_sec=1)
        self.assertEqual(requests[-1][1]['_json']['UserId'], 'user-1')

    def test_http_and_websocket_share_token_auth_identity_defaults(self):
        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'play_session_id': 'one',
        })
        self.assertEqual(api.client_name, 'Emby Web')
        self.assertEqual(api.device_name, 'embyToLocalPlayer')
        self.assertEqual(api.auth_headers['X-Emby-Client'], api.client_name)
        self.assertEqual(api.auth_headers['X-Emby-Device-Name'], api.device_name)
        self.assertIn(
            f'X-Emby-Client: {api.client_name}', api.websocket_headers,
        )
        self.assertIn(
            f'X-Emby-Device-Name: {api.device_name}', api.websocket_headers,
        )

    def test_auth_identity_fields_override_token_compatible_fallback(self):
        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'auth_client_name': 'Brand Web',
            'auth_device_name': 'brand-player',
            'auth_client_version': '9.1.0',
        })
        self.assertEqual(api.client_name, 'Brand Web')
        self.assertEqual(api.device_name, 'brand-player')
        self.assertEqual(api.client_version, '9.1.0')
        self.assertIn('Client="Brand Web"', api.auth_headers['X-Emby-Authorization'])
        self.assertIn('Device="brand-player"', api.auth_headers['X-Emby-Authorization'])

    def test_auth_identity_uses_only_scalar_api_client_metadata(self):
        identity = _extract_auth_identity({
            '_appName': 'Emby Web',
            '_deviceName': 'embyToLocalPlayer',
            '_appVersion': '',
            '_serverVersion': '4.9.0.30',
            '_userAuthInfo': {'AccessToken': 'not-read'},
        })
        self.assertEqual(identity, {
            'auth_client_name': 'Emby Web',
            'auth_device_name': 'embyToLocalPlayer',
            'auth_client_version': '4.9.0.30',
        })
        self.assertEqual(_extract_auth_identity({
            '_appName': {'nested': 'ignored'},
            '_deviceName': 'bad\nheader',
            '_appVersion': object(),
            '_serverVersion': '1.0',
        }), {
            'auth_client_name': '',
            'auth_device_name': '',
            'auth_client_version': '1.0',
        })

    def test_server_version_is_identity_version_fallback(self):
        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'server_version': '4.9.0.30',
        })
        self.assertEqual(api.client_version, '4.9.0.30')

    def test_websocket_url_preserves_reverse_proxy_prefix(self):
        api = EmbySessionApi({
            'host': 'https://media.test/media', 'api_key': 'token',
            'device_id': 'browser', 'play_session_id': 'one',
        })
        self.assertTrue(api.websocket_url.startswith(
            'wss://media.test/media/embywebsocket?'
        ))
        self.assertTrue(api.http_base_url.endswith('/media/emby'))

    def test_websocket_url_handles_explicit_emby_api_root(self):
        api = EmbySessionApi({
            'host': 'https://media.test/media/emby', 'api_key': 'token',
            'device_id': 'browser', 'play_session_id': 'one',
        })
        self.assertTrue(api.websocket_url.startswith(
            'wss://media.test/media/embywebsocket?'
        ))
        self.assertTrue(api.http_base_url.endswith('/media/emby'))

    def test_capabilities_endpoint_uses_query_session_id(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'device_id': 'browser', 'play_session_id': 'one',
        }, request_func=request)
        api.declare_capabilities(session_id='server-session')
        self.assertTrue(requests[0][0].endswith('/emby/Sessions/Capabilities/Full'))
        self.assertNotIn('server-session', requests[0][0])
        self.assertEqual(requests[0][1]['params'], {'Id': 'server-session'})
        self.assertEqual(
            requests[0][1]['_json']['SupportedCommands'],
            ['PlayPause', 'Stop', 'Pause', 'Unpause', 'Seek', 'DisplayMessage'],
        )

    def test_capabilities_endpoint_uses_query_session_id_for_non_full(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
        }, request_func=request)
        api.declare_capabilities(session_id='server-session', full=False)
        self.assertTrue(requests[0][0].endswith('/emby/Sessions/Capabilities'))
        self.assertEqual(requests[0][1]['params'], {'Id': 'server-session'})

    def test_capabilities_endpoint_rejects_empty_session_id_without_request(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
        }, request_func=request)
        with self.assertRaises(EmbySessionError):
            api.declare_capabilities(session_id='  ')
        with self.assertRaises(EmbySessionError):
            api.declare_capabilities()
        self.assertEqual(requests, [])


class RemoteControlClientTests(unittest.TestCase):
    def setUp(self):
        self.player = FakePlayer()
        self.api = FakeSessionApi()
        self.ws = FakeWebSocket()
        self.client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
            ws_factory=lambda *args, **kwargs: self.ws,
            report_interval=10, heartbeat_interval=100,
        )

    def test_playstate_and_display_message_commands(self):
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate', 'Data': json.dumps({'Command': 'Pause'})
        })))
        self.assertTrue(self.player.paused)
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate', 'Data': json.dumps({'Command': 'Unpause'})
        })))
        self.assertFalse(self.player.paused)
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate',
            'Data': json.dumps({'Command': 'Seek', 'SeekPositionTicks': 50 * 10 ** 7}),
        })))
        self.assertEqual(self.player.position, 50)
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'GeneralCommand',
            'Data': json.dumps({'Name': 'DisplayMessage',
                                'Arguments': {'Text': 'hello', 'TimeoutMs': 1000}}),
        })))
        self.assertIn(('show-text', 'hello', 1000), self.player.commands)

    def test_playpause_toggles_paused_player_and_reports_ack(self):
        self.player.paused = True
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': True}, now=0,
        )
        reports_before = len(self.api.reports)
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate',
            'Data': json.dumps({'Command': 'PLAYPAUSE'}),
        })))
        self.assertFalse(self.player.paused)
        self.assertEqual(self.api.reports[-1][0], 'progress')
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'Unpause')
        self.assertGreater(len(self.api.reports), reports_before)

        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate',
            'Data': json.dumps({'Command': 'PlayPause'}),
        })))
        self.assertTrue(self.player.paused)
        self.assertEqual(self.api.reports[-1][0], 'progress')
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'Pause')

    def test_playpause_with_invalid_snapshot_fails_without_report(self):
        reports_before = len(self.api.reports)
        with mock.patch.object(
            self.client, '_snapshot', side_effect=RuntimeError('snapshot failed')
        ), mock.patch.object(
            remote_control_client_module, 'mpv_set_pause'
        ) as set_pause:
            self.assertFalse(self.client.handle_message(json.dumps({
                'MessageType': 'Playstate',
                'Data': json.dumps({'Command': 'PlayPause'}),
            })))
        set_pause.assert_not_called()
        self.assertEqual(len(self.api.reports), reports_before)

    def test_pause_confirmation_uses_delayed_state_for_report(self):
        player = DelayedPausePlayer(pause_apply_after_reads=3)
        api = FakeSessionApi()
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=player, session_api=api, enabled=True,
        )
        self.assertTrue(client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        ))
        self.assertTrue(client.handle_message(json.dumps({
            'MessageType': 'Playstate',
            'Data': json.dumps({'Command': 'Pause'}),
        })))
        self.assertTrue(player.paused)
        self.assertEqual(api.reports[-1][0], 'progress')
        self.assertTrue(api.reports[-1][1]['is_paused'])
        self.assertEqual(api.reports[-1][1]['event_name'], 'Pause')

    def test_pause_confirmation_timeout_does_not_report_old_state(self):
        player = DelayedPausePlayer(pause_apply_after_reads=1000000)
        api = FakeSessionApi()
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=player, session_api=api, enabled=True,
        )
        self.assertTrue(client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        ))
        reports_before = len(api.reports)
        with mock.patch.object(
            remote_control_client_module, '_PAUSE_CONFIRM_TIMEOUT', 0.03,
        ), mock.patch.object(
            remote_control_client_module, '_PAUSE_CONFIRM_INTERVAL', 0.005,
        ):
            self.assertFalse(client.handle_message(json.dumps({
                'MessageType': 'Playstate',
                'Data': json.dumps({'Command': 'Pause'}),
            })))
        self.assertFalse(player.paused)
        self.assertEqual(len(api.reports), reports_before)

    def test_identity_uses_emby_pipe_delimited_data(self):
        self.assertTrue(self.client._send_identity(self.ws))
        self.assertEqual(self.ws.sent[0]['MessageType'], 'Identity')
        self.assertEqual(
            self.ws.sent[0]['Data'],
            'embyToLocalPlayer|%s|1.0|remote-control'
            % self.api.control_device_id,
        )

    def test_playstate_accepts_case_insensitive_actual_data_fields(self):
        self.assertTrue(self.client.handle_message(json.dumps({
            'messagetype': 'PLAYSTATE',
            'data': json.dumps({
                'command': 'PAUSE', 'playsessionid': 'session',
            }),
        })))
        self.assertTrue(self.player.paused)

    def test_playstate_session_mismatch_logs_safe_reason_and_does_not_execute(self):
        with mock.patch.object(remote_control_client_module.logger, 'info') as info:
            self.assertFalse(self.client.handle_message(json.dumps({
                'MessageType': 'Playstate',
                'Data': json.dumps({
                    'Command': 'Pause', 'PlaySessionId': 'other-session',
                }),
            })))
        self.assertFalse(self.player.paused)
        log_text = ' '.join(
            ' '.join(str(arg) for arg in call.args)
            for call in info.call_args_list
        )
        self.assertIn('play_session_mismatch', log_text)
        self.assertNotIn('other-session', log_text)

    def test_pause_and_small_seek_force_progress_reports(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        )
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate', 'Data': json.dumps({'Command': 'Pause'}),
        })))
        self.assertEqual(self.api.reports[-1][0], 'progress')
        pause_report_count = len(self.api.reports)
        self.assertTrue(self.client.handle_message(json.dumps({
            'MessageType': 'Playstate',
            'Data': json.dumps({'Command': 'Seek', 'SeekPositionSeconds': 11}),
        })))
        self.assertEqual(self.player.position, 11)
        self.assertEqual(len(self.api.reports), pause_report_count + 1)
        self.assertEqual(self.api.reports[-1][0], 'progress')

    def test_failed_command_does_not_force_progress_report(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        )
        with mock.patch.object(
            remote_control_client_module, 'mpv_set_pause', return_value=False
        ) as set_pause, mock.patch.object(self.client, '_report_snapshot') as report:
            self.assertFalse(self.client.handle_message(json.dumps({
                'MessageType': 'Playstate', 'Data': json.dumps({'Command': 'Pause'}),
            })))
        set_pause.assert_called_once_with(self.player, True)
        report.assert_not_called()

    def test_stop_without_snapshot_keeps_stopped_report_path(self):
        class StopWithoutSnapshot:
            def __init__(self):
                self.commands = []

            def command(self, *args):
                self.commands.append(args)
                return None

        player = StopWithoutSnapshot()
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=player, session_api=self.api, enabled=True,
        )
        with mock.patch.object(client, '_report_snapshot', return_value=False) as report:
            self.assertTrue(client.handle_message(json.dumps({
                'MessageType': 'Playstate', 'Data': json.dumps({'Command': 'Stop'}),
            })))
        report.assert_called_once_with(force=True)
        self.assertEqual(self.api.reports[-1][0], 'stopped')

    def test_pause_seek_and_stopped_reports(self):
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        ))
        self.assertEqual(self.api.reports[0][0], 'playing')
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': True}, now=1,
        ))
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'Pause')
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 40, 'is_paused': True}, now=2,
        ))
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'TimeUpdate')

    def test_initial_playing_resolves_session_before_capabilities(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        )
        self.client._declare_capabilities()
        self.assertEqual(
            self.api.calls[:2],
            [('find_session', 'session'), ('declare_capabilities', 'server-session', True)],
        )
        self.assertEqual(self.api.capabilities, [('server-session', True)])

    def test_capabilities_lookup_failure_remains_retryable(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        )
        self.api.find_session = lambda play_session_id=None: None
        self.client._declare_capabilities()
        self.assertFalse(self.client._session_capabilities_declared)

    def test_initial_playing_failure_retries_playing_before_progress(self):
        attempts = []

        def report_playing(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise TimeoutError('temporary request failure')

        self.api.report_playing = report_playing
        self.assertFalse(self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        ))
        self.assertIsNone(self.client._last_snapshot)
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=1,
        ))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(self.client._initial_playing_reported)

    def test_capabilities_retry_is_throttled_on_same_connection(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False}, now=0,
        )
        self.client._ws = self.ws
        self.api.find_results = [None, {'Id': 'server-session'}]
        now = [0.0]
        self.client._clock = lambda: now[0]

        self.client._declare_capabilities()
        self.assertFalse(self.client._session_capabilities_declared)
        self.assertEqual(self.api.sessions, 1)
        now[0] = 0.5
        self.client._declare_capabilities()
        self.assertEqual(self.api.sessions, 1)

        now[0] = 1.0
        self.client._declare_capabilities()
        self.assertTrue(self.client._session_capabilities_declared)
        self.assertEqual(self.api.sessions, 2)
        self.assertEqual(self.api.capabilities, [('server-session', True)])

    def test_playback_rate_is_reported_immediately(self):
        self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False, 'playback_rate': 1.0}, now=0,
        )
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 10, 'is_paused': False, 'playback_rate': 1.5}, now=1,
        ))
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'PlaybackRateChange')
        self.assertEqual(self.api.reports[-1][1]['playback_rate'], 1.5)

    def test_snapshot_reads_speed_and_defaults_to_one(self):
        self.player.speed = 1.75
        self.assertEqual(get_mpv_snapshot(self.player)['playback_rate'], 1.75)
        self.player.speed = None
        self.assertEqual(get_mpv_snapshot(self.player)['playback_rate'], 1.0)

    def test_reconnects_after_socket_disconnect_and_stops_all_sockets(self):
        sockets = []

        class DisconnectSocket(FakeWebSocket):
            def recv(self):
                raise ConnectionError('closed by server')

        class StableSocket(FakeWebSocket):
            def recv(self):
                raise TimeoutError()

        def factory(*_args, **_kwargs):
            ws = DisconnectSocket() if not sockets else StableSocket()
            sockets.append(ws)
            return ws

        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
            ws_factory=factory, reconnect_min=0.01, reconnect_max=0.02,
            ws_timeout=0.05, heartbeat_interval=100,
        )
        self.assertTrue(client.start())
        deadline = time.time() + 1
        while len(sockets) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(sockets), 2)
        client.stop(timeout=1)
        self.assertIsNone(client.thread)
        self.assertTrue(all(socket.closed for socket in sockets))

    def test_missing_websocket_dependency_degrades_without_thread(self):
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                remote_control_client_module,
                '_BUILTIN_WEBSOCKET_FILENAME',
                'websocket-client-test-wheel-is-missing.whl',
            ):
                self.assertFalse(client.start())
            self.assertIsNone(client.thread)
        finally:
            sys.modules.pop('websocket', None)

    def test_bundled_websocket_wheel_metadata_and_hash(self):
        wheel_path = ROOT / 'third_party' / 'websocket_client-1.8.0-py3-none-any.whl'
        self.assertTrue(wheel_path.is_file())
        digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        self.assertEqual(digest, remote_control_client_module._BUILTIN_WEBSOCKET_SHA256)
        with ZipFile(wheel_path) as wheel:
            names = set(wheel.namelist())
            self.assertIn('websocket/__init__.py', names)
            metadata = wheel.read('websocket_client-1.8.0.dist-info/METADATA').decode(
                'utf-8'
            )
            self.assertIn('Version: 1.8.0', metadata)
            self.assertIn('License: Apache-2.0', metadata)
            self.assertIn('websocket_client-1.8.0.dist-info/LICENSE', names)

    def test_bundled_websocket_fallback_in_isolated_subprocess(self):
        script = "\n".join((
            'import sys',
            f"sys.path.insert(0, {str(ROOT)!r})",
            'import importlib',
            "sys.modules['websocket'] = None",
            'from utils.remote_control_client import RemoteControlClient',
            "client = RemoteControlClient({'server': 'emby'}, player=object(), session_api=object(), enabled=True)",
            'factory = client._load_websocket_factory()',
            "module = importlib.import_module('websocket')",
            "assert callable(factory)",
            "assert factory is module.create_connection",
            "assert module.__version__ == '1.8.0'",
        ))
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0)

    def test_bundled_websocket_hash_mismatch_degrades_without_thread(self):
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                remote_control_client_module,
                '_BUILTIN_WEBSOCKET_SHA256',
                '0' * 64,
            ):
                self.assertFalse(client.start())
            self.assertIsNone(client.thread)
        finally:
            sys.modules.pop('websocket', None)

    def test_bundled_websocket_import_failure_cleans_up_path_and_modules(self):
        client = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        wheel_entry = str(
            ROOT / 'third_party' / 'websocket_client-1.8.0-py3-none-any.whl'
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                remote_control_client_module.importlib,
                'import_module',
                side_effect=ImportError('simulated wheel import failure'),
            ):
                self.assertFalse(client.start())
            self.assertIsNone(client.thread)
            self.assertNotIn(wheel_entry, sys.path)
            self.assertNotIn('websocket', sys.modules)
        finally:
            sys.modules.pop('websocket', None)

    def test_system_websocket_module_is_preferred(self):
        fake = types.ModuleType('websocket')

        def fake_factory(*_args, **_kwargs):
            return None

        fake.create_connection = fake_factory
        wheel_entry = str(
            ROOT / 'third_party' / 'websocket_client-1.8.0-py3-none-any.whl'
        )
        before = list(sys.path)
        with mock.patch.dict(sys.modules, {'websocket': fake}):
            client = RemoteControlClient(
                {'server': 'emby', 'remote_control_enabled': True},
                player=self.player, session_api=self.api, enabled=True,
            )
            self.assertIs(client._load_websocket_factory(), fake.create_connection)
        self.assertEqual(sys.path, before)
        self.assertNotIn(wheel_entry, sys.path)

    def test_normal_progress_does_not_look_like_a_seek(self):
        self.client.publish_snapshot({'position_sec': 10, 'is_paused': False}, now=0)
        self.assertFalse(self.client.publish_snapshot(
            {'position_sec': 11, 'is_paused': False}, now=1,
        ))
        self.assertFalse(self.client.publish_snapshot(
            {'position_sec': 12, 'is_paused': False}, now=2,
        ))
        self.assertTrue(self.client.publish_snapshot(
            {'position_sec': 20, 'is_paused': False}, now=10,
        ))
        self.assertEqual(self.api.reports[-1][1]['event_name'], 'TimeUpdate')

    def test_websocket_lifecycle_and_identity(self):
        self.assertTrue(self.client.start())
        deadline = time.time() + 1
        while not self.ws.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.ws.sent)
        self.assertEqual(self.ws.sent[0]['MessageType'], 'Identity')
        self.client.stop()
        self.assertFalse(self.client.thread)
        self.assertTrue(self.ws.closed)
        self.assertEqual(self.api.reports[-1][0], 'stopped')

    def test_missing_player_or_disabled_is_safe(self):
        disabled = RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': False},
            player=self.player, session_api=self.api,
        )
        self.assertFalse(disabled.start())
        self.assertFalse(RemoteControlClient(
            {'server': 'emby', 'remote_control_enabled': True},
            player=None, session_api=self.api, enabled=True,
        ).start())

    def test_remote_control_starts_when_room_sync_is_disabled(self):
        client = RemoteControlClient(
            {
                'server': 'emby',
                'remote_control_enabled': False,
                'remote_control_enabled': True,
            },
            player=self.player, session_api=self.api, enabled=None,
            ws_factory=lambda *args, **kwargs: self.ws,
            heartbeat_interval=100,
        )
        self.assertTrue(client.start())
        deadline = time.time() + 1
        while not self.ws.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.ws.sent)
        client.stop(timeout=1)

    def test_remote_control_explicitly_disabled_does_not_start(self):
        client = RemoteControlClient(
            {
                'server': 'emby',
                'remote_control_enabled': True,
                'remote_control_enabled': False,
            },
            player=self.player, session_api=self.api, enabled=None,
            ws_factory=lambda *args, **kwargs: self.ws,
        )
        self.assertFalse(client.start())
        self.assertIsNone(client.thread)

    def test_player_manager_reuses_and_serialises_remote_client_lifecycle(self):
        manager = PrefetchManager(
            data={
                'server': 'emby', 'scheme': 'https', 'netloc': 'media.test',
                'api_key': 'token', 'user_id': 'user',
            }, player_name='mpv', player_path='mpv'
        )
        manager.player_kwargs = {'mpv': self.player}
        fake_client = mock.Mock()
        fake_client.start.return_value = True
        with mock.patch('utils.player_manager.RemoteControlClient', return_value=fake_client) as factory:
            manager.start_realtime_playing_feedback()
            manager.start_realtime_playing_feedback()
            self.assertEqual(factory.call_count, 1)
            self.assertIs(manager.remote_control_client, fake_client)
            self.assertIs(manager.remote_control_client, fake_client)
            manager.stop_realtime_playing_feedback()
            self.assertEqual(fake_client.stop.call_count, 1)
            self.assertIsNone(manager.remote_control_client)


if __name__ == '__main__':
    unittest.main()
