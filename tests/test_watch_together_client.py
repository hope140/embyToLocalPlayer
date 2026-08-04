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
    EmbySessionApi,
    derive_control_device_id,
)
from utils.players import get_mpv_snapshot
import utils.watch_together_client as watch_together_client_module
from utils.watch_together_client import WatchTogetherClient


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


class FakeSessionApi:
    client_name = 'embyToLocalPlayer'
    device_name = 'watch-together'
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

    def find_session(self):
        self.sessions += 1
        return {'Id': self.session_id}

    def declare_capabilities(self, session_id=None, full=True):
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

    def test_capabilities_endpoint_has_no_session_id(self):
        requests = []

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {}

        api = EmbySessionApi({
            'scheme': 'https', 'netloc': 'media.test', 'api_key': 'token',
            'device_id': 'browser', 'play_session_id': 'one',
        }, request_func=request)
        api.declare_capabilities(session_id='must-not-be-in-url')
        self.assertTrue(requests[0][0].endswith('/emby/Sessions/Capabilities/Full'))
        self.assertNotIn('must-not-be-in-url', requests[0][0])
        self.assertEqual(
            requests[0][1]['_json']['SupportedCommands'],
            ['Pause', 'Unpause', 'Seek', 'DisplayMessage'],
        )


class WatchTogetherClientTests(unittest.TestCase):
    def setUp(self):
        self.player = FakePlayer()
        self.api = FakeSessionApi()
        self.ws = FakeWebSocket()
        self.client = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
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

        client = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
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
        client = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                watch_together_client_module,
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
        self.assertEqual(digest, watch_together_client_module._BUILTIN_WEBSOCKET_SHA256)
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
            'from utils.watch_together_client import WatchTogetherClient',
            "client = WatchTogetherClient({'server': 'emby'}, player=object(), session_api=object(), enabled=True)",
            'factory = client._load_websocket_factory()',
            "module = importlib.import_module('websocket')",
            "assert callable(factory)",
            "assert factory is module.create_connection",
            "assert module.__version__ == '1.8.0'",
            "sys.__stdout__.write(module.__version__ + '\\n')",
            "sys.__stdout__.flush()",
        ))
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding='utf-8',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.lstrip('\ufeff').strip(), '1.8.0')

    def test_bundled_websocket_hash_mismatch_degrades_without_thread(self):
        client = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                watch_together_client_module,
                '_BUILTIN_WEBSOCKET_SHA256',
                '0' * 64,
            ):
                self.assertFalse(client.start())
            self.assertIsNone(client.thread)
        finally:
            sys.modules.pop('websocket', None)

    def test_bundled_websocket_import_failure_cleans_up_path_and_modules(self):
        client = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
            player=self.player, session_api=self.api, enabled=True,
        )
        wheel_entry = str(
            ROOT / 'third_party' / 'websocket_client-1.8.0-py3-none-any.whl'
        )
        try:
            with mock.patch.dict(sys.modules, {'websocket': None}), mock.patch.object(
                watch_together_client_module.importlib,
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
            client = WatchTogetherClient(
                {'server': 'emby', 'watch_together_enabled': True},
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
        disabled = WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': False},
            player=self.player, session_api=self.api,
        )
        self.assertFalse(disabled.start())
        self.assertFalse(WatchTogetherClient(
            {'server': 'emby', 'watch_together_enabled': True},
            player=None, session_api=self.api, enabled=True,
        ).start())


if __name__ == '__main__':
    unittest.main()
