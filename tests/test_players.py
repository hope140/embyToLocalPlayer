import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import utils.players as players


class _FakeMpv:
    def __init__(self, path):
        self.path = path
        self.native_chapters = []
        self.set_chapters = []
        self.chapter_event = threading.Event()
        self.callbacks = {}
        self.is_iina = False
        self.is_mpvnet = False

    def on_event(self, name):
        def register(callback):
            self.callbacks.setdefault(name, []).append(callback)
            return callback
        return register

    def command(self, command, *args):
        if (command, *args[:1]) == ('get_property', 'path'):
            return self.path
        if (command, *args[:1]) == ('get_property', 'chapter-list'):
            return self.native_chapters
        if command == 'set_property' and args[0] == 'chapter-list':
            self.native_chapters = args[1]
            self.set_chapters.append(args[1])
            self.chapter_event.set()
            return None
        if (command, *args[:1]) == ('get_property', 'command-list'):
            return []
        return None


class _PlaylistFakeMpv(_FakeMpv):
    def __init__(self, path, supports_index=True):
        super().__init__(path)
        self.supports_index = supports_index
        self.load_calls = []
        self.load_event = threading.Event()
        self.playlist_done = threading.Event()
        self.time_pos = 0.0
        self.time_pos_reads = 0
        self.auto_advance_time_pos = True
        self.time_pos_read_event = threading.Event()
        self.time_pos_error = None

    def command(self, command, *args):
        if command == 'get_property' and args == ('time-pos',):
            self.time_pos_read_event.set()
            if self.time_pos_error is not None:
                raise self.time_pos_error
            self.time_pos_reads += 1
            if self.auto_advance_time_pos:
                return 0.0 if self.time_pos_reads == 1 else 0.1
            return self.time_pos
        if command == 'get_property' and args == ('command-list',):
            if self.supports_index:
                return [{'name': 'loadfile', 'args': [{'name': 'index'}]}]
            return [{'name': 'loadfile', 'args': []}]
        if command == 'loadfile':
            self.load_calls.append(args)
            self.load_event.set()
            return None
        if command == 'script-message' and args == ('etlp-playlist-done',):
            self.playlist_done.set()
            return None
        return super().command(command, *args)


class MpvStartupArgumentTests(unittest.TestCase):
    def _start_and_get_command(self, data):
        command = [r'C:\Program140\mpv\mpv.exe', 'http://127.0.0.1:58000/cd2/test']
        with mock.patch.object(players.subprocess, 'Popen') as popen, \
                mock.patch.object(players, 'activate_window_by_pid'), \
                mock.patch.object(players, 'init_player_instance', return_value=None), \
                mock.patch.object(players.configs, 'fullscreen', False), \
                mock.patch.object(players.configs, 'player_proxy', ''):
            players.mpv_player_start(
                cmd=command,
                media_title='Movie.mkv',
                mount_disk_mode=False,
                data=data,
                get_stop_sec=False,
            )
        return popen.call_args.args[0]

    def test_cd2_gateway_does_not_force_window_before_media_load(self):
        command = self._start_and_get_command({'use_strm_cd2_url': True})
        self.assertNotIn('--force-window=immediate', command)

    def test_ordinary_remote_http_keeps_eager_window(self):
        command = self._start_and_get_command({})
        self.assertIn('--force-window=immediate', command)

    def test_autocreate_playlist_is_disabled_for_etlp_managed_playlist(self):
        command = self._start_and_get_command({'use_strm_cd2_url': True})
        self.assertIn('--autocreate-playlist=no', command)

    def test_ipc_playlist_debug_data_redacts_paths_commands_urls_and_headers(self):
        payload = {
            'Episode 1': {
                'media_title': 'Episode 1',
                'order': 3,
                'media_path': 'http://127.0.0.1:58000/cd2/n?X-Emby-Token=EMBYSECRET',
                'source_path': 'https://plex.example/video?X-Plex-Token=PLEXSECRET',
                'mpv_cmd': ['loadfile', 'https://media.example/v?api_key=APISECRET'],
                'options': 'Authorization=Bearer AUTHSECRET',
                'api_key': 'APISECRET',
                'headers': {
                    'Authorization': 'Bearer AUTHSECRET',
                    'Cookie': 'session=COOKIESECRET',
                },
                'play_session_id': 'SESSIONSECRET',
                'nested': {
                    'title': 'Keep this title',
                    'url': 'https://media.example/v?Cookie=COOKIESECRET&ok=yes',
                    'token': 'TOKENSECRET',
                },
            },
        }
        encoded = json.dumps(players.redact_playlist_data_for_ipc(payload), ensure_ascii=False)

        for secret in (
                'EMBYSECRET', 'PLEXSECRET', 'APISECRET', 'AUTHSECRET',
                'COOKIESECRET', 'SESSIONSECRET', 'TOKENSECRET'):
            self.assertNotIn(secret, encoded)
        for sensitive_key in ('media_path', 'source_path', 'mpv_cmd', 'options',
                              'api_key', 'headers', 'play_session_id'):
            self.assertNotIn(sensitive_key, encoded)
        self.assertIn('Episode 1', encoded)
        self.assertIn('Keep this title', encoded)
        self.assertIn('"order": 3', encoded)


