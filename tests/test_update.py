import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile, ZipInfo

from utils import update


class UpdateArchiveTests(unittest.TestCase):
    def _archive(self, root: Path, entries):
        archive = root / "update.zip"
        with ZipFile(archive, "w") as zf:
            for name, payload in entries:
                zf.writestr(name, payload)
        return archive

    def test_update_url_is_pinned_to_latest_beta_release_asset(self):
        self.assertEqual(
            update.UPDATE_URL,
            "https://github.com/hope140/embyToLocalPlayer/releases/latest/download/etlp-remote-control-beta.zip",
        )
        self.assertIn("releases/latest/download", update.UPDATE_URL)

    def test_github_prefix_is_flattened_and_live_config_is_protected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self._archive(
                root,
                [
                    ("hope140-embyToLocalPlayer-beta/", ""),
                    ("hope140-embyToLocalPlayer-beta/utils/", ""),
                    ("hope140-embyToLocalPlayer-beta/embyToLocalPlayer.py", "new"),
                    ("hope140-embyToLocalPlayer-beta/utils/example.py", "util"),
                    (
                        "hope140-embyToLocalPlayer-beta/embyToLocalPlayer_config.ini",
                        "[remote_control]\nenable = yes\n",
                    ),
                ],
            )
            live_config = root / "embyToLocalPlayer_config.ini"
            live_config.write_text("admin_api_key = must stay\n", encoding="utf-8")
            example = root / "embyToLocalPlayer_example.ini"

            prefix = update.extract_update_archive(archive, root, example, is_windows=False)

            self.assertEqual(prefix, "hope140-embyToLocalPlayer-beta")
            self.assertEqual((root / "embyToLocalPlayer.py").read_text(), "new")
            self.assertEqual((root / "utils/example.py").read_text(), "util")
            self.assertEqual(live_config.read_text(), "admin_api_key = must stay\n")
            self.assertIn("[remote_control]", example.read_text())
            self.assertFalse((root / "hope140-embyToLocalPlayer-beta").exists())

    def test_flat_archive_is_supported_and_all_config_variants_are_protected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self._archive(
                root,
                [
                    ("nested/", ""),
                    ("embyToLocalPlayer.py", "flat"),
                    ("embyToLocalPlayer_config.ini", "[emby]\n"),
                    ("nested/embyToLocalPlayer_config.backup", "secret"),
                ],
            )
            protected = root / "nested" / "embyToLocalPlayer_config.backup"
            protected.parent.mkdir()
            protected.write_text("old", encoding="utf-8")
            example = root / "example.ini"

            self.assertIsNone(update.extract_update_archive(archive, root, example, is_windows=False))
            self.assertEqual((root / "embyToLocalPlayer.py").read_text(), "flat")
            self.assertEqual(protected.read_text(), "old")
            self.assertEqual(example.read_text(), "[emby]\n")

    def test_only_runtime_package_members_are_extracted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self._archive(
                root,
                [
                    ("embyToLocalPlayer.py", "runtime"),
                    ("utils/runtime.py", "util"),
                    ("third_party/runtime.bin", "third-party"),
                    ("user_script/runtime.user.js", "user-script"),
                    ("tests/test_should_not_ship.py", "test"),
                    ("docs/architecture.md", "docs"),
                    ("scripts/package_beta.ps1", "script"),
                    (".codex/state.json", "metadata"),
                    ("README.md", "readme"),
                    ("embyToLocalPlayer_config.ini", "[emby]\n"),
                ],
            )
            example = root / "embyToLocalPlayer_example.ini"

            update.extract_update_archive(archive, root, example, is_windows=False)

            self.assertEqual((root / "embyToLocalPlayer.py").read_text(), "runtime")
            self.assertEqual((root / "utils/runtime.py").read_text(), "util")
            self.assertEqual((root / "third_party/runtime.bin").read_text(), "third-party")
            self.assertEqual((root / "user_script/runtime.user.js").read_text(), "user-script")
            self.assertEqual((root / "README.md").read_text(), "readme")
            for extra in ("tests", "docs", "scripts", ".codex"):
                self.assertFalse((root / extra).exists(), extra)
            self.assertEqual(example.read_text(), "[emby]\n")

    def test_zip_slip_paths_are_rejected_before_writing(self):
        unsafe_names = ("/absolute.txt", "C:/drive.txt", "../parent.txt", "a/../../parent.txt", r"..\parent.txt")
        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = self._archive(
                    root,
                    [
                        (unsafe_name, "bad"),
                        ("embyToLocalPlayer_config.ini", "[emby]\n"),
                    ],
                )
                with self.assertRaises(ValueError):
                    update.extract_update_archive(archive, root / "out", root / "example.ini", is_windows=False)
                self.assertFalse((root / "parent.txt").exists())

    def test_symlink_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "symlink.zip"
            with ZipFile(archive, "w") as zf:
                symlink = ZipInfo("link")
                symlink.create_system = 3
                symlink.external_attr = (0o120777 << 16) | 0xA0000000
                zf.writestr(symlink, "../../outside")
                zf.writestr("embyToLocalPlayer_config.ini", "[emby]\n")
            with self.assertRaises(ValueError):
                update.extract_update_archive(archive, root / "out", root / "example.ini", is_windows=False)

    def test_extraction_does_not_download(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = self._archive(root, [("embyToLocalPlayer_config.ini", "[emby]\n")])
            with mock.patch.object(update, "requests_urllib", side_effect=AssertionError("network")):
                update.extract_update_archive(archive, root / "out", root / "example.ini", is_windows=False)


if __name__ == "__main__":
    unittest.main()
