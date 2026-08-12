import types
import unittest
from utils.clouddrive2_client import CloudDrive2Client

class Req:
    def __init__(self, **kw): self.__dict__.update(kw)
class PB2:
    FindFileByPathRequest = Req
    GetDownloadUrlPathRequest = Req
class FakeStub:
    def __init__(self, *, url='/static/{SCHEME}/{HOST}/{PREVIEW}/video.mkv', directory=False, direct=''):
        self.url, self.directory, self.direct = url, directory, direct
        self.calls=[]
    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req, kwargs))
        return types.SimpleNamespace(fullPathName=req.path, size=10, isDirectory=self.directory)
    def GetDownloadUrlPath(self, req, **kwargs):
        self.calls.append(('url', req, kwargs))
        return types.SimpleNamespace(downloadUrlPath=self.url, directUrl=self.direct)

def loader(): return types.SimpleNamespace(), PB2, types.SimpleNamespace()


class RefreshStub:
    def __init__(self, known_files=(), known_directories=(), refresh_results=None):
        self.known_files = set(known_files)
        self.known_directories = set(known_directories)
        self.refresh_results = dict(refresh_results or {})
        self.calls = []

    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req.path, kwargs))
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
        if result:
            self.known_files.update(result.get('files', ()))
            self.known_directories.update(result.get('directories', ()))
        return iter(())

    def GetDownloadUrlPath(self, req, **kwargs):
        self.calls.append(('url', req.path, kwargs))
        return types.SimpleNamespace(downloadUrlPath='/static/video.mkv')

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
    def test_invalid_configuration_and_missing_dependency_are_soft_failures(self):
        c = CloudDrive2Client('not a url', '   ')
        self.assertIsNone(c.resolve_download_url('/x'))
        c = CloudDrive2Client('https://host', 'token', _proto_loader=lambda: (_ for _ in ()).throw(ImportError()))
        self.assertIsNone(c.resolve_download_url('/x'))

    def make_refresh_client(self, stub, **kw):
        return CloudDrive2Client('https://cd2.example:8443/api', 'token',
            _stub_factory=lambda *_: stub, _proto_loader=loader,
            request_timeout_seconds=0.2, **kw)

    def test_missing_file_refreshes_known_parent_then_rechecks(self):
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': {'files': {'/library/new.mkv'}}},
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

    def test_refresh_failure_is_soft_and_short_cooldown_deduplicates(self):
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': RuntimeError('refresh failed')},
        )
        client = self.make_refresh_client(stub)
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        self.assertEqual(1, len([call for call in stub.calls if call[0] == 'refresh']))

    def test_existing_file_does_not_refresh(self):
        stub = RefreshStub(known_files={'/library/existing.mkv'})
        self.assertEqual(
            self.make_refresh_client(stub).resolve_cloud_path('/library/existing.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual([], [call for call in stub.calls if call[0] == 'refresh'])

if __name__ == '__main__': unittest.main()

