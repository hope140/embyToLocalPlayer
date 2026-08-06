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

if __name__ == '__main__': unittest.main()

