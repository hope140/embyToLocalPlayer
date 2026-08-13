import types
import threading
import unittest
from unittest import mock
from utils.clouddrive2_client import CloudDrive2Client, CloudDrive2DownloadTarget

class Req:
    def __init__(self, **kw): self.__dict__.update(kw)
class PB2:
    FindFileByPathRequest = Req
    GetDownloadUrlPathRequest = Req
class FakeStub:
    def __init__(self, *, url='/static/{SCHEME}/{HOST}/{PREVIEW}/video.mkv', directory=False,
                 direct='', expires_in=None, user_agent=None, additional_headers=None):
        self.url, self.directory, self.direct = url, directory, direct
        self.expires_in = expires_in
        self.user_agent = user_agent
        self.additional_headers = additional_headers
        self.calls=[]
    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req, kwargs))
        return types.SimpleNamespace(fullPathName=req.path, size=10, isDirectory=self.directory)
    def GetDownloadUrlPath(self, req, **kwargs):
        self.calls.append(('url', req, kwargs))
        return types.SimpleNamespace(
            downloadUrlPath=self.url,
            directUrl=self.direct,
            expiresIn=self.expires_in,
            userAgent=self.user_agent,
            additionalHeaders=self.additional_headers,
        )

def loader(): return types.SimpleNamespace(), PB2, types.SimpleNamespace()


class RefreshStub:
    def __init__(self, known_files=(), known_directories=(), refresh_results=None):
        self.known_files = set(known_files)
        self.known_directories = set(known_directories)
        self.refresh_results = dict(refresh_results or {})
        self.calls = []
        self.events = []

    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req.path, kwargs))
        self.events.append(('find', req.path))
        if req.path in self.known_files or req.path in self.known_directories:
            return types.SimpleNamespace(
                fullPathName=req.path,
                size=10,
                isDirectory=req.path in self.known_directories,
            )
        return None

    def GetSubFiles(self, req, **kwargs):
        self.calls.append(('refresh', req.path, req.forceRefresh, req.checkExpires, kwargs))
        result = self.refresh_results.get(req.path)
        if isinstance(result, BaseException):
            raise result
        if result and 'batches' in result:
            def stream():
                for batch in result['batches']:
                    self.events.append(('batch', req.path, batch))
                    yield batch
                self.known_files.update(result.get('files_after_eof', ()))
                self.known_directories.update(result.get('directories_after_eof', ()))
                self.events.append(('eof', req.path))
            return stream()
        if result:
            self.known_files.update(result.get('files', ()))
            self.known_directories.update(result.get('directories', ()))
        return iter(())

    def GetDownloadUrlPath(self, req, **kwargs):
        self.calls.append(('url', req.path, kwargs))
        return types.SimpleNamespace(downloadUrlPath='/static/video.mkv')

class LegacyDownloadRequest:
    def __init__(self, *, path, preview, lazy_read):
        self.path, self.preview, self.lazy_read = path, preview, lazy_read


class LegacyPB2:
    FindFileByPathRequest = Req
    GetDownloadUrlPathRequest = LegacyDownloadRequest


