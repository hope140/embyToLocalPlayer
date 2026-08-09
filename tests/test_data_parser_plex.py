import unittest
from unittest import mock

import utils.data_parser as data_parser


class PlexParserTests(unittest.TestCase):
    @staticmethod
    def _received_data(mount_disk_enable):
        return {
            'extraData': {},
            'playbackUrl': (
                'https://plex.example:32400/library/metadata/100?'
                'X-Plex-Token=plex-token&'
                'X-Plex-Client-Identifier=client-id&'
                'X-Plex-Version=1.40.0'
            ),
            'playbackData': {
                'MediaContainer': {
                    'Metadata': [{
                        'title': 'Movie',
                        'ratingKey': '100',
                        'type': 'movie',
                        'Media': [{
                            'id': 'media-100',
                            'duration': 120000,
                            'Part': [{
                                'file': r'C:\Media\Movie.mkv',
                                'size': 123,
                                'key': '/library/parts/100/file.mkv',
                            }],
                        }],
                    }],
                },
            },
            'mountDiskEnable': mount_disk_enable,
        }

    def _parse(self, mount_disk_enable):
        received_data = self._received_data(mount_disk_enable)
        mapped_path = r'Z:\Mounted\Mapped.Movie.mkv'
        with mock.patch.object(data_parser, 'show_version_info'), \
                mock.patch.object(data_parser, 'logger_setup'), \
                mock.patch.object(data_parser, 'force_disk_mode_by_path', return_value=False), \
                mock.patch.object(data_parser, 'translate_path_by_ini', return_value=mapped_path), \
                mock.patch.object(data_parser.configs, 'media_title_translate', return_value={}), \
                mock.patch.object(data_parser, 'maybe_register_strm_cd2_url',
                                  return_value='https://cd2.example/should-not-be-used') as register:
            result = data_parser.parse_received_data_plex(received_data)
        register.assert_not_called()
        self.assertNotIn('use_strm_cd2_url', result)
        self.assertNotIn('strm_cd2_local_path', result)
        return result, mapped_path

    def test_mounted_payload_uses_translated_media_path_and_basename(self):
        result, mapped_path = self._parse('true')

        self.assertTrue(result['mount_disk_mode'])
        self.assertEqual(result['media_path'], mapped_path)
        self.assertEqual(result['media_basename'], 'Mapped.Movie.mkv')

    def test_network_payload_uses_plex_stream_path_and_basename(self):
        result, _ = self._parse('false')

        self.assertFalse(result['mount_disk_mode'])
        self.assertEqual(
            result['media_path'],
            'https://plex.example:32400/library/parts/100/file.mkv?'
            'download=0&X-Plex-Token=plex-token',
        )
        self.assertEqual(
            result['media_basename'],
            'file.mkv?download=0&X-Plex-Token=plex-token',
        )


if __name__ == '__main__':
    unittest.main()
