import copy
import math
import unittest
from unittest import mock

import utils.data_parser as data_parser
from utils.tools import main_ep_chapters, main_ep_intro_time


class ChapterNormalizationTests(unittest.TestCase):
    def test_normalizer_keeps_all_valid_entries_and_fractional_seconds(self):
        item = {
            'Chapters': [
                {'Name': 'Late chapter', 'StartPositionTicks': 25000000},
                {'MarkerType': 'IntroStart', 'StartPositionTicks': 0},
                {'MarkerType': 'IntroEnd', 'StartPositionTicks': 1000000000},
                {'Name': 'Round chapter', 'StartPositionTicks': 1000000000},
                {'MarkerType': 'CreditsStart', 'StartPositionTicks': 3000000000},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 100000000},
                {'Name': {'malformed': True}, 'StartPositionTicks': 4000000000},
                {'StartPositionTicks': -1},
                {'StartPositionTicks': float('nan')},
                {'StartPositionTicks': True},
                None,
            ],
        }
        original = copy.deepcopy(item)

        chapters = main_ep_chapters(item)

        self.assertEqual(
            chapters,
            [
                {'title': 'Opening', 'time': 0.0},
                {'title': 'Late chapter', 'time': 2.5},
                {'title': 'Chapter 3', 'time': 10.0},
                {'title': 'Main', 'time': 100.0},
                {'title': 'Round chapter', 'time': 100.0},
                {'title': 'Credits', 'time': 300.0},
                {'title': 'Chapter 7', 'time': 400.0},
            ],
        )
        self.assertEqual(item, original)
        self.assertTrue(all(math.isfinite(chapter['time']) for chapter in chapters))

    def test_intro_uses_explicit_pair_including_zero_and_late_markers(self):
        item = {
            'Chapters': [
                {'MarkerType': 'Chapter', 'StartPositionTicks': 0},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 100000000},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 200000000},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 300000000},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 400000000},
                {'MarkerType': 'IntroStart', 'StartPositionTicks': 0},
                {'MarkerType': 'Chapter', 'StartPositionTicks': 1000000000},
                {'MarkerType': 'IntroEnd', 'StartPositionTicks': 1000000000},
            ],
        }

        self.assertEqual(
            main_ep_intro_time(item),
            {'intro_start': 0.0, 'intro_end': 100.0},
        )

    def test_intro_rejects_incomplete_or_non_increasing_pairs(self):
        self.assertEqual(
            main_ep_intro_time({'Chapters': [
                {'MarkerType': 'IntroStart', 'StartPositionTicks': 10000000},
                {'MarkerType': 'IntroEnd', 'StartPositionTicks': 10000000},
            ]}),
            {},
        )
        self.assertEqual(
            main_ep_intro_time({'Chapters': [
                {'MarkerType': 'IntroStart', 'StartPositionTicks': 10000000},
            ]}),
            {},
        )


