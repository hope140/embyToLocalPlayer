import copy
import inspect
import unittest
from configparser import ConfigParser
from unittest import mock

import utils.data_parser as data_parser
from utils.emby_api import EmbyApi
from utils.emby_api_thin import EmbyApiThin


class EmbyMultiVersionTests(unittest.TestCase):
    @staticmethod
    def _source(source_id, path, *, item_id=None, name=None, container='mkv',
                source_type=None, size=1, runtime=3600):
        source = {
            'Id': source_id,
            'Path': path,
            'Name': name or source_id,
            'Container': container,
            'MediaStreams': [],
            'RunTimeTicks': runtime * 10 ** 7,
            'Size': size,
        }
        if item_id is not None:
            source['ItemId'] = item_id
        if source_type is not None:
            source['Type'] = source_type
        return source

    @classmethod
    def _episode(cls, item_id, path, source, *, season=1, index=1,
                 chapters=None):
        item = {
            'Id': item_id,
            'Type': 'Episode',
            'ProviderIds': {},
            'SeriesId': 'series-1',
            'SeasonId': 'season-1',
            'SeriesName': 'Show',
            'Name': item_id,
            'Path': path,
            'ParentIndexNumber': season,
            'IndexNumber': index,
            'MediaSources': [source],
        }
        if chapters is not None:
            item['Chapters'] = chapters
        return item

    @staticmethod
    def _list_data(*, source_id='source-a', item_id='item-a', path='/media/A.mkv',
                   playlist_info=None, mount=False, server='emby'):
        return {
            'server': server,
            'scheme': 'http',
            'netloc': 'emby.example',
            'api_key': 'token',
            'user_id': 'user',
            'mount_disk_mode': mount,
            'playlist_info': playlist_info or [],
            'main_ep_info': {
                'Id': item_id,
                'SeasonId': 'season-1',
                'SeriesId': 'series-1',
                'SeriesName': 'Show',
                'Name': 'Episode',
            },
            'episodes_info': [],
            'server_version': '4.10.0.40',
            'basename': path.rsplit('/', 1)[-1],
            'is_strm': path.lower().endswith('.strm'),
            'is_http_source': path.startswith('http'),
            'strm_direct': False,
            'is_http_direct_strm': False,
            'strm_local_by_file_path': False,
            'item_id': item_id,
            'media_source_id': source_id,
            'file_path': path,
            'media_title': 'Show S1:E1 - Episode',
            'sub_inner_idx': 0,
            'device_id': 'device',
            'play_session_id': 'session',
            'headers': {},
            'sub_file': None,
            'chapters': [],
        }

    def test_normalizer_is_pure_and_uses_explicit_rows_and_source_values(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A', size=10,
            runtime=10, source_type='Default')
        source_b = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B', size=20,
            runtime=20)
        source_b_explicit = copy.deepcopy(source_b)
        original = [
            self._episode('item-a', '/media/A.mkv', source_a, chapters=[{'Name': 'A'}]),
            self._episode('item-b', '/media/B-explicit.mkv', source_b_explicit,
                          chapters=[{'Name': 'B'}]),
        ]
        before = copy.deepcopy(original)

        rows = data_parser._normalize_emby_episode_items(original)

        self.assertEqual(original, before)
        self.assertEqual([row['Id'] for row in rows], ['item-a', 'item-b'])
        self.assertEqual(rows[1]['Path'], '/media/B-explicit.mkv')
        self.assertEqual(rows[1]['MediaSources'][0]['Size'], 20)
        self.assertEqual(rows[1]['Chapters'], [{'Name': 'B'}])

        missing_explicit = [copy.deepcopy(original[0])]
        missing_explicit[0]['MediaSources'] = [source_a, source_b]
        rows = data_parser._normalize_emby_episode_items(missing_explicit)
        self.assertEqual([row['Id'] for row in rows], ['item-a', 'item-b'])
        self.assertNotIn('Chapters', rows[1])
        self.assertEqual(rows[1]['MediaSources'][0]['RunTimeTicks'], 20 * 10 ** 7)

    def test_normalizer_keeps_default_source_and_skips_ambiguous_strm_hints(self):
        source_a = self._source(
            'source-a', '/remote/S01E01.mkv', item_id='item-a', name='S01E01',
            container='strm', source_type='Default')
        source_b = self._source(
            'source-b', '/remote/S01E01-1080p.mkv', item_id='item-b', name='S01E01-1080p',
            container='strm')
        item = self._episode(
            'item-a', '/library/S01E01.strm', source_a,
            chapters=[{'Name': 'default'}])
        item['MediaSources'] = [source_b, source_a]
        rows = data_parser._normalize_emby_episode_items([item])

        self.assertEqual(rows[0]['MediaSources'][0]['Id'], 'source-a')
        self.assertEqual(rows[0]['Path'], '/library/S01E01.strm')
        self.assertEqual(rows[1]['Path'], '/library/S01E01-1080p.strm')
        self.assertNotIn('Chapters', rows[1])
        self.assertTrue(rows[1]['_etlp_path_unverified'])

        ambiguous = copy.deepcopy(item)
        ambiguous['MediaSources'][0]['Name'] = 'S01E01'
        ambiguous['MediaSources'][1]['Name'] = 'S01E01'
        self.assertEqual(
            data_parser._normalize_emby_episode_items([ambiguous]),
            [data_parser._normalize_emby_episode_items([item])[0]],
        )

    def test_source_lookup_does_not_downgrade_missing_source_id_to_item_id(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        row = self._episode('item-a', '/media/A.mkv', source_a)
        rows = data_parser._normalize_emby_episode_items([row])
        self.assertIsNone(
            data_parser._find_source_row(rows, source_id='missing', item_id='item-a')
        )

    def test_mismatched_source_item_id_never_inherits_default_sidecar(self):
        for source_type in (None, 'Default'):
            with self.subTest(source_type=source_type):
                source_b = self._source(
                    'source-b', '/server/B.mkv', item_id='item-b', name=None,
                    container='strm', source_type=source_type)
                source_b['Name'] = None
                item = self._episode('item-a', '/library/A.strm', source_b,
                                     chapters=[{'Name': 'A'}])
                item['MediaSources'] = [source_b]
                self.assertEqual(
                    data_parser._normalize_emby_episode_items([item]), [])

    def test_list_episodes_flattens_missing_non_admin_rows(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default', size=10, runtime=10)
        source_b = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B',
            size=20, runtime=20)
        backend = self._episode(
            'item-a', '/media/A.mkv', source_a,
            chapters=[{'Name': 'default'}])
        backend['MediaSources'] = [source_a, source_b]
        data = self._list_data()
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}) as request, \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertIn('AlternateMediaSources', request.call_args.kwargs['params']['Fields'])
        self.assertEqual([row['item_id'] for row in rows], ['item-a', 'item-b'])
        self.assertEqual(rows[1]['media_source_id'], 'source-b')
        self.assertEqual(rows[1]['size'], 20)
        self.assertEqual(rows[1]['total_sec'], 20)
        self.assertEqual(rows[1]['chapters'], [])

    def test_list_episodes_uses_per_item_mount_mode_for_http_alternate(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        source_b = self._source(
            'source-b', 'https://media.example/B.mkv', item_id='item-b', name='B')
        backend = self._episode('item-a', '/media/A.mkv', source_a)
        backend['MediaSources'] = [source_a, source_b]
        data = self._list_data(mount=True)
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser, 'translate_path_by_ini',
                                  side_effect=lambda value: f'LOCAL:{value}') as translate, \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual([row['media_source_id'] for row in rows],
                         ['source-a', 'source-b'])
        self.assertTrue(rows[0]['mount_disk_mode'])
        self.assertEqual(rows[0]['media_path'], 'LOCAL:/media/A.mkv')
        self.assertFalse(rows[1]['mount_disk_mode'])
        self.assertEqual(rows[1]['media_path'], rows[1]['stream_url'])
        self.assertEqual(translate.call_args_list,
                         [mock.call('/media/A.mkv')])

    def test_list_preserves_startup_basename_for_exact_current_source(self):
        source_b = self._source(
            'source-b', '/server/S01E01.mkv', item_id='item-b',
            name='S01E01-B', container='strm')
        exact_b = self._episode(
            'item-b', '/library/S01E01-B-exact.strm', source_b, index=1)
        data = self._list_data(
            source_id='source-b', item_id='item-a',
            path='/library/S01E01-B-hint.strm')
        data.update({
            'basename': 'S01E01-B-hint.strm',
            'file_path': '/library/S01E01-B-hint.strm',
            'media_title': 'Show S1:E1 - S01E01-B-hint.strm',
            'is_strm': True,
            'strm_local_by_file_path': False,
            'main_ep_info': {
                'Id': 'item-a', 'SeasonId': 'season-1',
                'SeriesId': 'series-1', 'SeriesName': 'Show',
                'Name': 'Episode',
            },
        })
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [exact_b]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['media_source_id'], 'source-b')
        self.assertEqual(rows[0]['file_path'], '/library/S01E01-B-exact.strm')
        self.assertEqual(rows[0]['basename'], 'S01E01-B-hint.strm')
        self.assertEqual(rows[0]['media_title'], data['media_title'])

    def test_list_does_not_inherit_first_http_direct_flag_to_unverified_item(self):
        source_a = self._source(
            'source-a', 'https://media.example/S01E01.mkv', item_id='item-a',
            name='S01E01', container='strm', source_type='Default')
        source_b = self._source(
            'source-b', '/server/S01E01-B.mkv', item_id='item-b',
            name='S01E01-B', container='strm')
        backend = self._episode('item-a', '/library/S01E01.strm', source_a)
        backend['MediaSources'] = [source_a, source_b]
        data = self._list_data(
            source_id='source-a', item_id='item-a',
            path='/library/S01E01.strm', mount=True)
        data.update({
            'is_strm': True,
            'is_http_source': True,
            'strm_direct': True,
            'is_http_direct_strm': True,
            'strm_local_by_file_path': False,
        })
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual([row['media_source_id'] for row in rows],
                         ['source-a', 'source-b'])
        self.assertEqual(rows[0]['stream_url'], source_a['Path'])
        self.assertEqual(rows[0]['media_path'], source_a['Path'])
        self.assertFalse(rows[1]['is_http_direct_strm'])
        self.assertFalse(rows[1]['mount_disk_mode'])
        self.assertEqual(rows[1]['media_path'], rows[1]['stream_url'])
        self.assertNotEqual(rows[1]['media_path'], source_b['Path'])

    def test_playlist_projection_keeps_count_and_selected_source(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        source_b = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B')
        backend = self._episode('item-a', '/media/A.mkv', source_a)
        backend['MediaSources'] = [source_a, source_b]
        playlist = [{'Id': 'item-a'}, {'Id': 'item-b'}]
        data = self._list_data(playlist_info=playlist)
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row['item_id'] for row in rows], ['item-a', 'item-b'])
        self.assertEqual([row['media_source_id'] for row in rows],
                         ['source-a', 'source-b'])

    def test_version_filter_uses_media_source_and_keeps_non_contiguous_next_episode(self):
        a1 = self._episode(
            'item-a1', '/media/A1.mkv',
            self._source('source-a1', '/media/A1.mkv', item_id='item-a1', name='A1'),
            index=1)
        b1 = self._episode(
            'item-b1', '/media/B1.mkv',
            self._source('source-b1', '/media/B1.mkv', item_id='item-b1', name='B1'),
            index=1)
        a2 = self._episode(
            'item-a2', '/media/A2.mkv',
            self._source('source-a2', '/media/A2.mkv', item_id='item-a2', name='A2'),
            index=2)
        data = self._list_data(source_id='source-b1', item_id='item-b1',
                               path='/media/B1.mkv')
        data['main_ep_info']['Id'] = 'item-b1'
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [a1, b1, a2]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get',
                                  side_effect=lambda section, key, fallback=None:
                                  'B1' if key == 'version_filter' else fallback), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual([row['item_id'] for row in rows], ['item-b1', 'item-a2'])

    def test_parse_received_data_uses_unverified_strm_hint_without_local_or_chapters(self):
        source_a = self._source(
            'source-a', '/server/S01E01.mkv', item_id='item-a', name='S01E01',
            container='strm', source_type='Default')
        source_b = self._source(
            'source-b', '/server/S01E01-1080p.mkv', item_id='item-b',
            name='S01E01-1080p', container='strm', size=20, runtime=20)
        received = {
            'extraData': {
                'mainEpInfo': {
                    'Id': 'item-a', 'Path': '/library/S01E01.strm',
                    'Type': 'Episode', 'Name': 'Episode',
                    'MediaSources': [source_a],
                },
                'episodesInfo': [],
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_a, source_b],
            },
            'mountDiskEnable': 'true',
        }
        raw = ConfigParser()
        raw.read_dict({'dev': {'strm_local_by_file_path': 'yes'}})
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Episode'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'force_disk_mode_by_path', return_value=True), \
                mock.patch.object(data_parser, 'translate_path_by_ini', side_effect=lambda value: value), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url') as register, \
                mock.patch.object(data_parser.configs, 'raw', raw), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False):
            result = data_parser.parse_received_data_emby(received)

        self.assertEqual(result['media_source_id'], 'source-b')
        self.assertEqual(result['file_path'], '/library/S01E01-1080p.strm')
        self.assertFalse(result['strm_local_path_resolved'])
        self.assertTrue(result['_path_unverified'])
        self.assertFalse(result['use_strm_local_path'])
        self.assertFalse(result['use_strm_cd2_url'])
        self.assertFalse(result['mount_disk_mode'])
        self.assertEqual(result['chapters'], [])
        register.assert_not_called()

    def test_list_falls_back_to_current_stream_when_alternate_identity_is_ambiguous(self):
        source_a = self._source(
            'source-a', '/server/S01E01.mkv', name='A', container='strm',
            source_type='Default')
        source_b = self._source(
            'source-b', '/server/other-version.mkv', name='B', container='strm')
        backend = self._episode('item-a', '/library/S01E01.strm', source_a)
        backend['Chapters'] = [{'MarkerType': 'IntroStart', 'StartPositionTicks': 0}]
        backend['MediaSources'] = [source_a, source_b]
        data = self._list_data(
            source_id='source-b', item_id='item-a',
            path='/library/S01E01.strm')
        data['is_strm'] = True
        data['media_path'] = 'http://emby.example/stream-b'
        data['stream_url'] = 'http://emby.example/stream-b'
        data['chapters'] = []
        original_data = copy.deepcopy(data)
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['media_source_id'], 'source-b')
        self.assertEqual(rows[0]['stream_url'], 'http://emby.example/stream-b')
        self.assertEqual(rows[0]['chapters'], [])
        self.assertEqual(data, original_data)

    def test_list_handles_missing_items_empty_sources_paths_and_runtime(self):
        valid_source = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B',
            runtime=0)
        valid_source['RunTimeTicks'] = None
        cases = [
            ('missing-items', {}),
            ('none-items', {'Items': None}),
            ('empty-items', {'Items': []}),
            ('empty-source', {'Items': [{'Id': 'item-b', 'MediaSources': []}]}),
            ('missing-path', {'Items': [{
                'Id': 'item-b',
                'Path': '/media/B.mkv',
                'MediaSources': [{
                    'Id': 'source-b', 'Path': None, 'MediaStreams': [],
                    'RunTimeTicks': 10 ** 7,
                }],
            }]}),
            ('unknown-runtime', {'Items': [{
                'Id': 'item-b', 'Path': '/media/B.mkv',
                'Type': 'Episode', 'ProviderIds': {},
                'SeriesId': 'series-1', 'ParentIndexNumber': 1,
                'IndexNumber': 1, 'Name': 'B',
                'MediaSources': [valid_source],
            }]}),
        ]
        for label, response in cases:
            with self.subTest(label=label):
                data = self._list_data(
                    source_id='source-b', item_id='item-b', path='/media/B.mkv')
                data['stream_url'] = 'http://emby.example/current-b'
                data['media_path'] = data['stream_url']
                with mock.patch.object(data_parser, 'requests_urllib',
                                       return_value=response), \
                        mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                        mock.patch.object(data_parser, 'debug_beep_win32'), \
                        mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                        mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                        mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                        mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
                    rows = data_parser.list_episodes(data)

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['media_source_id'], 'source-b')
                if label == 'unknown-runtime':
                    self.assertEqual(rows[0]['total_sec'], 3600 * 24)
                else:
                    self.assertEqual(rows[0]['stream_url'], data['stream_url'])

    def test_parse_received_data_uses_exact_strm_sidecar_and_chapters(self):
        source_b = self._source(
            'source-b', '/server/S01E01-1080p.mkv', item_id='item-b',
            name='S01E01-1080p', container='strm')
        exact = self._episode(
            'item-b', '/library/S01E01-1080p.strm', source_b,
            chapters=[{'MarkerType': 'IntroStart', 'StartPositionTicks': 0}])
        received = {
            'extraData': {
                'mainEpInfo': {
                    'Id': 'item-a', 'Path': '/library/S01E01.strm',
                    'Type': 'Episode', 'Name': 'Episode',
                },
                'episodesInfo': [exact],
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_b],
            },
            'mountDiskEnable': 'false',
        }
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Episode'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser.configs, 'raw') as raw, \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False):
            raw.getboolean.return_value = False
            result = data_parser.parse_received_data_emby(received)

        self.assertEqual(result['file_path'], '/library/S01E01-1080p.strm')
        self.assertTrue(result['strm_local_path_resolved'])
        self.assertFalse(result['_path_unverified'])
        self.assertEqual(result['chapters'], [
            {'title': 'Opening', 'time': 0.0},
        ])

    def test_parse_unverified_http_strm_does_not_bypass_direct_or_redirect(self):
        source_a = self._source(
            'source-a', '/server/S01E01.mkv', item_id='item-a', name='S01E01',
            container='strm', source_type='Default')
        source_b = self._source(
            'source-b', 'https://media.example/S01E01-1080p.mkv',
            item_id='item-b', name='S01E01-1080p', container='strm')
        received = {
            'extraData': {
                'mainEpInfo': {
                    'Id': 'item-a', 'Path': '/library/S01E01.strm',
                    'Type': 'Episode', 'Name': 'Episode',
                },
                'episodesInfo': [],
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_a, source_b],
            },
            'mountDiskEnable': 'true',
        }
        raw = ConfigParser()
        raw.read_dict({'dev': {'strm_local_by_file_path': 'yes'}})

        def check_str_match(*args, **kwargs):
            return len(args) >= 3 and args[2] == 'strm_direct_host'

        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Episode'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'force_disk_mode_by_path', return_value=True), \
                mock.patch.object(data_parser, 'translate_path_by_ini') as translate, \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url') as register, \
                mock.patch.object(data_parser.configs, 'raw', raw), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', side_effect=check_str_match), \
                mock.patch.object(data_parser.configs, 'string_replace_by_ini_pair',
                                  side_effect=lambda value, *args: value.replace(
                                      'http://emby.example', 'http://proxy.example')):
            result = data_parser.parse_received_data_emby(received)

        self.assertFalse(result['strm_local_path_resolved'])
        self.assertFalse(result['is_http_direct_strm'])
        self.assertFalse(result['mount_disk_mode'])
        self.assertIn('MediaSourceId=source-b', result['stream_url'])
        self.assertTrue(result['stream_url'].startswith('http://proxy.example'))
        self.assertEqual(result['media_path'], result['stream_url'])
        translate.assert_not_called()
        register.assert_not_called()

    def test_parse_received_data_keeps_mismatched_source_on_server_stream(self):
        source_b = self._source(
            'source-b', '/server/B.mkv', item_id='item-b', name=None,
            container='strm')
        source_b['Name'] = None
        source_b['Type'] = 'Default'
        received = {
            'extraData': {
                'mainEpInfo': {
                    'Id': 'item-a', 'Path': '/library/A.strm',
                    'Type': 'Episode', 'Name': 'Episode',
                    'Chapters': [
                        {'MarkerType': 'IntroStart', 'StartPositionTicks': 0},
                    ],
                },
                'episodesInfo': [],
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_b],
            },
            'mountDiskEnable': 'true',
        }
        raw = ConfigParser()
        raw.read_dict({'dev': {'strm_local_by_file_path': 'yes'}})
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Episode'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'force_disk_mode_by_path', return_value=True), \
                mock.patch.object(data_parser, 'translate_path_by_ini', side_effect=lambda value: value), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url') as register, \
                mock.patch.object(data_parser.configs, 'raw', raw), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False):
            result = data_parser.parse_received_data_emby(received)

        self.assertEqual(result['media_source_id'], 'source-b')
        self.assertFalse(result['strm_local_path_resolved'])
        self.assertFalse(result['mount_disk_mode'])
        self.assertFalse(result['use_strm_local_path'])
        self.assertFalse(result['use_strm_cd2_url'])
        self.assertEqual(result['chapters'], [])
        self.assertIn('MediaSourceId=source-b', result['stream_url'])
        self.assertEqual(result['media_path'], result['stream_url'])
        register.assert_not_called()

    def test_movie_selected_source_without_item_id_clears_default_chapters(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        source_b = self._source('source-b', '/media/B.mkv', name='B')
        received = {
            'extraData': {
                'mainEpInfo': {
                    'Id': 'item-a', 'Path': '/media/A.mkv',
                    'Type': 'Movie', 'Name': 'Movie',
                    'Chapters': [
                        {'MarkerType': 'IntroStart', 'StartPositionTicks': 0},
                    ],
                },
                'episodesInfo': [],
            },
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_a, source_b],
            },
            'mountDiskEnable': 'false',
        }
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Movie'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser.configs, 'raw') as raw, \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False):
            raw.getboolean.return_value = False
            result = data_parser.parse_received_data_emby(received)

        self.assertTrue(result['_selected_source_differs'])
        self.assertEqual(result['media_source_id'], 'source-b')
        self.assertEqual(result['chapters'], [])
        self.assertFalse(result['mount_disk_mode'])

    def test_movie_parse_then_list_keeps_selected_version_chapters_empty(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        source_b = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B')
        main_info = {
            'Id': 'item-a', 'Path': '/media/A.mkv', 'Type': 'Movie',
            'Name': 'Movie', 'MediaSources': [source_a, source_b],
            'Chapters': [
                {'MarkerType': 'IntroStart', 'StartPositionTicks': 0},
            ],
        }
        received = {
            'extraData': {'mainEpInfo': main_info, 'episodesInfo': []},
            'ApiClient': {
                '_serverAddress': 'http://emby.example',
                '_serverVersion': '4.10.0.40',
            },
            'playbackUrl': (
                'http://emby.example/emby/Items/item-a/stream.mkv?'
                'X-Emby-Token=token&X-Emby-Device-Id=device&'
                'MediaSourceId=source-b&StartTimeTicks=0&UserId=user'
            ),
            'request': {'headers': {}},
            'playbackData': {
                'PlaySessionId': 'session',
                'MediaSources': [source_a, source_b],
            },
            'mountDiskEnable': 'false',
        }
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'main_ep_to_title', return_value='Movie'), \
                mock.patch.object(data_parser, 'main_ep_intro_time', return_value={}), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser.configs, 'raw') as raw, \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False):
            raw.getboolean.return_value = False
            parsed = data_parser.parse_received_data_emby(received)
            listed = data_parser.list_episodes(parsed)

        self.assertEqual(parsed['media_source_id'], 'source-b')
        self.assertEqual(parsed['chapters'], [])
        self.assertTrue(parsed['_selected_source_differs'])
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['media_source_id'], 'source-b')
        self.assertEqual(listed[0]['chapters'], [])

    def test_jellyfin_list_does_not_expand_media_sources(self):
        source_a = self._source(
            'source-a', '/media/A.mkv', item_id='item-a', name='A',
            source_type='Default')
        source_b = self._source(
            'source-b', '/media/B.mkv', item_id='item-b', name='B')
        backend = self._episode('item-a', '/media/A.mkv', source_a)
        backend['MediaSources'] = [source_a, source_b]
        data = self._list_data(server='jellyfin')
        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': [backend]}), \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser, 'debug_beep_win32'), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            rows = data_parser.list_episodes(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['media_source_id'], 'source-a')

    def test_emby_api_appends_alternate_media_sources_without_new_argument(self):
        signature = inspect.signature(EmbyApi.get_episodes)
        self.assertNotIn('flat_multi_ver', signature.parameters)
        api = EmbyApi('http://emby.example', 'token', 'user')
        with mock.patch.object(api, 'get', return_value={'Items': []}) as request:
            api.get_episodes('series', season_id='season', get_sources=True)
        fields = request.call_args.kwargs['params']['Fields']
        self.assertIn('MediaSources', fields)
        self.assertIn('AlternateMediaSources', fields)

    def test_emby_api_uses_etlp_identity_headers_for_requests(self):
        api = EmbyApi('http://emby.example', 'token', 'user')
        response = mock.Mock()
        response.json.return_value = {'Items': []}
        with mock.patch.object(api.req, 'get', return_value=response) as request:
            self.assertEqual(api.get('Items/item'), {'Items': []})

        request.assert_called_once_with(
            'http://emby.example/emby/Items/item',
            params={'X-Emby-Token': 'token'},
        )
        self.assertEqual(api.req.headers['User-Agent'], 'embyToLocalPlayer/1.1')
        self.assertEqual(
            api.req.headers['X-Emby-Authorization'],
            'MediaBrowser Client="embyToLocalPlayer",Token="token"',
        )
        self.assertEqual(
            api.req.headers['Authorization'],
            'MediaBrowser Client="embyToLocalPlayer",Token="token"',
        )
        self.assertEqual(api.req.headers['Referer'], 'http://emby.example/web/index.html')

    def test_emby_api_thin_uses_matching_client_identity_in_query_and_headers(self):
        api = EmbyApiThin(host='http://emby.example', api_key='token', user_id='user')
        with mock.patch.object(api, 'req', return_value={'Items': []}) as request:
            self.assertEqual(api.get('Items/item', params={'Limit': 1}), {'Items': []})

        call = request.call_args
        self.assertEqual(call.args[0], 'http://emby.example/emby/Items/item')
        self.assertEqual(call.kwargs['params']['Limit'], 1)
        self.assertEqual(call.kwargs['params']['X-Emby-Token'], 'token')
        self.assertEqual(
            call.kwargs['params']['Authorization'],
            'MediaBrowser Client="embyToLocalPlayer",Token="token"',
        )
        self.assertEqual(
            call.kwargs['headers']['X-Emby-Authorization'],
            'MediaBrowser Client="embyToLocalPlayer",Token="token"',
        )
        self.assertEqual(
            call.kwargs['headers']['Authorization'],
            'MediaBrowser Client="embyToLocalPlayer",Token="token"',
        )
        self.assertEqual(call.kwargs['headers']['Referer'], 'http://emby.example/web/index.html')


if __name__ == '__main__':
    unittest.main()