class MpvChapterPlaybackTests(unittest.TestCase):
    def test_start_entry_registers_cmd_path_and_injects_loaded_first_item(self):
        startup = 'http://127.0.0.1:58000/cd2/startup'
        mpv = _FakeMpv(startup)
        command = ['mpv', startup]
        data = {
            'media_path': startup,
            'chapters': [{'title': 'Opening', 'time': 0.0},
                         {'title': 'Main', 'time': 100.5}],
            'use_strm_cd2_url': True,
            'sub_inner_idx': 0,
        }
        with mock.patch.object(players.subprocess, 'Popen') as popen, \
                mock.patch.object(players, 'activate_window_by_pid'), \
                mock.patch.object(players, 'init_player_instance', return_value=mpv), \
                mock.patch.object(players.configs, 'fullscreen', False), \
                mock.patch.object(players.configs, 'player_proxy', ''):
            players.mpv_player_start(
                cmd=command, media_title='Episode', mount_disk_mode=False,
                data=data, get_stop_sec=False,
            )

        self.assertEqual(popen.call_args.args[0][1], startup)
        self.assertEqual(mpv._etlp_startup_media_path, startup)
        self.assertEqual(mpv.set_chapters, [[
            {'title': 'Opening', 'time': 0.0},
            {'title': 'Main', 'time': 100.5},
        ]])
        self.assertEqual(len(mpv.callbacks['file-loaded']), 1)

    def test_refreshed_current_item_chapters_are_applied_to_original_gateway(self):
        startup = 'http://127.0.0.1:58000/cd2/startup'
        refreshed = 'http://127.0.0.1:58000/cd2/refreshed'
        mpv = _FakeMpv(startup)
        data = {
            'basename': 'ep-1.mkv', 'media_path': startup,
            'item_id': 'ep-1', 'mount_disk_mode': False,
            'media_title': 'Episode 1', 'chapters': [], 'sub_file': None,
        }
        episodes = [{
            'basename': 'ep-1.mkv', 'media_path': refreshed,
            'item_id': 'ep-1', 'media_title': 'Episode 1', 'sub_file': None,
            'chapters': [{'title': 'Opening', 'time': 0.0},
                         {'title': 'Credits', 'time': 321.75}],
        }]
        with mock.patch.object(players, 'list_episodes', return_value=episodes), \
                mock.patch.object(players.configs.raw, 'getboolean', return_value=False):
            players.playlist_add_mpv(mpv, data, limit=1)
            self.assertTrue(mpv.chapter_event.wait(1))

        self.assertEqual(mpv.set_chapters, [[
            {'title': 'Opening', 'time': 0.0},
            {'title': 'Credits', 'time': 321.75},
        ]])
        self.assertIn(players._mpv_path_key(startup), mpv._etlp_chapters_by_path)
        self.assertIn(players._mpv_path_key(refreshed), mpv._etlp_chapters_by_path)

        # The actual startup command can instead point to a GUI cache file.
        # The later parser response still contains only the gateway URLs.
        mpv = _FakeMpv(os.path.abspath('cache/ep-1.mkv'))
        mpv._etlp_startup_media_path = mpv.path
        with mock.patch.object(players, 'list_episodes', return_value=episodes), \
                mock.patch.object(players.configs.raw, 'getboolean', return_value=False):
            players.playlist_add_mpv(mpv, data, limit=1)
            self.assertTrue(mpv.chapter_event.wait(1))
        self.assertEqual(mpv.native_chapters, episodes[0]['chapters'])

    def test_playlist_waits_for_playback_progress_before_adding(self):
        startup = 'http://127.0.0.1:58000/cd2/startup'
        next_gateway = 'http://127.0.0.1:58000/cd2/next'
        mpv = _PlaylistFakeMpv(startup)
        mpv.auto_advance_time_pos = False
        data = {
            'basename': 'ep-1.mkv', 'media_path': startup,
            'item_id': 'ep-1', 'mount_disk_mode': False,
            'media_title': 'Episode 1', 'chapters': [], 'sub_file': None,
        }
        episodes = [
            dict(data),
            {
                'basename': 'ep-2.mkv', 'media_path': next_gateway,
                'item_id': 'ep-2', 'media_title': 'Episode 2',
                'chapters': [], 'sub_file': None,
            },
        ]
        with mock.patch.object(players.configs.raw, 'getboolean', return_value=False):
            players.playlist_add_mpv(mpv, data, eps_data=episodes, limit=2)
            self.assertTrue(mpv.time_pos_read_event.wait(1))
            self.assertFalse(mpv.load_event.wait(0.1))
            mpv.time_pos = 0.5
            self.assertTrue(mpv.load_event.wait(1))
            self.assertTrue(mpv.playlist_done.wait(1))

    def test_playlist_start_timeout_skips_addition_and_done_message(self):
        startup = 'http://127.0.0.1:58000/cd2/startup'
        next_gateway = 'http://127.0.0.1:58000/cd2/next'
        mpv = _PlaylistFakeMpv(startup)
        mpv.auto_advance_time_pos = False
        data = {
            'basename': 'ep-1.mkv', 'media_path': startup,
            'item_id': 'ep-1', 'mount_disk_mode': False,
            'media_title': 'Episode 1', 'chapters': [], 'sub_file': None,
        }
        episodes = [
            dict(data),
            {
                'basename': 'ep-2.mkv', 'media_path': next_gateway,
                'item_id': 'ep-2', 'media_title': 'Episode 2',
                'chapters': [], 'sub_file': None,
            },
        ]
        with mock.patch.object(players.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(players, '_MPV_PLAYLIST_START_TIMEOUT_SECONDS', 0.05), \
                mock.patch.object(players, '_MPV_PLAYLIST_START_POLL_SECONDS', 0.01):
            players.playlist_add_mpv(mpv, data, eps_data=episodes, limit=2)
            self.assertTrue(mpv.time_pos_read_event.wait(1))
            self.assertFalse(mpv.load_event.wait(0.2))
            self.assertFalse(mpv.playlist_done.is_set())

    def test_playlist_ipc_close_skips_addition_and_done_message(self):
        startup = 'http://127.0.0.1:58000/cd2/startup'
        next_gateway = 'http://127.0.0.1:58000/cd2/next'
        mpv = _PlaylistFakeMpv(startup)
        mpv.time_pos_error = players.MPVError('closed')
        data = {
            'basename': 'ep-1.mkv', 'media_path': startup,
            'item_id': 'ep-1', 'mount_disk_mode': False,
            'media_title': 'Episode 1', 'chapters': [], 'sub_file': None,
        }
        episodes = [
            dict(data),
            {
                'basename': 'ep-2.mkv', 'media_path': next_gateway,
                'item_id': 'ep-2', 'media_title': 'Episode 2',
                'chapters': [], 'sub_file': None,
            },
        ]
        with mock.patch.object(players.configs.raw, 'getboolean', return_value=False):
            players.playlist_add_mpv(mpv, data, eps_data=episodes, limit=2)
            self.assertTrue(mpv.time_pos_read_event.wait(1))
            self.assertFalse(mpv.load_event.wait(0.2))
            self.assertFalse(mpv.playlist_done.is_set())

    def test_playlist_registers_actual_gui_cache_path_for_old_and_new_loadfile(self):
        for supports_index in (False, True):
            with self.subTest(supports_index=supports_index):
                startup = 'http://127.0.0.1:58000/cd2/startup'
                next_gateway = 'http://127.0.0.1:58000/cd2/next'
                mpv = _PlaylistFakeMpv(startup, supports_index=supports_index)
                data = {
                    'basename': 'ep-1.mkv', 'media_path': startup,
                    'item_id': 'ep-1', 'mount_disk_mode': False,
                    'media_title': 'Episode 1',
                    'chapters': [{'title': 'Opening', 'time': 0.0}],
                    'gui_without_confirm': True,
                    'sub_file': None,
                }
                episodes = [
                    dict(data),
                    {
                        'basename': 'ep-2.mkv', 'media_path': next_gateway,
                        'item_id': 'ep-2', 'fake_name': 'ep-2.mkv',
                        'media_title': 'Episode 2', 'sub_file': None,
                        'sub_inner_idx': 0,
                        'chapters': [{'title': 'Credits', 'time': 222.25}],
                        'source_path': next_gateway,
                    },
                ]
                with tempfile.TemporaryDirectory() as cache_dir, \
                        mock.patch.object(players.configs.raw, 'getboolean', return_value=False), \
                        mock.patch.object(players.configs, 'cache_path', cache_dir):
                    players.playlist_add_mpv(mpv, data, eps_data=episodes, limit=2)
                    self.assertTrue(mpv.load_event.wait(1))
                    self.assertEqual(len(mpv.load_calls), 1)
                    cache_path = os.path.join(cache_dir, 'ep-2.mkv')
                    self.assertEqual(mpv.load_calls[0][0], cache_path)
                    self.assertEqual(len(mpv.load_calls[0]), 4 if supports_index else 3)
                    self.assertEqual(mpv.load_calls[0][1], 'append')
                    self.assertNotIn('chapters-file', mpv.load_calls[0][-1])
                    self.assertTrue(mpv.playlist_done.wait(1))

                mpv.path = cache_path
                mpv.native_chapters = []
                mpv.callbacks['file-loaded'][0]({})
                self.assertEqual(mpv.set_chapters[-1], [
                    {'title': 'Credits', 'time': 222.25},
                ])

    def test_file_loaded_uses_actual_path_and_preserves_native_chapters(self):
        startup = os.path.join('relative', 'episode-1.mkv')
        mpv = _FakeMpv(os.path.abspath(startup))
        players._register_mpv_chapter_data(
            mpv,
            {'item_id': 'ep-1', 'media_path': startup,
             'chapters': [{'title': 'Opening', 'time': 0.0},
                          {'title': 'Credits', 'time': 100.5}]},
        )
        players._ensure_mpv_chapter_listener(mpv)
        players._ensure_mpv_chapter_listener(mpv)
        self.assertEqual(len(mpv.callbacks['file-loaded']), 1)

        embedded = [{'title': 'Embedded chapter', 'time': 12.0}]
        mpv.native_chapters = embedded
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(mpv.native_chapters, embedded)
        self.assertEqual(mpv.set_chapters, [])

        mpv.native_chapters = []
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(mpv.set_chapters, [[
            {'title': 'Opening', 'time': 0.0},
            {'title': 'Credits', 'time': 100.5},
        ]])

        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(len(mpv.set_chapters), 1)

    def test_inserted_and_appended_cd2_episodes_keep_their_own_chapters(self):
        def episode(number):
            return {
                'item_id': f'ep-{number}', 'basename': f'ep-{number}.strm',
                'media_path': f'http://127.0.0.1:58000/cd2/episode-{number}',
                'media_title': f'Episode {number}', 'sub_file': None,
                'mount_disk_mode': True,
                'chapters': [{'title': f'Chapter {number}', 'time': number + 0.25}],
            }

        episodes = [episode(number) for number in (1, 2, 3)]
        current = episodes[1]
        mpv = _PlaylistFakeMpv(current['media_path'])
        with mock.patch.object(players.configs.raw, 'getboolean', return_value=False):
            players.playlist_add_mpv(mpv, current, eps_data=episodes, limit=2)
            self.assertTrue(mpv.playlist_done.wait(1))

        calls = {args[0]: args for args in mpv.load_calls}
        self.assertEqual(set(calls), {episodes[0]['media_path'], episodes[2]['media_path']})
        self.assertEqual(calls[episodes[0]['media_path']][1:3], ('insert-at', '0'))
        self.assertEqual(calls[episodes[2]['media_path']][1:3], ('append', '-1'))
        for ep in (episodes[2], episodes[0], current):
            mpv.path = ep['media_path']
            mpv.native_chapters = []
            mpv.callbacks['file-loaded'][0]({})
            self.assertEqual(mpv.native_chapters, ep['chapters'])

    def test_gateway_paths_are_isolated_and_legacy_intro_is_supported(self):
        first = 'http://127.0.0.1:58000/cd2/nonce-a'
        second = 'http://127.0.0.1:58000/cd2/nonce-b'
        mpv = _FakeMpv(first)
        players._register_mpv_chapter_data(
            mpv,
            {'item_id': 'ep-1', 'media_path': first,
             'intro_start': 0.0, 'intro_end': 42.25},
        )
        players._register_mpv_chapter_data(
            mpv,
            {'item_id': 'ep-2', 'media_path': second, 'chapters': []},
        )
        players._ensure_mpv_chapter_listener(mpv)
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(mpv.set_chapters, [[
            {'title': 'Opening', 'time': 0.0},
            {'title': 'Main', 'time': 42.25},
        ]])

        mpv.path = second
        mpv.native_chapters = []
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(len(mpv.set_chapters), 1)

        mpv.path = 'http://127.0.0.1:58000/cd2/unknown'
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(len(mpv.set_chapters), 1)

        mpv.path = first
        mpv.native_chapters = []
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(mpv.set_chapters[-1], [
            {'title': 'Opening', 'time': 0.0},
            {'title': 'Main', 'time': 42.25},
        ])

    def test_path_change_between_reads_skips_stale_injection(self):
        first = 'http://127.0.0.1:58000/cd2/nonce-a'
        second = 'http://127.0.0.1:58000/cd2/nonce-b'
        mpv = _FakeMpv(first)
        players._register_mpv_chapter_data(
            mpv,
            {'item_id': 'ep-1', 'media_path': first,
             'chapters': [{'title': 'Opening', 'time': 0.0}]},
        )
        players._ensure_mpv_chapter_listener(mpv)
        original_command = mpv.command
        path_reads = 0

        def changing_command(command, *args):
            nonlocal path_reads
            if command == 'get_property' and args == ('path',):
                path_reads += 1
                if path_reads == 2:
                    mpv.path = second
            return original_command(command, *args)

        mpv.command = changing_command
        mpv.callbacks['file-loaded'][0]({})
        self.assertEqual(mpv.set_chapters, [])


if __name__ == '__main__':
    unittest.main()