class DataParserChapterTests(unittest.TestCase):
    @staticmethod
    def _source(source_id, path):
        return {
            'Id': source_id,
            'Path': path,
            'MediaStreams': [],
            'RunTimeTicks': 3600 * 10 ** 7,
            'Size': 1,
        }

    @staticmethod
    def _ticks(marker, seconds):
        return {'MarkerType': marker, 'StartPositionTicks': seconds * 10 ** 7}

    def test_episode_rows_use_backend_chapters_or_exact_id_fallback(self):
        backend_items = [
            {
                'Id': 'ep-1', 'Type': 'Episode', 'ProviderIds': {},
                'SeriesId': 'series', 'Path': '/media/ep-1.mkv',
                'ParentIndexNumber': 1, 'IndexNumber': 1, 'Name': 'One',
                'RunTimeTicks': 3600 * 10 ** 7,
                'MediaSources': [self._source('source-1', '/media/ep-1.mkv')],
                'Chapters': [
                    self._ticks('IntroStart', 0),
                    self._ticks('IntroEnd', 100),
                    {'Name': 'Middle', 'StartPositionTicks': 200 * 10 ** 7},
                    self._ticks('CreditsStart', 300),
                ],
            },
            {
                'Id': 'ep-2', 'Type': 'Episode', 'ProviderIds': {},
                'SeriesId': 'series', 'Path': '/media/ep-2.mkv',
                'ParentIndexNumber': 1, 'IndexNumber': 2, 'Name': 'Two',
                'RunTimeTicks': 3600 * 10 ** 7,
                'MediaSources': [self._source('source-2', '/media/ep-2.mkv')],
                # No Chapters: use the browser cache for this exact item ID.
            },
            {
                'Id': 'ep-2-other-version', 'Type': 'Episode', 'ProviderIds': {},
                'SeriesId': 'series', 'Path': '/media/ep-2-alt.mkv',
                'ParentIndexNumber': 1, 'IndexNumber': 2, 'Name': 'Two alt',
                'RunTimeTicks': 3600 * 10 ** 7,
                'MediaSources': [self._source('source-2-alt', '/media/ep-2-alt.mkv')],
                # Same season/episode number, no matching cache ID.
            },
        ]
        browser_ep2 = {
            'Id': 'ep-2', 'SeasonId': 'season', 'SeriesName': 'Show',
            'ParentIndexNumber': 1, 'IndexNumber': 2, 'Name': 'Two',
            'Chapters': [self._ticks('IntroStart', 20), self._ticks('IntroEnd', 30)],
        }
        data = {
            'server': 'emby', 'scheme': 'http', 'netloc': 'emby.example',
            'api_key': 'token', 'user_id': 'user', 'mount_disk_mode': False,
            'playlist_info': [], 'main_ep_info': {
                'Id': 'ep-1', 'SeasonId': 'season', 'SeriesId': 'series',
            },
            'episodes_info': [browser_ep2], 'server_version': '4.8.0.40',
            'basename': 'ep-1.mkv', 'is_strm': False, 'is_http_source': False,
            'strm_direct': False, 'is_http_direct_strm': False,
            'strm_local_by_file_path': False, 'item_id': 'ep-1',
            'media_source_id': 'source-1', 'file_path': '/media/ep-1.mkv',
            'media_title': 'Show S1:E1 - One', 'sub_inner_idx': 0,
            'device_id': 'device', 'play_session_id': 'session',
            'headers': {}, 'sub_file': None,
            'chapters': [{'title': 'First item only', 'time': 999.0}],
            'intro_start': 900.0, 'intro_end': 999.0,
        }

        with mock.patch.object(data_parser, 'requests_urllib',
                               return_value={'Items': backend_items}) as request, \
                mock.patch.object(data_parser, 'match_version_range', return_value=True), \
                mock.patch.object(data_parser.configs.raw, 'get', return_value=''), \
                mock.patch.object(data_parser.configs.raw, 'getboolean', return_value=False), \
                mock.patch.object(data_parser.configs, 'check_str_match', return_value=False), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}):
            episodes = data_parser.list_episodes(data)
            self.assertTrue(request.call_args.args[0].endswith('/Shows/season/Episodes'))
            self.assertIn('Chapters', request.call_args.kwargs['params']['Fields'])
            data['playlist_info'] = [{'Id': item['Id']} for item in backend_items]
            playlist_episodes = data_parser.list_episodes(data)
            self.assertTrue(request.call_args.args[0].endswith('/Users/user/Items'))
            self.assertEqual([ep['chapters'] for ep in playlist_episodes],
                             [ep['chapters'] for ep in episodes])
            backend_items[1]['Chapters'] = []
            cleared = data_parser.list_episodes(data)
            self.assertEqual(cleared[1]['chapters'], [])
            self.assertIsNone(cleared[1]['intro_end'])

        self.assertIn('Chapters', request.call_args.kwargs['params']['Fields'])
        self.assertEqual(
            episodes[0]['chapters'], [
                {'title': 'Opening', 'time': 0.0},
                {'title': 'Main', 'time': 100.0},
                {'title': 'Middle', 'time': 200.0},
                {'title': 'Credits', 'time': 300.0},
            ],
        )
        self.assertEqual(
            episodes[1]['chapters'], [
                {'title': 'Opening', 'time': 20.0},
                {'title': 'Main', 'time': 30.0},
            ],
        )
        self.assertEqual(episodes[2]['chapters'], [])
        self.assertIsNone(episodes[2]['intro_start'])


if __name__ == '__main__':
    unittest.main()
