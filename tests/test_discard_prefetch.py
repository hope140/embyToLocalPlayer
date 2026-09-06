import io
import unittest
from unittest import mock

import utils.downloader as downloader
import utils.player_manager as player_manager
from utils.players import prefetch_data


class FakeResponse:
    def __init__(self, body=b'', status=200, headers=None, read_error=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers or {}
        self.read_error = read_error
        self.read_sizes = []
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.read_error:
            raise self.read_error
        return self._body.read(size)

    def close(self):
        self.closed = True


class StopLoop(Exception):
    pass


class DiscardPrefetchHttpTests(unittest.TestCase):
    url = 'https://media.example/video.mp4?token=secret'

    def test_known_size_requests_exact_inclusive_range_and_closes_response(self):
        response = FakeResponse(
            body=b'x' * 50,
            status=206,
            headers={'Content-Range': 'bytes 0-49/1000'},
        )
        with mock.patch.object(downloader, 'requests_urllib', return_value=response) as request, \
                mock.patch.object(downloader, 'TaskFileManager', side_effect=AssertionError), \
                mock.patch.object(downloader, 'create_sparse_file', side_effect=AssertionError), \
                mock.patch('builtins.open', side_effect=AssertionError):
            self.assertTrue(downloader.prefetch_http_range(self.url, 0, 0.05, size=1000))

        self.assertEqual(request.call_args.kwargs['headers'], {'Range': 'bytes=0-49'})
        self.assertEqual(response.read_sizes, [50])
        self.assertTrue(response.closed)

    def test_ignored_range_for_first_prefix_reads_only_requested_bytes(self):
        response = FakeResponse(body=b'x' * 1000, status=200, headers={'Content-Length': '1000'})
        with mock.patch.object(downloader, 'requests_urllib', return_value=response):
            self.assertTrue(downloader.prefetch_http_range(self.url, 0, 0.05, size=1000))
        self.assertEqual(response.read_sizes, [50])
        self.assertTrue(response.closed)

    def test_unknown_size_uses_head_then_range_and_closes_both_responses(self):
        head = FakeResponse(status=200, headers={'Content-Length': '1000'})
        ranged = FakeResponse(
            body=b'x' * 20,
            status=206,
            headers={'Content-Range': 'bytes 980-999/1000'},
        )
        with mock.patch.object(downloader, 'requests_urllib', side_effect=[head, ranged]) as request:
            self.assertTrue(downloader.prefetch_http_range(self.url, 0.98, 1))

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs['method'], 'HEAD')
        self.assertEqual(request.call_args_list[0].kwargs['retry'], 1)
        self.assertEqual(request.call_args_list[0].kwargs['timeout'], 10)
        self.assertEqual(request.call_args_list[1].kwargs['headers'], {'Range': 'bytes=980-999'})
        self.assertEqual(ranged.read_sizes, [20])
        self.assertTrue(head.closed)
        self.assertTrue(ranged.closed)

    def test_invalid_size_skips_network_without_creating_files(self):
        with mock.patch.object(downloader, 'requests_urllib') as request:
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05, size=0))
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05, size='invalid'))
        request.assert_not_called()

    def test_head_without_length_closes_response_and_skips_range(self):
        head = FakeResponse(status=200)
        with mock.patch.object(downloader, 'requests_urllib', return_value=head) as request:
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05))
        request.assert_called_once()
        self.assertTrue(head.closed)

    def test_mismatched_content_range_and_short_body_fail_safely(self):
        mismatched = FakeResponse(
            body=b'x' * 50,
            status=206,
            headers={'Content-Range': 'bytes 1-50/1000'},
        )
        with mock.patch.object(downloader, 'requests_urllib', return_value=mismatched):
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05, size=1000))
        self.assertEqual(mismatched.read_sizes, [])
        self.assertTrue(mismatched.closed)

        short = FakeResponse(
            body=b'x' * 49,
            status=206,
            headers={'Content-Range': 'bytes 0-49/1000'},
        )
        with mock.patch.object(downloader, 'requests_urllib', return_value=short):
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05, size=1000))
        self.assertEqual(short.read_sizes, [50, 1])
        self.assertTrue(short.closed)

    def test_response_read_error_is_best_effort_and_closes_response(self):
        response = FakeResponse(
            body=b'x' * 50,
            status=206,
            headers={'Content-Range': 'bytes 0-49/1000'},
            read_error=OSError('network failed'),
        )
        with mock.patch.object(downloader, 'requests_urllib', return_value=response):
            self.assertFalse(downloader.prefetch_http_range(self.url, 0, 0.05, size=1000))
        self.assertTrue(response.closed)

    def test_ignored_range_for_nonzero_start_is_skipped_without_reading(self):
        response = FakeResponse(body=b'x' * 1000, status=200, headers={'Content-Length': '1000'})
        with mock.patch.object(downloader, 'requests_urllib', return_value=response):
            self.assertFalse(downloader.prefetch_http_range(self.url, 0.98, 1, size=1000))
        self.assertEqual(response.read_sizes, [])
        self.assertTrue(response.closed)

    def test_player_manager_null_prefetch_uses_two_discard_range_threads(self):
        started = []
        helper = mock.Mock(return_value=True)

        class ImmediateThread:
            def __init__(self, target, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                started.append((target, args, self.kwargs, daemon))

            def start(self):
                self.target(*self.args, **self.kwargs)

        manager = player_manager.PrefetchManager.__new__(player_manager.PrefetchManager)
        manager.playlist_data = {
            'current': {
                'media_path': 'https://media.example/current.mp4',
                'netloc': 'media.example',
                'file_path': '/media/current.mp4',
                'total_sec': 100,
            },
            'next': {
                'media_path': 'https://media.example/next.mp4',
                'stream_url': self.url,
                'netloc': 'media.example',
                'file_path': '/media/next.mp4',
                'total_sec': 100,
                'basename': 'next.mp4',
                'size': 1000,
            },
        }
        manager.player_kwargs = {}

        old_state = (prefetch_data['on'], prefetch_data['stop_sec_dict'], prefetch_data['done_list'])
        prefetch_data['on'] = False
        prefetch_data['stop_sec_dict'] = {'current': 95}
        prefetch_data['done_list'] = []

        def stop_after_iteration(_seconds):
            prefetch_data['on'] = False

        try:
            with mock.patch.object(player_manager, 'prefetch_http_range', helper), \
                    mock.patch.object(player_manager.threading, 'Thread', ImmediateThread), \
                    mock.patch.object(player_manager.time, 'sleep', side_effect=stop_after_iteration), \
                    mock.patch.object(player_manager.configs.raw, 'getfloat', return_value=0), \
                    mock.patch.object(player_manager.configs.raw, 'get',
                                      side_effect=lambda section, key, fallback=None: {
                                          'prefetch_type': 'null',
                                          'prefetch_host': 'media.example',
                                          'prefetch_path': '',
                                      }.get(key, fallback)), \
                    mock.patch.object(player_manager.configs, 'check_str_match', return_value=True):
                manager.prefetch_next_ep_loop()
        finally:
            prefetch_data['on'], prefetch_data['stop_sec_dict'], prefetch_data['done_list'] = old_state

        self.assertEqual(len(started), 2)
        self.assertEqual(helper.call_args_list, [
            mock.call(self.url, 0, 0.08, size=1000),
            mock.call(self.url, 0.98, 1, size=1000),
        ])

    def test_resume_prefetch_uses_discard_ranges_without_downloader(self):
        item = {
            'Id': 'item',
            'ServerId': 'server',
            'SeriesName': 'Show',
            'Path': '/media/Show/S01E01.mp4',
            'MediaSources': [{
                'Id': 'source',
                'Name': 'S01E01.mp4',
                'Path': '/media/Show/S01E01.mp4',
            }],
        }
        playback_info = {
            'PlaySessionId': 'session',
            'MediaSources': [{
                'Id': 'source',
                'Name': 'S01E01.mp4',
                'Path': '/media/Show/S01E01.mp4',
            }],
        }
        emby = mock.Mock()
        emby.host = 'https://emby.example'
        emby.api_key = 'secret'
        emby.get_resume_items.return_value = {'Items': [item]}
        emby.get_playback_info.return_value = playback_info
        helper = mock.Mock(return_value=True)

        def stop_after_iteration(_seconds):
            raise StopLoop

        with mock.patch.object(downloader, 'prefetch_http_range', helper), \
                mock.patch.object(downloader.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(downloader.configs, 'check_str_match', return_value=False), \
                mock.patch.object(downloader.configs, 'ini_str_split', return_value=[]), \
                mock.patch.object(downloader.time, 'sleep', side_effect=stop_after_iteration):
            with self.assertRaises(StopLoop):
                downloader._prefetch_resume_tv(emby, ('/media',))

        self.assertEqual(helper.call_args_list, [
            mock.call(mock.ANY, 0, 0.05, size=None),
            mock.call(mock.ANY, 0.98, 1, size=None),
        ])
        self.assertEqual(helper.call_args[0][0].split('/emby/videos/')[0], 'https://emby.example')


if __name__ == '__main__':
    unittest.main()
