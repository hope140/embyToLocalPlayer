import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from utils import net_tools


class FakeResponse:
    def __init__(self, body=b'', status=200, headers=None):
        self._body = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers or {}
        self.closed = False

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        self.closed = True


class SubtitleCacheTests(unittest.TestCase):
    url = 'https://emby.example/emby/videos/item/subtitles/0/stream.srt?X-Emby-Token=secret'

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cwd_patch = mock.patch.object(net_tools.configs, 'cwd', self.temp_dir.name)
        self.cwd_patch.start()
        self.addCleanup(self.cwd_patch.stop)
        self.cleaned_patch = mock.patch.object(net_tools, 'subtitle_cache_cleaned', False)
        self.cleaned_patch.start()
        self.addCleanup(self.cleaned_patch.stop)

    def run_cache(self, responses):
        calls = []

        def fake_request(url, **kwargs):
            calls.append((url, kwargs))
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        request_patch = mock.patch.object(net_tools, 'requests_urllib', side_effect=fake_request)
        request_patch.start()
        self.addCleanup(request_patch.stop)
        return calls

    def cache_files(self):
        return list((Path(self.temp_dir.name) / '.tmp' / 'subtitles').glob('*'))

    def test_etag_304_keeps_cached_body_and_does_not_write_token_to_metadata(self):
        calls = self.run_cache([
            FakeResponse(b'old subtitle', headers={
                'ETag': '"v1"',
                'Last-Modified': 'Wed, 12 Aug 2026 04:00:00 GMT',
            }),
            FakeResponse(status=304),
        ])

        first_path = Path(net_tools.cache_sub_file(self.url))
        second_path = Path(net_tools.cache_sub_file(self.url))

        self.assertEqual(first_path, second_path)
        self.assertEqual(first_path.read_bytes(), b'old subtitle')
        self.assertEqual(calls[1][1]['headers'], {
            'If-None-Match': '"v1"',
            'If-Modified-Since': 'Wed, 12 Aug 2026 04:00:00 GMT',
        })
        metadata_path = next(path for path in self.cache_files() if path.name.endswith('.meta.json'))
        metadata_text = metadata_path.read_text(encoding='utf-8')
        self.assertNotIn('secret', metadata_text)
        self.assertNotIn(self.url, metadata_text)
        self.assertTrue(calls[1][1]['res_only'])

    def test_validator_200_replaces_changed_body(self):
        self.run_cache([
            FakeResponse(b'old subtitle', headers={'ETag': '"v1"'}),
            FakeResponse(b'new subtitle', headers={'ETag': '"v2"'}),
        ])

        path = Path(net_tools.cache_sub_file(self.url))
        self.assertEqual(path.read_bytes(), b'old subtitle')
        self.assertEqual(Path(net_tools.cache_sub_file(self.url)).read_bytes(), b'new subtitle')

    def test_missing_validator_still_rechecks_same_url_content(self):
        self.run_cache([
            FakeResponse(b'old subtitle'),
            FakeResponse(b'new subtitle'),
        ])

        path = Path(net_tools.cache_sub_file(self.url))
        self.assertEqual(path.read_bytes(), b'old subtitle')
        self.assertEqual(Path(net_tools.cache_sub_file(self.url)).read_bytes(), b'new subtitle')

    def test_invalid_sidecar_forces_full_download(self):
        self.run_cache([
            FakeResponse(b'old subtitle', headers={'ETag': '"v1"'}),
            FakeResponse(b'new subtitle', headers={'ETag': '"v2"'}),
        ])

        path = Path(net_tools.cache_sub_file(self.url))
        metadata_path = next(path for path in self.cache_files() if path.name.endswith('.meta.json'))
        metadata_path.write_text('{not-json', encoding='utf-8')
        self.assertEqual(Path(net_tools.cache_sub_file(self.url)).read_bytes(), b'new subtitle')

    def test_download_failure_keeps_old_body_and_falls_back_to_http(self):
        calls = self.run_cache([
            FakeResponse(b'old subtitle', headers={'ETag': '"v1"'}),
            ConnectionError('offline'),
        ])

        path = Path(net_tools.cache_sub_file(self.url))
        result = net_tools.cache_sub_file(self.url)

        self.assertEqual(result, self.url)
        self.assertEqual(path.read_bytes(), b'old subtitle')
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(path.name.endswith('.part') for path in self.cache_files()))

    @mock.patch.object(net_tools.urllib.request, 'urlopen')
    def test_requests_urllib_exposes_304_for_response_only_call(self, urlopen):
        error = urllib.error.HTTPError(
            self.url, 304, 'Not Modified', {}, io.BytesIO())
        urlopen.side_effect = error

        response = net_tools.requests_urllib(self.url, res_only=True, retry=1)

        self.assertIs(response, error)


if __name__ == '__main__':
    unittest.main()
