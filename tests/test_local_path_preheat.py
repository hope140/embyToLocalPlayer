from configparser import ConfigParser
import io
import threading
import unittest
from unittest import mock

import utils.tools as tools


class LocalPathPreheatTests(unittest.TestCase):
    def make_config(self, **dev_options):
        config = ConfigParser()
        config.read_dict({
            'emby': {'player': 'mpv'},
            'exe': {'mpv': 'player.exe'},
            'dandan': {'enable': 'no'},
            'dev': {
                'player_by_path': '',
                'strm_local_path_retry_seconds': '0',
                **dev_options,
            },
        })
        return config

    def test_preheat_success_reads_configured_prefix(self):
        media_file = mock.mock_open(read_data=b'video data')
        monotonic = mock.Mock(side_effect=[0, 0.1, 0.2])

        with mock.patch.object(tools._logger, 'info') as log_info:
            result = tools._preheat_local_media_path(
                'Z:/mounted/movie.mkv', 1024, 1,
                open_func=media_file, monotonic_func=monotonic,
            )

        self.assertTrue(result)
        media_file.assert_called_once_with('Z:/mounted/movie.mkv', 'rb')
        media_file.return_value.read.assert_called_once_with(1024)
        log_info.assert_called_once()

    def test_preheat_read_failure_is_best_effort(self):
        def open_failure(*args, **kwargs):
            raise OSError('mount read failed')

        with mock.patch.object(tools._logger, 'warn') as log_warn:
            result = tools._preheat_local_media_path(
                'Z:/mounted/movie.mkv', 1024, 1,
                open_func=open_failure, monotonic_func=lambda: 0,
            )

        self.assertFalse(result)
        log_warn.assert_called_once()
        self.assertIn('preheat failed', log_warn.call_args.args[0])

    def test_preheat_timeout_is_best_effort(self):
        read_started = threading.Event()
        release_read = threading.Event()

        def blocking_open(*args, **kwargs):
            read_started.set()
            release_read.wait(1)
            return io.BytesIO(b'video data')

        with mock.patch.object(tools._logger, 'warn') as log_warn:
            result = tools._preheat_local_media_path(
                'Z:/mounted/movie.mkv', 1024, 0.01, open_func=blocking_open)
            release_read.set()

        self.assertTrue(read_started.is_set())
        self.assertFalse(result)
        log_warn.assert_called_once()
        self.assertIn('preheat timed out', log_warn.call_args.args[0])

    def test_get_player_cmd_preheats_ready_local_strm_path(self):
        config = self.make_config(
            strm_local_path_preheat='yes',
            strm_local_path_preheat_bytes='2048',
            strm_local_path_preheat_timeout_seconds='1.5',
        )
        with mock.patch.object(tools.configs, 'raw', config), \
                mock.patch.object(tools.os.path, 'exists', return_value=True), \
                mock.patch.object(tools, '_preheat_local_media_path', return_value=True) as preheat:
            result = tools.get_player_cmd(
                'Z:/mounted/movie.mkv', 'movie.strm',
                data={'use_strm_local_path': True},
            )

        self.assertEqual(['player.exe', 'Z:/mounted/movie.mkv'], result)
        preheat.assert_called_once_with('Z:/mounted/movie.mkv', 2048, 1.5)

    def test_get_player_cmd_skips_preheat_when_disabled(self):
        config = self.make_config(strm_local_path_preheat='no')
        with mock.patch.object(tools.configs, 'raw', config), \
                mock.patch.object(tools.os.path, 'exists', return_value=True), \
                mock.patch.object(tools, '_preheat_local_media_path') as preheat:
            result = tools.get_player_cmd(
                'Z:/mounted/movie.mkv', 'movie.strm',
                data={'use_strm_local_path': True},
            )

        self.assertEqual(['player.exe', 'Z:/mounted/movie.mkv'], result)
        preheat.assert_not_called()

    def test_get_player_cmd_does_not_preheat_http_direct_link(self):
        config = self.make_config(strm_local_path_preheat='yes')
        with mock.patch.object(tools.configs, 'raw', config), \
                mock.patch.object(tools, '_preheat_local_media_path') as preheat:
            result = tools.get_player_cmd(
                'https://example.test/movie.mkv', 'movie.strm',
                data={'use_strm_local_path': True},
            )

        self.assertEqual(['player.exe', 'https://example.test/movie.mkv'], result)
        preheat.assert_not_called()

    def test_get_player_cmd_keeps_file_not_found_semantics(self):
        config = self.make_config()
        with mock.patch.object(tools.configs, 'raw', config), \
                mock.patch.object(tools.os.path, 'exists', return_value=False), \
                mock.patch.object(tools, '_preheat_local_media_path') as preheat:
            with self.assertRaises(FileNotFoundError):
                tools.get_player_cmd(
                    'Z:/mounted/movie.mkv', 'movie.strm',
                    data={'use_strm_local_path': True},
                )

        preheat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
