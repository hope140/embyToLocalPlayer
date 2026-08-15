import types
import threading
import unittest
from unittest import mock
import utils.clouddrive2_client as client_module
from utils.clouddrive2_client import CloudDrive2Client

class Req:
    def __init__(self, **kw): self.__dict__.update(kw)
class PB2:
    FindFileByPathRequest = Req
    GetDownloadUrlPathRequest = Req
class FakeStub:
    def __init__(self, *, url='/static/{SCHEME}/{HOST}/{PREVIEW}/video.mkv', directory=False,
                 direct='', url_error=None):
        self.url, self.directory, self.direct, self.url_error = (
            url, directory, direct, url_error)
        self.calls=[]
    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req, kwargs))
        return types.SimpleNamespace(fullPathName=req.path, size=10, isDirectory=self.directory)
    def GetDownloadUrlPath(self, req, **kwargs):
        self.calls.append(('url', req, kwargs))
        if self.url_error:
            raise self.url_error
        return types.SimpleNamespace(downloadUrlPath=self.url, directUrl=self.direct)

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


class ErrorLookupStub(RefreshStub):
    def FindFileByPath(self, req, **kwargs):
        self.calls.append(('find', req.path, kwargs))
        raise RuntimeError('lookup failed')


class CaptureLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(str(message))

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

    def test_logs_lookup_and_download_url_success_without_path_or_url(self):
        logger = CaptureLogger()
        stub = FakeStub()
        self.assertIsNotNone(self.make(stub, logger=logger).resolve_cloud_path(
            '/library/video.mkv'))
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 lookup state=found', messages)
        self.assertIn('cd2 download_url status=success', messages)
        self.assertNotIn('/library/video.mkv', messages)
        self.assertNotIn('https://', messages)

    def test_uses_default_logger_when_caller_does_not_supply_one(self):
        logger = CaptureLogger()
        with mock.patch.object(client_module, '_get_default_logger', return_value=logger):
            self.assertIsNotNone(self.make(FakeStub()).resolve_cloud_path(
                '/library/video.mkv'))
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 lookup state=found', messages)
        self.assertIn('cd2 download_url status=success', messages)

    def test_lookup_error_does_not_refresh_and_is_visible(self):
        logger = CaptureLogger()
        stub = ErrorLookupStub()
        self.assertIsNone(self.make_refresh_client(stub, logger=logger).resolve_cloud_path(
            '/library/video.mkv'))
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 lookup state=error', messages)
        self.assertNotIn('cd2 refresh stage=', messages)
        self.assertEqual([], [call for call in stub.calls if call[0] == 'refresh'])

    def test_logs_invalid_download_url(self):
        logger = CaptureLogger()
        client = self.make(FakeStub(url='https://other/file'), logger=logger)
        self.assertIsNone(client.resolve_cloud_path('/library/video.mkv'))
        self.assertIn('cd2 download_url status=invalid', '\n'.join(logger.messages))

    def test_logs_download_url_error(self):
        logger = CaptureLogger()
        client = self.make(FakeStub(url_error=RuntimeError('url failed')), logger=logger)
        self.assertIsNone(client.resolve_cloud_path('/library/video.mkv'))
        self.assertIn('cd2 download_url status=error', '\n'.join(logger.messages))
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

    def test_missing_file_consumes_parent_stream_to_eof_then_rechecks(self):
        logger = CaptureLogger()
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': {
                'batches': ('first', 'second'),
                'files_after_eof': {'/library/new.mkv'},
            }},
        )
        self.assertEqual(
            self.make_refresh_client(stub, logger=logger).resolve_cloud_path('/library/new.mkv'),
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
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 lookup state=missing', messages)
        self.assertIn('cd2 refresh stage=direct status=started', messages)
        self.assertIn('cd2 refresh stage=direct status=success', messages)
        self.assertIn('cd2 recheck state=found', messages)
        self.assertIn('cd2 download_url status=success', messages)
        self.assertNotIn('/library/new.mkv', messages)

    def test_missing_parent_refreshes_upper_then_parent(self):
        logger = CaptureLogger()
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={
                '/library': {'directories': {'/library/new'}},
                '/library/new': {'files': {'/library/new/video.mkv'}},
            },
        )
        self.assertEqual(
            self.make_refresh_client(stub, logger=logger).resolve_cloud_path('/library/new/video.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual(
            [call[1] for call in stub.calls if call[0] == 'refresh'],
            ['/library', '/library/new'],
        )
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 refresh stage=direct status=failed reason=parent_missing', messages)
        self.assertIn('cd2 refresh stage=upper status=started', messages)
        self.assertIn('cd2 refresh stage=upper status=success', messages)
        self.assertIn('cd2 refresh stage=direct status=started', messages)
        self.assertIn('cd2 refresh stage=direct status=success', messages)

    def test_missing_season_and_show_refreshes_category_downward(self):
        logger = CaptureLogger()
        category = '/115open/115/欧美剧'
        show = category + '/奇迹人 (2026) [tmdb=198178]'
        season = show + '/Season 01'
        target = season + '/奇迹人.2026.S01E01.2160p.WEB-DL.HDR10.H265.mkv'
        stub = RefreshStub(
            known_directories={category},
            refresh_results={
                category: {'directories': {show}},
                show: {'directories': {season}},
                season: {'files': {target}},
            },
        )

        self.assertEqual(
            self.make_refresh_client(stub, logger=logger).resolve_cloud_path(target),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual(
            [call[1] for call in stub.calls if call[0] == 'refresh'],
            [category, show, season],
        )
        messages = '\n'.join(logger.messages)
        self.assertIn('cd2 refresh stage=category status=started', messages)
        self.assertIn('cd2 refresh stage=show status=started', messages)
        self.assertIn('cd2 refresh stage=season status=started', messages)
        self.assertIn('cd2 recheck state=found', messages)
        self.assertIn('cd2 download_url status=success', messages)
        self.assertNotIn(category, messages)

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

    def test_refresh_target_cooldown_is_visible(self):
        logger = CaptureLogger()
        stub = RefreshStub(
            known_directories={'/library'},
            refresh_results={'/library': RuntimeError('refresh failed')},
        )
        now = [100.0]
        client = self.make_refresh_client(stub, _clock=lambda: now[0], logger=logger)
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        logger.messages.clear()
        now[0] += 1.0
        self.assertIsNone(client.resolve_cloud_path('/library/new.mkv'))
        self.assertIn('cd2 refresh stage=target status=cooldown', '\n'.join(logger.messages))

    def test_existing_file_does_not_refresh(self):
        stub = RefreshStub(known_files={'/library/existing.mkv'})
        self.assertEqual(
            self.make_refresh_client(stub).resolve_cloud_path('/library/existing.mkv'),
            'https://cd2.example:8443/api/static/video.mkv',
        )
        self.assertEqual([], [call for call in stub.calls if call[0] == 'refresh'])

if __name__ == '__main__': unittest.main()

