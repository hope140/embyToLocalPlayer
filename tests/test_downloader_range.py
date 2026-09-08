import io
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


if __name__ == "__main__":
    unittest.main()
