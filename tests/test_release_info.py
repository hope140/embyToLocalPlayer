import sys
import unittest
from unittest import mock

from utils.configs import MyLogger, configs
from utils import release_info


class ReleaseInfoTests(unittest.TestCase):
    def test_valid_metadata_is_read(self):
        with mock.patch.object(release_info, "RELEASE_VERSION", "2026.08.12.2-beta"), \
                mock.patch.object(release_info, "RELEASE_COMMIT", "0123456789ab"), \
                mock.patch.object(release_info, "RELEASE_CHANNEL", "beta"):
            self.assertEqual(release_info.load_release_info(),
                             ("2026.08.12.2-beta", "0123456789ab"))
            self.assertEqual(release_info.load_release_channel(), "beta")

    def test_stable_channel_is_read(self):
        with mock.patch.object(release_info, "RELEASE_VERSION", "2026.08.14"), \
                mock.patch.object(release_info, "RELEASE_COMMIT", "0123456789ab"), \
                mock.patch.object(release_info, "RELEASE_CHANNEL", "stable"):
            self.assertEqual(release_info.load_release_info(), ("2026.08.14", "0123456789ab"))
            self.assertEqual(release_info.load_release_channel(), "stable")

    def test_source_placeholders_use_stable_fallback(self):
        with mock.patch.object(release_info, "RELEASE_VERSION", "source"), \
                mock.patch.object(release_info, "RELEASE_COMMIT", "unknown"), \
                mock.patch.object(release_info, "RELEASE_CHANNEL", "source"):
            self.assertEqual(release_info.load_release_info(), ("source", "unknown"))
            self.assertEqual(release_info.load_release_channel(), "beta")

    def test_invalid_fields_use_stable_fallback(self):
        with mock.patch.object(release_info, "RELEASE_VERSION", "../secret"), \
                mock.patch.object(release_info, "RELEASE_COMMIT", "not-a-commit"), \
                mock.patch.object(release_info, "RELEASE_CHANNEL", "prod"):
            self.assertEqual(release_info.load_release_info(), ("source", "unknown"))
            self.assertEqual(release_info.load_release_channel(), "beta")

    def test_print_version_includes_searchable_release_line(self):
        with mock.patch.object(MyLogger, "log") as log, \
                mock.patch("utils.tools.show_version_info", return_value="script"):
            configs.print_version()
        self.assertTrue(any(
            call.args and isinstance(call.args[0], str)
            and call.args[0].startswith("ETLP release=")
            and " commit=" in call.args[0]
            and " channel=" in call.args[0]
            for call in log.call_args_list
        ))

    def test_print_version_falls_back_when_metadata_module_is_unavailable(self):
        with mock.patch.dict(sys.modules, {"utils.release_info": None}), \
                mock.patch.object(MyLogger, "log") as log, \
                mock.patch("utils.tools.show_version_info", return_value="script"):
            configs.print_version()
        self.assertTrue(any(
            call.args and "ETLP release=source commit=unknown" in call.args[0]
            for call in log.call_args_list
        ))

    def test_print_version_falls_back_when_metadata_is_corrupt(self):
        with mock.patch.object(release_info, "load_release_info",
                               side_effect=ValueError("corrupt")), \
                mock.patch.object(MyLogger, "log") as log, \
                mock.patch("utils.tools.show_version_info", return_value="script"):
            configs.print_version()
        self.assertTrue(any(
            call.args and "ETLP release=source commit=unknown" in call.args[0]
            for call in log.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
