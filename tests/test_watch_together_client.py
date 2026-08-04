import json
import threading
import time
import unittest

from utils.emby_session_api import (
    EmbySessionApi,
    derive_control_device_id,
)
from utils.watch_together_client import WatchTogetherClient


class FakePlayer:
    def __init__(self):
        self.position = 10.0
        self.paused = False
        self.duration = 120.0
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
                                'Arguments': {'Text': 'hello', 'Timeout': 1000}}),
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
        self.client.stop()
        self.assertEqual(self.api.reports[-1][0], 'stopped')

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
