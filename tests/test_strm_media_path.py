import unittest
from configparser import ConfigParser
from unittest import mock

from utils import data_parser


class StrmMediaPathTests(unittest.TestCase):
    def test_filename_forms_keep_sidecar_directory_and_basename(self):
        raw = ConfigParser()
        raw.read_dict({'dev': {'strm_local_fallback_ext': ''}})
        cases = [
            ('Film.strm', '/different/Source.mkv', 'Film.mkv'),
            ('Film.strm', r'C:\different\Source.MP4', 'Film.MP4'),
            ('Film.strm', 'https://media.example/d/code/Source.mkv', 'Film.mkv'),
            ('Film.strm', 'https://media.example/d/code?/Source.mkv', 'Film.mkv'),
            ('Film.strm', 'https://media.example/d?pickcode=abc&name=Source%20Name.mkv', 'Film.mkv'),
            ('Film.strm', 'https://media.example/index.php?pickcode=abc&filename=Source.mp4', 'Film.mp4'),
            ('Film.strm', 'https://media.example/d?file_name=Source.mkv&pickcode=abc', 'Film.mkv'),
            ('Film.strm', 'https://media.example/d?pickcode=abc', 'Film.strm'),
            ('Film.strm', 'https://media.example/d?pickcode=abc.mkv', 'Film.strm'),
            ('Film.strm', 'https://media.example/index.php?pickcode=abc', 'Film.strm'),
            ('Film.strm', 'https://media.example/d?name=Source.exe', 'Film.strm'),
            ('Film.strm', 'http://[invalid', 'Film.strm'),
            ('Film.mkv.strm', 'https://media.example/d?pickcode=abc', 'Film.mkv'),
            ('Film.mkv.strm', '/different/Source.mkv', 'Film.mkv'),
            ('Film.MKV.STRM', '/different/Source.mp4', 'Film.MKV'),
            ('Film.2026.strm', 'https://media.example/d?pickcode=abc', 'Film.2026.strm'),
        ]
        with mock.patch.object(data_parser.configs, 'raw', raw):
            for sidecar, source, expected in cases:
                with self.subTest(sidecar=sidecar, source=source):
                    self.assertEqual(
                        data_parser.strm_local_media_path('/media/library/' + sidecar, source),
                        '/media/library/' + expected,
                    )

    def test_pure_pickcode_uses_only_valid_configured_fallback(self):
        for configured, expected in [('mkv', 'Film.mkv'), ('.MP4', 'Film.MP4'),
                                     ('strm', 'Film.strm'), ('exe', 'Film.strm')]:
            raw = ConfigParser()
            raw.read_dict({'dev': {'strm_local_fallback_ext': configured}})
            with self.subTest(configured=configured), \
                    mock.patch.object(data_parser.configs, 'raw', raw):
                self.assertEqual(data_parser.strm_local_media_path(
                    '/media/' + 'Film.strm', 'https://media.example/d?pickcode=abc'),
                    '/media/' + expected)