class CloudDrive2ClientTests(unittest.TestCase):
    def make(self, stub, **kw):
        return CloudDrive2Client('https://CD2.example:8443/api', 'Bearer secret',
            _stub_factory=lambda *_: stub, _proto_loader=loader, **kw)
    def test_mapping_is_case_insensitive_longest_and_boundary_safe(self):
        c = self.make(FakeStub(), path_map=[('C:/Media', '/library'), ('C:/Media/Movies', '/movies')])
        self.assertEqual(c.map_local_path_to_cloud_path(r'c:\media\movies\A.mkv'), '/movies/A.mkv')
        self.assertEqual(c.map_local_path_to_cloud_path(r'C:\MediaX\A.mkv'), None)
        self.assertIsNone(c.map_local_path_to_cloud_path(r'C:\Media\..\secret.mkv'))
    def test_resolve_uses_auth_and_expected_requests(self):
        stub = FakeStub()
        c = self.make(stub)
        self.assertEqual(c.resolve_download_url('/library/video.mkv'), 'https://cd2.example:8443/api/static/https/cd2.example:8443/false/video.mkv')
        self.assertEqual(stub.calls[0][1].parentPath, '')
        self.assertEqual(stub.calls[0][1].path, '/library/video.mkv')
        self.assertEqual(stub.calls[0][2]['metadata'], (('authorization', 'Bearer secret'),))
        self.assertFalse(stub.calls[1][1].preview)
        self.assertFalse(stub.calls[1][1].lazy_read)
        self.assertFalse(stub.calls[1][1].get_direct_url)
    def test_rejects_directory_direct_external_and_foreign_urls(self):
        self.assertIsNone(self.make(FakeStub(directory=True)).resolve_cloud_path('/x'))
        self.assertIsNone(self.make(FakeStub(direct='https://other/file')).resolve_cloud_path('/x'))
        self.assertIsNone(self.make(FakeStub(url='https://other/file')).resolve_cloud_path('/x'))
        self.assertIsNone(self.make(FakeStub(url='javascript:alert(1)')).resolve_cloud_path('/x'))

    def test_opt_in_returns_direct_target_with_metadata_and_fallback(self):
        stub = FakeStub(
            direct='https://cdn.example/video.mkv?signature=synthetic',
            expires_in=600,
            user_agent='CloudDrive2 synthetic UA',
            additional_headers={
                'Authorization': 'Bearer synthetic',
                'X-Cloud-Header': 'enabled',
                'x-cloud-header': 'last-value-wins',
                'Host': 'must-not-forward',
                'Connection': 'must-not-forward',
            },
        )
        target = self.make(stub, get_direct_url=True).resolve_cloud_path_target('/x')

        self.assertIsInstance(target, CloudDrive2DownloadTarget)
        self.assertTrue(target.is_direct)
        self.assertEqual(target.url, 'https://cdn.example/video.mkv?signature=synthetic')
        self.assertEqual(target.expires_in, 600)
        self.assertEqual(target.user_agent, 'CloudDrive2 synthetic UA')
        self.assertEqual(target.additional_headers, (
            ('Authorization', 'Bearer synthetic'),
            ('x-cloud-header', 'last-value-wins'),
        ))
        self.assertEqual(
            target.fallback_url,
            'https://cd2.example:8443/api/static/https/cd2.example:8443/false/video.mkv',
        )
        self.assertTrue(stub.calls[1][1].get_direct_url)

    def test_invalid_or_expired_direct_url_falls_back_to_download_path(self):
        for direct, expires_in in (
                ('https://cdn.example/video.mkv', 0),
                ('ftp://cdn.example/video.mkv', 600),
                ('https://user:password@cdn.example/video.mkv', 600)):
            with self.subTest(direct=direct, expires_in=expires_in):
                stub = FakeStub(direct=direct, expires_in=expires_in)
                target = self.make(stub, get_direct_url=True).resolve_cloud_path_target('/x')
                self.assertIsInstance(target, CloudDrive2DownloadTarget)
                self.assertFalse(target.is_direct)
                self.assertEqual(
                    target.url,
                    'https://cd2.example:8443/api/static/https/cd2.example:8443/false/video.mkv',
                )

    def test_direct_url_request_is_compatible_with_old_proto(self):
        stub = FakeStub()
        client = CloudDrive2Client(
            'https://cd2.example:8443/api', 'token', get_direct_url=True,
            _stub_factory=lambda *_: stub,
            _proto_loader=lambda: (types.SimpleNamespace(), LegacyPB2, types.SimpleNamespace()),
        )

        target = client.resolve_cloud_path_target('/x')
        self.assertFalse(target.is_direct)
        self.assertEqual(stub.calls[1][1].path, '/x')
        self.assertFalse(hasattr(stub.calls[1][1], 'get_direct_url'))

    def test_direct_url_failures_do_not_write_url_to_logs(self):
        direct = 'https://cdn.example/video.mkv?signature=synthetic-secret'
        logger = mock.Mock()
        target = self.make(
            FakeStub(direct=direct, expires_in=600),
            get_direct_url=True,
            logger=logger,
        ).resolve_cloud_path_target('/x')
        self.assertEqual(target.url, direct)
        self.assertNotIn(direct, repr(logger.mock_calls))
    def test_invalid_configuration_and_missing_dependency_are_soft_failures(self):
        c = CloudDrive2Client('not a url', '   ')
        self.assertIsNone(c.resolve_download_url('/x'))
        c = CloudDrive2Client('https://host', 'token', _proto_loader=lambda: (_ for _ in ()).throw(ImportError()))
        self.assertIsNone(c.resolve_download_url('/x'))

    def make_refresh_client(self, stub, **kw):
        return CloudDrive2Client('https://cd2.example:8443/api', 'token',
            _stub_factory=lambda *_: stub, _proto_loader=loader,
            request_timeout_seconds=0.2, **kw)

    def test_missing_file_consumes_parent_stream_to_eof_then_rechecks(self):
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': {
                'batches': ('first', 'second'),
                'files_after_eof': {'/library/new.mkv'},
            }},
        )
        self.assertEqual(
            self.make_refresh_client(stub).resolve_cloud_path('/library/new.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual([call[0:2] for call in stub.calls], [
            ('find', '/library/new.mkv'),
            ('find', '/library'),
            ('refresh', '/library'),
            ('find', '/library/new.mkv'),
            ('url', '/library/new.mkv'),
        ])
        refresh = next(call for call in stub.calls if call[0] == 'refresh')
        self.assertTrue(refresh[2])
        self.assertTrue(refresh[3])
        target_find = [index for index, event in enumerate(stub.events)
                       if event == ('find', '/library/new.mkv')][-1]
        eof = stub.events.index(('eof', '/library'))
        self.assertLess(eof, target_find)

    def test_missing_parent_refreshes_upper_then_parent(self):
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={
                '/library': {'directories': {'/library/new'}},
                '/library/new': {'files': {'/library/new/video.mkv'}},
            },
        )
        self.assertEqual(
            self.make_refresh_client(stub).resolve_cloud_path('/library/new/video.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual(
            [call[1] for call in stub.calls if call[0] == 'refresh'],
            ['/library', '/library/new'],
        )

    def test_unknown_upper_parent_stops_without_refresh(self):
        stub = RefreshStub()
        self.assertIsNone(
            self.make_refresh_client(stub).resolve_cloud_path('/library/new/video.mkv'))
        self.assertEqual([], [call for call in stub.calls if call[0] == 'refresh'])

    def test_refresh_cooldown_coalesces_concurrent_and_one_second_retries(self):
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': RuntimeError('refresh failed')},
        )
        now = [100.0]
        client = self.make_refresh_client(stub, _clock=lambda: now[0])
        barrier = threading.Barrier(4)
        threads = [threading.Thread(
            target=lambda: (barrier.wait(), client.resolve_cloud_path('/library/new.mkv')))
            for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        refreshes = lambda: len([call for call in stub.calls if call[0] == 'refresh'])
        self.assertEqual(1, refreshes())
        now[0] += 1.0
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        self.assertEqual(1, refreshes())
        now[0] += 4.0
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        self.assertEqual(2, refreshes())

    def test_existing_file_does_not_refresh(self):
        stub = RefreshStub(known_files={'/library/existing.mkv'})
        self.assertEqual(
            self.make_refresh_client(stub).resolve_cloud_path('/library/existing.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual([], [call for call in stub.calls if call[0] == 'refresh'])

if __name__ == '__main__': unittest.main()

