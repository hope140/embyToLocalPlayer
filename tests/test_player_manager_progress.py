import unittest
from unittest import mock

import utils.player_manager as player_manager
from utils.player_manager import PlayerManager


class PlaylistPositionFallbackTests(unittest.TestCase):
    def make_manager(self, mpv):
        manager = PlayerManager.__new__(PlayerManager)
        manager.data = {}
        manager.player_name = 'mpv'
        manager.player_kwargs = {'mpv': mpv}
        manager.playlist_time = {}
        manager.playlist_total_sec = {}
        manager.is_http_sub = False
        manager.stop_realtime_playing_feedback = mock.Mock()
        return manager

    def test_position_fallback_fills_title_key_missing_records(self):
        mpv = mock.Mock()
        mpv.is_iina = False
        mpv.mpv_playlist_titles = ['EP1', 'EP2', 'EP3']
        mpv.mpv_stop_sec_by_pos = {2: 123}
        manager = self.make_manager(mpv)

        fake_stop_sec = mock.Mock(return_value=({'EP1': 45}, {'EP1': 3000}))
        with mock.patch.object(player_manager, 'stop_sec_func_dict', {'mpv': fake_stop_sec}), \
                mock.patch.object(player_manager.configs.raw, 'getboolean', return_value=False):
            manager.update_playlist_time_loop()

        self.assertEqual(manager.playlist_time, {'EP1': 45, 'EP3': 123})

    def test_position_fallback_does_not_overwrite_existing_title(self):
        mpv = mock.Mock()
        mpv.is_iina = False
        mpv.mpv_playlist_titles = ['EP1', 'EP2']
        mpv.mpv_stop_sec_by_pos = {1: 999, 0: 77}
        manager = self.make_manager(mpv)

        fake_stop_sec = mock.Mock(return_value=({'EP1': 45, 'EP2': 88}, {'EP1': 3000}))
        with mock.patch.object(player_manager, 'stop_sec_func_dict', {'mpv': fake_stop_sec}), \
                mock.patch.object(player_manager.configs.raw, 'getboolean', return_value=False):
            manager.update_playlist_time_loop()

        self.assertEqual(manager.playlist_time, {'EP1': 45, 'EP2': 88})

    def test_position_fallback_skips_iina_and_bad_positions(self):
        mpv = mock.Mock()
        mpv.is_iina = True
        mpv.mpv_playlist_titles = ['EP1', 'EP2']
        mpv.mpv_stop_sec_by_pos = {1: 123, 99: 456, 'bad': 789}
        manager = self.make_manager(mpv)

        fake_stop_sec = mock.Mock(return_value=({'EP1': 45}, {'EP1': 3000}))
        with mock.patch.object(player_manager, 'stop_sec_func_dict', {'mpv': fake_stop_sec}), \
                mock.patch.object(player_manager.configs.raw, 'getboolean', return_value=False):
            manager.update_playlist_time_loop()

        self.assertEqual(manager.playlist_time, {'EP1': 45})


class UpdatePlaybackForEpsTests(unittest.TestCase):
    def make_manager(self, times=None):
        manager = PlayerManager.__new__(PlayerManager)
        manager.data = {}
        manager.player_name = 'mpv'
        manager.player_kwargs = {'mpv': mock.Mock()}
        manager.playlist_total_sec = {}
        manager.playlist_time = times or {'EP1': 100, 'EP2': 200}
        manager.playlist_data = {
            'EP1': {'basename': 'ep1.strm', 'server': 'emby', 'total_sec': 3600 * 24,
                    'start_sec': 0, 'netloc': 'host', 'item_id': 'id1', 'order': 0,
                    'media_title': 'EP1', 'file_path': 'p1'},
            'EP2': {'basename': 'ep2.strm', 'server': 'emby', 'total_sec': 3600 * 24,
                    'start_sec': 0, 'netloc': 'host', 'item_id': 'id2', 'order': 1,
                    'media_title': 'EP2', 'file_path': 'p2'},
        }
        manager.emby_thin = mock.Mock()
        manager.emby_thin.get_playback_info.return_value = {
            'MediaSources': [{'RunTimeTicks': 27000000000, 'Id': 'id2'}],
        }
        return manager

    def test_episode_failure_does_not_drop_other_episodes(self):
        manager = self.make_manager()
        manager.emby_thin.get_playback_info.side_effect = [
            RuntimeError('emby down'),
            {'MediaSources': [{'RunTimeTicks': 27000000000, 'Id': 'id2'}]},
        ]
        updated = []

        with mock.patch.object(
                player_manager, 'update_server_playback_progress',
                side_effect=lambda stop_sec, data: updated.append((stop_sec, data['basename']))):
            manager.update_playback_for_eps()

        self.assertEqual(updated, [(200, 'ep2.strm')])

    def test_known_runtime_episodes_update_directly(self):
        manager = self.make_manager()
        manager.playlist_data['EP1']['total_sec'] = 3000
        manager.playlist_data['EP2']['total_sec'] = 3000
        updated = []

        with mock.patch.object(
                player_manager, 'update_server_playback_progress',
                side_effect=lambda stop_sec, data: updated.append(stop_sec)):
            manager.update_playback_for_eps()

        self.assertEqual(updated, [100, 200])


if __name__ == '__main__':
    unittest.main()
