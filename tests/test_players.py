import unittest
from unittest import mock

import utils.players as players


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


if __name__ == '__main__':
    unittest.main()
