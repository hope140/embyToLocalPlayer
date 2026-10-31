import io
import copy
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import utils.downloader as downloader


class FakeResponse:
    def __init__(self, body=b"", status=206, headers=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers or {}
        self.closed = False
        self.read_sizes = []

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self._body.read(size)

    def close(self):
        self.closed = True


def make_downloader(path, size=10):
    result = downloader.Downloader.__new__(downloader.Downloader)
    result.id = "fixture.mkv"
    result.url = "https://media.example/fixture.mkv"
    result.file = str(path)
    result.size = size
    result.chunk_size = 1024
    result.cancel = False
    result.pause = False
    result.progress = 0
    result.file_is_busy = False
    result.file_lock = types.SimpleNamespace(has_lock=True)
    result._range_error = None
    result._range_retryable = False
    result._last_success_position = 0
    return result


class DownloaderRangeTests(unittest.TestCase):
    def test_valid_range_uses_half_open_contract_and_closes_response(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            path.write_bytes(b"ABCDEFGHIJ")
            response = FakeResponse(
                b"56789",
                headers={"Content-Range": "bytes 5-9/10", "Content-Length": "5"},
            )
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", return_value=response) as request, \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertEqual(dl.range_download(5, 10), 10)
            self.assertEqual(request.call_args.kwargs["headers"]["Range"], "bytes=5-9")
            self.assertEqual(request.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
            self.assertEqual(request.call_args.kwargs["headers"]["User-Agent"], "embyToLocalPlayer/dl")
            self.assertEqual(path.read_bytes(), b"ABCDE56789")
            self.assertTrue(response.closed)

    def test_200_full_response_never_changes_existing_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            original = b"ABCDEFGHIJ"
            path.write_bytes(original)
            response = FakeResponse(b"0123456789", status=200,
                                    headers={"Content-Length": "10"})
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", return_value=response), \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertEqual(dl.range_download(5, 10), 5)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(dl._range_retryable)
            self.assertTrue(response.closed)

    def test_invalid_content_range_preserves_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            original = b"ABCDEFGHIJ"
            path.write_bytes(original)
            response = FakeResponse(
                b"56789",
                headers={"Content-Range": "bytes 4-8/10", "Content-Length": "5"},
            )
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", return_value=response), \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertEqual(dl.range_download(5, 10), 5)
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(response.closed)

    def test_valid_first_range_replaces_old_cache_after_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            path.write_bytes(b"ABCDEFGHIJ")
            response = FakeResponse(
                b"01234",
                headers={"Content-Range": "bytes 0-4/10", "Content-Length": "5"},
            )
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", return_value=response), \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertEqual(dl.range_download(0, 5), 5)
            self.assertEqual(path.read_bytes(), b"01234\x00\x00\x00\x00\x00")
            self.assertTrue(response.closed)

    def test_short_response_retries_from_next_unwritten_byte(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            path.write_bytes(b"ABCDEFGHIJ")
            first = FakeResponse(
                b"567",
                headers={"Content-Range": "bytes 5-9/10", "Content-Length": "5"},
            )
            second = FakeResponse(
                b"89",
                headers={"Content-Range": "bytes 8-9/10", "Content-Length": "2"},
            )
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", side_effect=[first, second]) as request, \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False), \
                    mock.patch.object(downloader.time, "sleep"):
                self.assertTrue(dl.percent_download(0.5, 1))
            self.assertEqual([call.kwargs["headers"]["Range"] for call in request.call_args_list],
                             ["bytes=5-9", "bytes=8-9"])
            self.assertEqual(path.read_bytes(), b"ABCDE56789")
            self.assertTrue(first.closed)
            self.assertTrue(second.closed)
            self.assertFalse(dl.file_is_busy)

    def test_lock_or_read_only_failure_does_not_touch_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            path.write_bytes(b"ABCDEFGHIJ")
            dl = make_downloader(path)
            dl.file_lock.has_lock = False
            with mock.patch.object(downloader, "requests_urllib") as request, \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertFalse(dl.percent_download(0, 1))
            request.assert_not_called()
            self.assertEqual(path.read_bytes(), b"ABCDEFGHIJ")

    def test_tiny_preheat_does_not_claim_prefix_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.mkv"
            path.write_bytes(b"ABCDEFGHIJ")
            tail = FakeResponse(
                b"J", headers={"Content-Range": "bytes 9-9/10", "Content-Length": "1"})
            dl = make_downloader(path)
            with mock.patch.object(downloader, "requests_urllib", return_value=tail), \
                    mock.patch.object(downloader.configs.raw, "getboolean", return_value=False):
                self.assertTrue(dl.download_fist_last())
            self.assertEqual(dl.progress, 0)

    def _make_download_manager(self, cache_dir):
        manager = downloader.DownloadManager.__new__(downloader.DownloadManager)
        manager.cache_path = str(cache_dir)
        manager.tasks = {}
        manager.db = {}
        manager.db_path = str(cache_dir / '.db.json')
        manager.update_loop_lock = False
        manager.speed_limit = 0
        manager.download_semaphore = threading.Semaphore(3)
        manager.per_domain_limit = 2
        manager.domain_semaphores = {}
        return manager

    def _play_data(self):
        return {
            'stream_url': 'https://media.example/fixture.mkv',
            'fake_name': 'fixture.mkv',
            'position': 0,
            'size': 100,
        }

    def _run_download_play(self, responses, *, play=True):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp)
            manager = self._make_download_manager(cache_dir)
            data = self._play_data()
            media_ranges = []
            dispatched = []
            response_iter = iter(responses)

            def request(url, *args, **kwargs):
                if url.endswith('/dl'):
                    dispatched.append(copy.deepcopy(kwargs['_json']))
                    return None
                media_ranges.append(kwargs['headers']['Range'])
                return next(response_iter)

            with mock.patch.object(downloader, 'requests_urllib', side_effect=request), \
                    mock.patch.object(downloader.configs.raw, 'getboolean', return_value=False), \
                    mock.patch.object(downloader.time, 'sleep'):
                try:
                    manager.download_play(data, play=play)
                finally:
                    for task in manager.tasks.values():
                        task.file_lock.release_lock()
            cache_file = cache_dir / 'fixture.mkv'
            return (data, media_ranges, dispatched, cache_file,
                    cache_file.exists(), cache_file.stat().st_size if cache_file.exists() else None)

    def test_download_play_falls_back_once_after_first_range_403(self):
        data, media_ranges, dispatched, _, cache_exists, _ = self._run_download_play([
            FakeResponse(status=403),
        ])
        self.assertEqual(media_ranges, ['bytes=0-0'])
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], data['stream_url'])
        self.assertFalse(cache_exists)

    def test_download_play_falls_back_after_invalid_prefix_range(self):
        data, media_ranges, dispatched, _, _, _ = self._run_download_play([
            FakeResponse(
                b'X', headers={'Content-Range': 'bytes 1-1/100', 'Content-Length': '1'}),
        ])
        self.assertEqual(media_ranges, ['bytes=0-0'])
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], data['stream_url'])

    def test_download_play_falls_back_after_exhausted_prefix_short_reads(self):
        responses = [FakeResponse(
            b'', headers={'Content-Range': 'bytes 0-0/100', 'Content-Length': '1'})
                    for _ in range(downloader._RANGE_MAX_ATTEMPTS)]
        data, media_ranges, dispatched, _, _, _ = self._run_download_play(responses)
        self.assertEqual(media_ranges, ['bytes=0-0'] * downloader._RANGE_MAX_ATTEMPTS)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], data['stream_url'])

    def test_download_play_falls_back_after_tail_failure_without_full_download(self):
        responses = [
            FakeResponse(
                b'X', headers={'Content-Range': 'bytes 0-0/100', 'Content-Length': '1'}),
            FakeResponse(status=403),
        ]
        data, media_ranges, dispatched, _, _, _ = self._run_download_play(responses)
        self.assertEqual(media_ranges, ['bytes=0-0', 'bytes=99-99'])
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], data['stream_url'])

    def test_download_play_falls_back_after_exhausted_tail_short_reads(self):
        responses = [
            FakeResponse(
                b'X', headers={'Content-Range': 'bytes 0-0/100', 'Content-Length': '1'}),
            *[FakeResponse(
                b'', headers={'Content-Range': 'bytes 99-99/100', 'Content-Length': '1'})
              for _ in range(downloader._RANGE_MAX_ATTEMPTS)],
        ]
        data, media_ranges, dispatched, _, _, _ = self._run_download_play(responses)
        self.assertEqual(media_ranges, ['bytes=0-0'] + ['bytes=99-99'] * downloader._RANGE_MAX_ATTEMPTS)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], data['stream_url'])

    def test_download_play_uses_cache_only_after_both_ranges_succeed(self):
        responses = [
            FakeResponse(
                b'X', headers={'Content-Range': 'bytes 0-0/100', 'Content-Length': '1'}),
            FakeResponse(
                b'Y', headers={'Content-Range': 'bytes 99-99/100', 'Content-Length': '1'}),
            FakeResponse(
                b'Z' * 99,
                headers={'Content-Range': 'bytes 1-99/100', 'Content-Length': '99'}),
        ]
        data, media_ranges, dispatched, cache_file, cache_exists, cache_size = self._run_download_play(responses)
        self.assertEqual(media_ranges, ['bytes=0-0', 'bytes=99-99', 'bytes=1-99'])
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]['media_path'], str(cache_file))
        self.assertTrue(cache_exists)
        self.assertEqual(cache_size, 100)

    def test_download_play_false_never_dispatches_playback(self):
        responses = [
            FakeResponse(
                b'X', headers={'Content-Range': 'bytes 0-0/100', 'Content-Length': '1'}),
            FakeResponse(
                b'Y', headers={'Content-Range': 'bytes 99-99/100', 'Content-Length': '1'}),
            FakeResponse(
                b'Z' * 99,
                headers={'Content-Range': 'bytes 1-99/100', 'Content-Length': '99'}),
        ]
        _, media_ranges, dispatched, _, cache_exists, _ = self._run_download_play(
            responses, play=False)
        self.assertEqual(media_ranges, ['bytes=0-0', 'bytes=99-99', 'bytes=1-99'])
        self.assertEqual(dispatched, [])
        self.assertTrue(cache_exists)

    def test_download_play_preserves_cached_resume_for_positive_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp)
            manager = self._make_download_manager(cache_dir)
            data = self._play_data()
            data['media_path'] = 'https://origin.example/original.mkv?version=7'
            old_cache = b'preserved-cache'
            media_ranges = []
            dispatched = []

            def request(url, *args, **kwargs):
                if url.endswith('/dl'):
                    dispatched.append(copy.deepcopy(kwargs['_json']))
                    return None
                media_ranges.append(kwargs['headers']['Range'])
                return FakeResponse(status=403)

            with mock.patch.object(downloader.configs.raw, 'getboolean', return_value=False), \
                    mock.patch.object(downloader, 'requests_urllib', side_effect=request):
                _, _, _, dl = manager._init_dl(data)
                dl.progress = 0.25
                dl.file_is_busy = True
                Path(dl.file).write_bytes(old_cache)
                try:
                    manager.download_play(data)
                finally:
                    for task in manager.tasks.values():
                        task.file_lock.release_lock()

            self.assertEqual(media_ranges, [])
            self.assertEqual(len(dispatched), 1)
            self.assertEqual(dispatched[0]['media_path'], str(cache_dir / 'fixture.mkv'))
            self.assertEqual((cache_dir / 'fixture.mkv').read_bytes(), old_cache)

    def test_download_play_failure_modes_keep_original_media_and_cache(self):
        original_media_path = 'https://origin.example/original.mkv?version=7'
        old_cache = b'preserved-cache'
        for mode in ('read_only', 'no_lock', 'pause', 'cancel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                cache_dir = Path(temp)
                manager = self._make_download_manager(cache_dir)
                data = self._play_data()
                data['media_path'] = original_media_path
                cache_file = cache_dir / 'fixture.mkv'
                media_ranges = []
                dispatched = []

                def getboolean(section, option, fallback=False):
                    return mode == 'read_only' if (section, option) == ('gui', 'read_only') else False

                def request(url, *args, **kwargs):
                    if url.endswith('/dl'):
                        dispatched.append(copy.deepcopy(kwargs['_json']))
                        return None
                    media_ranges.append(kwargs['headers']['Range'])
                    return FakeResponse(status=403)

                with mock.patch.object(downloader.configs.raw, 'getboolean', side_effect=getboolean), \
                        mock.patch.object(downloader, 'requests_urllib', side_effect=request):
                    if mode == 'read_only':
                        cache_file.write_bytes(old_cache)
                    else:
                        _, _, _, dl = manager._init_dl(data)
                        if mode == 'no_lock':
                            dl.file_lock.release_lock()
                        dl.pause = mode == 'pause'
                        dl.cancel = mode == 'cancel'
                        cache_file.write_bytes(old_cache)
                    try:
                        manager.download_play(data)
                    finally:
                        for task in manager.tasks.values():
                            if task.file_lock.has_lock:
                                task.file_lock.release_lock()

                self.assertEqual(media_ranges, [])
                self.assertEqual(len(dispatched), 1)
                self.assertEqual(dispatched[0]['media_path'], original_media_path)
                self.assertEqual(cache_file.read_bytes(), old_cache)

    def test_resume_does_not_start_full_download_after_preheat_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            cache_dir = Path(temp)
            manager = self._make_download_manager(cache_dir)
            data = self._play_data()
            started = []
            response = FakeResponse(status=403)

            class ImmediateThread:
                def __init__(self, target, kwargs=None, **_):
                    started.append((target, kwargs or {}))

                def start(self):
                    raise AssertionError('full download must not start after preheat failure')

            with mock.patch.object(downloader, 'requests_urllib', return_value=response), \
                    mock.patch.object(downloader.configs.raw, 'getboolean', return_value=False), \
                    mock.patch.object(downloader.threading, 'Thread', ImmediateThread):
                try:
                    manager.resume_or_pause(data={'operate': 'resume', 'data_list': [data]})
                finally:
                    for task in manager.tasks.values():
                        task.file_lock.release_lock()
            self.assertEqual(started, [])
            self.assertTrue(response.closed)
            self.assertIn('fixture.mkv', manager.tasks)


if __name__ == "__main__":
    unittest.main()
