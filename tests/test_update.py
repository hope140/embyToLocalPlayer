import hashlib
import json
import subprocess
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

    def test_release_api_is_channel_scoped(self):
        self.assertEqual(
            update.RELEASES_API_URL,
            "https://api.gitcode.com/api/v5/repos/h0pe14o/embyToLocalPlayer/releases?per_page=100",
        )
        self.assertNotIn("releases/latest/download", update.RELEASES_API_URL)
        self.assertEqual(
            update.GITHUB_RELEASES_API_URL,
            "https://api.github.com/repos/hope140/embyToLocalPlayer/releases?per_page=100",
        )
        self.assertEqual(update.DEFAULT_RELEASE_SOURCE, "gitcode")
        self.assertEqual(update.RELEASE_SOURCE_ORDER, ("gitcode", "github"))
        self.assertEqual(
            update.CHANNEL_ASSETS,
            {
                "beta": {
                    "package": "etlp-remote-control-beta.zip",
                    "checksum": "etlp-remote-control-beta.zip.sha256",
                },
                "stable": {
                    "package": "etlp-remote-control-stable.zip",
                    "checksum": "etlp-remote-control-stable.zip.sha256",
                },
            },
        )

    def test_latest_release_selection_keeps_channels_separate(self):
        releases = [
            {
                "id": 1,
                "tag_name": "2026.08.13.3-beta",
                "published_at": "2026-08-13T10:00:00Z",
                "assets": [
                    {"name": "etlp-remote-control-beta.zip"},
                    {"name": "etlp-remote-control-beta.zip.sha256"},
                ],
            },
            {
                "id": 2,
                "tag_name": "2026.08.14",
                "published_at": "2026-08-14T10:00:00Z",
                "assets": [
                    {"name": "etlp-remote-control-stable.zip"},
                    {"name": "etlp-remote-control-stable.zip.sha256"},
                ],
            },
            {
                "id": 3,
                "tag_name": "2026.08.14.1-beta",
                "published_at": "2026-08-14T11:00:00Z",
                "assets": [
                    {"name": "etlp-remote-control-beta.zip"},
                    {"name": "etlp-remote-control-beta.zip.sha256"},
                ],
            },
        ]

        beta = update.select_latest_release(releases, "beta")
        stable = update.select_latest_release(releases, "stable")

        self.assertEqual(beta["tag"], "2026.08.14.1-beta")
        self.assertEqual(beta["package_asset"], "etlp-remote-control-beta.zip")
        self.assertEqual(stable["tag"], "2026.08.14")
        self.assertEqual(stable["package_asset"], "etlp-remote-control-stable.zip")
        self.assertNotIn("/latest/", beta["update_url"])
        self.assertIn("/2026.08.14.1-beta/", beta["update_url"])
        self.assertIn("/2026.08.14/", stable["update_url"])
        self.assertEqual(beta["source"], "gitcode")

    def test_gitcode_release_uses_attachment_url_from_api_when_available(self):
        assets = update.CHANNEL_ASSETS["beta"]
        tag = "2026.08.14.2-beta"
        release = update.select_latest_release(
            [{
                "id": "12",
                "tag_name": tag,
                "created_at": "2026-08-14T12:00:00+08:00",
                "release_status": "pre",
                "assets": [
                    {
                        "name": assets["package"],
                        "browser_download_url": "https://gitcode.com/download/package.zip",
                    },
                    {
                        "name": assets["checksum"],
                    },
                    {
                        "name": update.MANIFEST_ASSET,
                        "browser_download_url": "https://gitcode.com/download/release-plan.json",
                    },
                ],
            }],
            "beta",
        )

        self.assertEqual(release["source"], "gitcode")
        self.assertEqual(release["update_url"], "https://gitcode.com/download/package.zip")
        self.assertEqual(
            release["checksum_url"],
            "https://api.gitcode.com/api/v5/repos/h0pe14o/embyToLocalPlayer/releases/"
            "2026.08.14.2-beta/attach_files/etlp-remote-control-beta.zip.sha256/download",
        )
        self.assertEqual(release["manifest_url"], "https://gitcode.com/download/release-plan.json")

    def test_resolve_update_urls_falls_back_to_github_on_gitcode_api_failure(self):
        assets = update.CHANNEL_ASSETS["beta"]
        releases = [{
            "id": 1,
            "tag_name": "2026.08.14.3-beta",
            "published_at": "2026-08-14T13:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }]
        calls = []

        def fake_requests(url, **kwargs):
            calls.append(url)
            if url == update.GITCODE_RELEASES_API_URL:
                raise ConnectionError("GitCode unavailable")
            if url == update.GITHUB_RELEASES_API_URL:
                return releases
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
            resolved = update.resolve_update_urls("beta")

        self.assertEqual(calls, [update.GITCODE_RELEASES_API_URL, update.GITHUB_RELEASES_API_URL])
        self.assertEqual(resolved["source"], "github")
        self.assertTrue(resolved["update_url"].startswith("https://github.com/"))

    def test_resolve_update_urls_falls_back_when_gitcode_has_no_qualified_assets(self):
        assets = update.CHANNEL_ASSETS["beta"]
        releases = [{
            "id": 2,
            "tag_name": "2026.08.14.4-beta",
            "published_at": "2026-08-14T14:00:00Z",
            "assets": [{"name": assets["package"]}],
        }]
        calls = []

        def fake_requests(url, **kwargs):
            calls.append(url)
            if url == update.GITCODE_RELEASES_API_URL:
                return releases
            if url == update.GITHUB_RELEASES_API_URL:
                return [{
                    "id": 3,
                    "tag_name": "2026.08.14.5-beta",
                    "published_at": "2026-08-14T15:00:00Z",
                    "assets": [
                        {"name": assets["package"]},
                        {"name": assets["checksum"]},
                    ],
                }]
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
            resolved = update.resolve_update_urls("beta")

        self.assertEqual(calls, [update.GITCODE_RELEASES_API_URL, update.GITHUB_RELEASES_API_URL])
        self.assertEqual(resolved["source"], "github")
        self.assertEqual(resolved["tag"], "2026.08.14.5-beta")

    def test_resolve_update_urls_fails_closed_when_both_sources_fail(self):
        calls = []

        def fake_requests(url, **kwargs):
            calls.append(url)
            raise ConnectionError("source unavailable")

        with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
            with self.assertRaisesRegex(ValueError, "GitCode primary or GitHub fallback"):
                update.resolve_update_urls("beta")

        self.assertEqual(calls, [update.GITCODE_RELEASES_API_URL, update.GITHUB_RELEASES_API_URL])

    def test_latest_release_selection_treats_manifest_as_optional(self):
        assets = update.CHANNEL_ASSETS["beta"]
        release = {
            "id": 1,
            "tag_name": "2026.08.14.1-beta",
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }

        without_manifest = update.select_latest_release([release], "beta")
        self.assertNotIn("manifest_url", without_manifest)

        release["assets"].append({"name": update.MANIFEST_ASSET})
        with_manifest = update.select_latest_release([release], "beta")
        self.assertEqual(with_manifest["manifest_asset"], update.MANIFEST_ASSET)
        self.assertIn(f"/{update.MANIFEST_ASSET}", with_manifest["manifest_url"])

    def test_latest_release_selection_rejects_missing_assets(self):
        with self.assertRaisesRegex(ValueError, "no published stable release"):
            update.select_latest_release(
                [{
                    "tag_name": "2026.08.14",
                    "published_at": "2026-08-14T10:00:00Z",
                    "assets": [{"name": "etlp-remote-control-stable.zip"}],
                }],
                "stable",
            )

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
                    ("embyToLocalPlayer_debug.bat", "launcher"),
                    ("LICENSE", "license"),
                    ("requirements.txt", "requirements"),
                    ("utils/runtime.py", "util"),
                    ("utils/release_info.py", "release"),
                    ("utils/notes.md", "docs"),
                    ("utils/others/etlp_run.command", "alternate launcher"),
                    ("third_party/runtime.whl", "third-party wheel"),
                    ("third_party/runtime.bin", "not a wheel"),
                    ("third_party/README.md", "third-party docs"),
                    ("third_party/clouddrive2/clouddrive.proto", "source proto"),
                    ("user_script/runtime.user.js", "user-script"),
                    ("tests/test_should_not_ship.py", "test"),
                    ("docs/architecture.md", "docs"),
                    ("scripts/package_beta.ps1", "script"),
                    (".codex/state.json", "metadata"),
                    ("README.md", "readme"),
                    ("FUNCTIONS.md", "functions"),
                    ("embyToLocalPlayer_config.ini", "[emby]\n"),
                ],
            )
            example = root / "embyToLocalPlayer_example.ini"

            update.extract_update_archive(archive, root, example, is_windows=False)

            self.assertEqual((root / "embyToLocalPlayer.py").read_text(), "runtime")
            self.assertEqual((root / "embyToLocalPlayer_debug.bat").read_text(), "launcher")
            self.assertEqual((root / "LICENSE").read_text(), "license")
            self.assertEqual((root / "requirements.txt").read_text(), "requirements")
            self.assertEqual((root / "utils/runtime.py").read_text(), "util")
            self.assertEqual((root / "utils/release_info.py").read_text(), "release")
            self.assertEqual((root / "third_party/runtime.whl").read_text(), "third-party wheel")
            self.assertEqual((root / "user_script/runtime.user.js").read_text(), "user-script")
            for extra in (
                "tests",
                "docs",
                "scripts",
                ".codex",
                "README.md",
                "FUNCTIONS.md",
                "utils/notes.md",
                "utils/others",
                "third_party/runtime.bin",
                "third_party/README.md",
                "third_party/clouddrive2",
            ):
                self.assertFalse((root / extra).exists(), extra)
            self.assertEqual(example.read_text(), "[emby]\n")

    def test_release_info_member_is_accepted_by_legacy_update_rules(self):
        legacy_source = subprocess.check_output(
            ["git", "show", "23a107c:utils/update.py"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
        )
        self.assertNotIn("etlp_release.json", legacy_source)

        def legacy_runtime_member(name):
            if not name or "/" not in name:
                return False
            parts = name.split("/")
            if parts[0] == "utils":
                suffix = Path(parts[-1]).suffix.casefold()
                return suffix == ".py" and not any(
                    part.casefold() == "others" for part in parts[1:-1]
                )
            return False

        self.assertTrue(legacy_runtime_member("utils/release_info.py"))

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


class UpdateDownloadTests(unittest.TestCase):
    def _stub_download(self, archive_payload, checksum_text, channel="beta"):
        tag = "2026.08.14.1-beta" if channel == "beta" else "2026.08.14"
        assets = update.CHANNEL_ASSETS[channel]
        releases = [{
            "id": 1,
            "tag_name": tag,
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }]
        release = update.select_latest_release(releases, channel)
        github_release = update.select_latest_release(releases, channel, source="github")
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url in (update.GITCODE_RELEASES_API_URL, update.GITHUB_RELEASES_API_URL):
                return releases
            if url in (release["checksum_url"], github_release["checksum_url"]):
                return checksum_text
            if url in (release["update_url"], github_release["update_url"]):
                Path(kwargs["save_path"]).write_bytes(archive_payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        return calls, fake_requests, release

    def test_checksum_parser_accepts_single_record_and_normalises_case(self):
        digest = "A" * 64
        self.assertEqual(
            update.parse_checksum(f"{digest}  {update.PACKAGE_ASSET}\n"),
            digest.lower(),
        )

    def test_checksum_parser_rejects_multiple_records_invalid_hex_and_wrong_name(self):
        valid = "a" * 64
        invalid_records = (
            f"{valid}  {update.PACKAGE_ASSET}\n{valid}  {update.PACKAGE_ASSET}\n",
            f"{'g' * 64}  {update.PACKAGE_ASSET}\n",
            f"{valid} {update.PACKAGE_ASSET}\n",
            f"{valid}  other.zip\n",
        )
        for checksum_text in invalid_records:
            with self.subTest(checksum_text=checksum_text):
                with self.assertRaises(ValueError):
                    update.parse_checksum(checksum_text)

    def test_verified_download_replaces_archive_atomically_and_keeps_pycache(self):
        payload = b"verified update archive"
        digest = hashlib.sha256(payload).hexdigest()
        checksum_text = f"{digest}  {update.PACKAGE_ASSET}\n"
        calls, fake_requests, release = self._stub_download(payload, checksum_text)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")
            pycache = root / "utils" / "__pycache__"
            pycache.mkdir(parents=True)
            marker = pycache / "keep.pyc"
            marker.write_bytes(b"old bytecode")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                result = update.download_verified_update(root)

            self.assertEqual(Path(result), live_archive)
            self.assertEqual(live_archive.read_bytes(), payload)
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())
            self.assertTrue(marker.exists())
            self.assertEqual(
                [url for url, _ in calls],
                [
                    update.RELEASES_API_URL,
                    release["checksum_url"],
                    release["update_url"],
                ],
            )
            self.assertEqual(calls[2][1]["save_path"], str(root / "embyToLocalPlayer.zip.part"))

    def test_verified_download_validates_optional_manifest(self):
        payload = b"verified manifest archive"
        digest = hashlib.sha256(payload).hexdigest()
        assets = update.CHANNEL_ASSETS["beta"]
        tag = "2026.08.14.1-beta"
        releases = [{
            "id": 1,
            "tag_name": tag,
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
                {"name": update.MANIFEST_ASSET},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        manifest = {
            "schema": 1,
            "channel": "beta",
            "version": tag,
            "branch": "beta",
            "commit": "a" * 40,
            "packageAsset": assets["package"],
            "checksumAsset": assets["checksum"],
            "packageSha256": digest,
            "packageSize": len(payload),
        }
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == update.RELEASES_API_URL:
                return releases
            if url == release["checksum_url"]:
                return f"{digest}  {assets['package']}\n"
            if url == release["manifest_url"]:
                return json.dumps(manifest)
            if url == release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                result = update.download_verified_update(root)

            self.assertEqual(Path(result).read_bytes(), payload)
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())

        self.assertEqual(
            [url for url, _ in calls],
            [
                update.RELEASES_API_URL,
                release["checksum_url"],
                release["manifest_url"],
                release["update_url"],
            ],
        )

    def test_invalid_manifest_keeps_live_archive_and_cleans_part(self):
        payload = b"must not replace archive"
        digest = hashlib.sha256(payload).hexdigest()
        assets = update.CHANNEL_ASSETS["beta"]
        tag = "2026.08.14.1-beta"
        releases = [{
            "id": 1,
            "tag_name": tag,
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
                {"name": update.MANIFEST_ASSET},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == update.RELEASES_API_URL:
                return releases
            if url == update.GITHUB_RELEASES_API_URL:
                raise ConnectionError("GitHub unavailable")
            if url == release["checksum_url"]:
                return f"{digest}  {assets['package']}\n"
            if url == release["manifest_url"]:
                return json.dumps({
                    "schema": 1,
                    "channel": "beta",
                    "version": "wrong-tag",
                    "branch": "beta",
                    "packageAsset": assets["package"],
                    "checksumAsset": assets["checksum"],
                    "packageSha256": digest,
                    "packageSize": len(payload),
                })
            if url == release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")
            part = root / "embyToLocalPlayer.zip.part"
            part.write_bytes(b"stale partial archive")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                with self.assertRaisesRegex(ValueError, "version"):
                    update.download_verified_update(root)

            self.assertEqual(live_archive.read_bytes(), b"old archive")
            self.assertFalse(part.exists())

        self.assertEqual(
            [url for url, _ in calls],
            [
                update.RELEASES_API_URL,
                release["checksum_url"],
                release["manifest_url"],
                update.GITHUB_RELEASES_API_URL,
            ],
        )

    def test_manifest_size_mismatch_keeps_live_archive_and_cleans_part(self):
        payload = b"archive with an unexpected size"
        digest = hashlib.sha256(payload).hexdigest()
        assets = update.CHANNEL_ASSETS["beta"]
        tag = "2026.08.14.1-beta"
        releases = [{
            "id": 1,
            "tag_name": tag,
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
                {"name": update.MANIFEST_ASSET},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == update.RELEASES_API_URL:
                return releases
            if url == update.GITHUB_RELEASES_API_URL:
                raise ConnectionError("GitHub unavailable")
            if url == release["checksum_url"]:
                return f"{digest}  {assets['package']}\n"
            if url == release["manifest_url"]:
                return json.dumps({
                    "schema": 1,
                    "channel": "beta",
                    "version": tag,
                    "branch": "beta",
                    "packageAsset": assets["package"],
                    "checksumAsset": assets["checksum"],
                    "packageSha256": digest,
                    "packageSize": len(payload) + 1,
                })
            if url == release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                with self.assertRaisesRegex(ValueError, "size mismatch"):
                    update.download_verified_update(root)

            self.assertEqual(live_archive.read_bytes(), b"old archive")
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())

        self.assertEqual(
            [url for url, _ in calls],
            [
                update.RELEASES_API_URL,
                release["checksum_url"],
                release["manifest_url"],
                release["update_url"],
                update.GITHUB_RELEASES_API_URL,
            ],
        )

    def test_checksum_mismatch_cleans_part_without_changing_live_files(self):
        payload = b"downloaded bytes that do not match"
        expected = hashlib.sha256(b"different bytes").hexdigest()
        calls, fake_requests, release = self._stub_download(
            payload,
            f"{expected}  {update.PACKAGE_ASSET}\n",
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")
            pycache = root / "utils" / "__pycache__"
            pycache.mkdir(parents=True)
            marker = pycache / "keep.pyc"
            marker.write_bytes(b"old bytecode")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                    update.download_verified_update(root)

            self.assertEqual(live_archive.read_bytes(), b"old archive")
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())
            self.assertTrue(marker.exists())
            self.assertEqual(
                [url for url, _ in calls],
                [
                    update.RELEASES_API_URL,
                    release["checksum_url"],
                    release["update_url"],
                    update.GITHUB_RELEASES_API_URL,
                    update._release_download_url(
                        release["tag"], release["checksum_asset"], source="github"
                    ),
                    update._release_download_url(
                        release["tag"], release["package_asset"], source="github"
                    ),
                ],
            )

    def test_malformed_checksum_does_not_download_or_change_live_files(self):
        calls, fake_requests, _ = self._stub_download(b"unused", "not a checksum\n")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")
            part = root / "embyToLocalPlayer.zip.part"
            part.write_bytes(b"stale partial archive")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                with self.assertRaises(ValueError):
                    update.download_verified_update(root)

            self.assertEqual(live_archive.read_bytes(), b"old archive")
            self.assertFalse(part.exists())
            self.assertEqual(
                [url for url, _ in calls],
                [
                    update.RELEASES_API_URL,
                    update.select_latest_release(
                        [{
                            "id": 1,
                            "tag_name": "2026.08.14.1-beta",
                            "published_at": "2026-08-14T10:00:00Z",
                            "assets": [
                                {"name": update.CHANNEL_ASSETS["beta"]["package"]},
                                {"name": update.CHANNEL_ASSETS["beta"]["checksum"]},
                            ],
                        }],
                        "beta",
                    )["checksum_url"],
                    update.GITHUB_RELEASES_API_URL,
                    update._release_download_url(
                        "2026.08.14.1-beta",
                        update.CHANNEL_ASSETS["beta"]["checksum"],
                        source="github",
                    ),
                ],
            )

    def test_archive_download_exception_cleans_partial_file(self):
        payload = b"partial archive"
        checksum = hashlib.sha256(payload).hexdigest()
        assets = update.CHANNEL_ASSETS["beta"]
        releases = [{
            "id": 1,
            "tag_name": "2026.08.14.1-beta",
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        calls = []

        def failing_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == update.RELEASES_API_URL:
                return releases
            if url == update.GITHUB_RELEASES_API_URL:
                raise ConnectionError("GitHub unavailable")
            if url == release["checksum_url"]:
                return f"{checksum}  {update.PACKAGE_ASSET}\n"
            if url == release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                raise OSError("simulated download failure")
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")

            with mock.patch.object(update, "requests_urllib", side_effect=failing_requests):
                with self.assertRaisesRegex(ValueError, "simulated download failure"):
                    update.download_verified_update(root)

            self.assertEqual(live_archive.read_bytes(), b"old archive")
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())
        self.assertEqual(
            [url for url, _ in calls],
            [
                update.RELEASES_API_URL,
                release["checksum_url"],
                release["update_url"],
                update.GITHUB_RELEASES_API_URL,
            ],
        )

    def test_verified_download_falls_back_to_github_after_gitcode_hash_failure(self):
        payload = b"valid GitHub fallback archive"
        digest = hashlib.sha256(payload).hexdigest()
        wrong_digest = hashlib.sha256(b"GitCode served a different archive").hexdigest()
        assets = update.CHANNEL_ASSETS["beta"]
        tag = "2026.08.14.6-beta"
        releases = [{
            "id": 1,
            "tag_name": tag,
            "published_at": "2026-08-14T16:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }]
        gitcode_release = update.select_latest_release(releases, "beta")
        github_release = update.select_latest_release(releases, "beta", source="github")
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == update.GITCODE_RELEASES_API_URL:
                return releases
            if url == gitcode_release["checksum_url"]:
                return f"{wrong_digest}  {assets['package']}\n"
            if url == gitcode_release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            if url == update.GITHUB_RELEASES_API_URL:
                return releases
            if url == github_release["checksum_url"]:
                return f"{digest}  {assets['package']}\n"
            if url == github_release["update_url"]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_archive = root / "embyToLocalPlayer.zip"
            live_archive.write_bytes(b"old archive")

            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                result = update.download_verified_update(root)

            self.assertEqual(Path(result).read_bytes(), payload)
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())

        self.assertEqual(
            [url for url, _ in calls],
            [
                update.GITCODE_RELEASES_API_URL,
                gitcode_release["checksum_url"],
                gitcode_release["update_url"],
                update.GITHUB_RELEASES_API_URL,
                github_release["checksum_url"],
                github_release["update_url"],
            ],
        )

    def test_stable_download_uses_stable_assets(self):
        payload = b"verified stable archive"
        digest = hashlib.sha256(payload).hexdigest()
        stable_asset = update.CHANNEL_ASSETS["stable"]["package"]
        checksum_text = f"{digest}  {stable_asset}\n"
        calls, fake_requests, release = self._stub_download(payload, checksum_text, channel="stable")

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests), \
                    mock.patch.object(update, "load_release_channel", return_value="stable"):
                update.download_verified_update(Path(temp))

        self.assertEqual(release["channel"], "stable")
        self.assertIn("etlp-remote-control-stable.zip", release["update_url"])
        self.assertNotIn("-beta/", release["update_url"])

    def test_cdn_template_rewrites_api_checksum_and_archive_urls(self):
        payload = b"verified CDN archive"
        digest = hashlib.sha256(payload).hexdigest()
        cdn_template = "https://cdn.example/{url}"
        assets = update.CHANNEL_ASSETS["beta"]
        releases = [{
            "id": 1,
            "tag_name": "2026.08.14.1-beta",
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        expected_urls = [
            cdn_template.replace("{url}", update.RELEASES_API_URL),
            cdn_template.replace("{url}", release["checksum_url"]),
            cdn_template.replace("{url}", release["update_url"]),
        ]
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == expected_urls[0]:
                return releases
            if url == expected_urls[1]:
                return f"{digest}  {release['package_asset']}\n"
            if url == expected_urls[2]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                result = update.download_verified_update(root, update_cdn_url=cdn_template)

            self.assertEqual(Path(result).read_bytes(), payload)
            self.assertFalse((root / "embyToLocalPlayer.zip.part").exists())

        self.assertEqual([url for url, _ in calls], expected_urls)

    def test_cdn_template_rewrites_optional_manifest_url(self):
        payload = b"verified CDN manifest archive"
        digest = hashlib.sha256(payload).hexdigest()
        cdn_template = "https://cdn.example/{url}"
        assets = update.CHANNEL_ASSETS["beta"]
        releases = [{
            "id": 1,
            "tag_name": "2026.08.14.1-beta",
            "published_at": "2026-08-14T10:00:00Z",
            "assets": [
                {"name": assets["package"]},
                {"name": assets["checksum"]},
                {"name": update.MANIFEST_ASSET},
            ],
        }]
        release = update.select_latest_release(releases, "beta")
        expected_urls = [
            cdn_template.replace("{url}", update.RELEASES_API_URL),
            cdn_template.replace("{url}", release["checksum_url"]),
            cdn_template.replace("{url}", release["manifest_url"]),
            cdn_template.replace("{url}", release["update_url"]),
        ]
        manifest = {
            "schema": 1,
            "channel": "beta",
            "version": release["tag"],
            "branch": "beta",
            "packageAsset": assets["package"],
            "checksumAsset": assets["checksum"],
            "packageSha256": digest,
            "packageSize": len(payload),
        }
        calls = []

        def fake_requests(url, **kwargs):
            calls.append((url, kwargs))
            if url == expected_urls[0]:
                return releases
            if url == expected_urls[1]:
                return f"{digest}  {assets['package']}\n"
            if url == expected_urls[2]:
                return json.dumps(manifest)
            if url == expected_urls[3]:
                Path(kwargs["save_path"]).write_bytes(payload)
                return kwargs["save_path"]
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(update, "requests_urllib", side_effect=fake_requests):
                result = update.download_verified_update(root, update_cdn_url=cdn_template)

            self.assertEqual(Path(result).read_bytes(), payload)

        self.assertEqual([url for url, _ in calls], expected_urls)

    def test_cdn_template_rejects_invalid_values_before_network(self):
        invalid_templates = (
            "https://cdn.example/no-placeholder",
            "https://cdn.example/{url}/{url}",
            "http://cdn.example/{url}",
            "https:///{url}",
        )
        for cdn_template in invalid_templates:
            with self.subTest(cdn_template=cdn_template):
                with mock.patch.object(update, "requests_urllib") as requests:
                    with self.assertRaisesRegex(ValueError, "update_cdn_url"):
                        update.resolve_update_urls("beta", update_cdn_url=cdn_template)
                requests.assert_not_called()


if __name__ == "__main__":
    unittest.main()
