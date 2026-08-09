import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

import utils.dependency_bootstrap as dependency_bootstrap


class DependencyBootstrapTests(unittest.TestCase):
    def _runtime_environment(self, *, platform="win32"):
        temp_dir = tempfile.TemporaryDirectory()
        package_root = Path(temp_dir.name) / "package"
        wheel_root = package_root / "third_party"
        (package_root / "utils").mkdir(parents=True)
        wheel_root.mkdir()

        wheel_names = ("alpha-1.0-py3-none-any.whl", "bravo-2.0-py3-none-any.whl")
        for index, wheel_name in enumerate(wheel_names):
            wheel_path = wheel_root / wheel_name
            with ZipFile(wheel_path, "w") as archive:
                archive.writestr(
                    f"sample_{index}/__init__.py",
                    f"VALUE = {index}\n",
                )
        manifest = {
            wheel_name: hashlib.sha256((wheel_root / wheel_name).read_bytes()).hexdigest()
            for wheel_name in wheel_names
        }

        patches = mock.patch.multiple(
            dependency_bootstrap,
            __file__=str(package_root / "utils" / "dependency_bootstrap.py"),
            _WHEELS=manifest,
        )
        platform_patch = mock.patch.object(dependency_bootstrap.sys, "platform", platform)
        version_patch = mock.patch.object(
            dependency_bootstrap.sys,
            "version_info",
            (3, 9, 0),
        )
        pointer_patch = mock.patch.object(dependency_bootstrap.struct, "calcsize", return_value=4)
        old_sys_path = list(sys.path)
        patches.start()
        platform_patch.start()
        version_patch.start()
        pointer_patch.start()
        self.addCleanup(pointer_patch.stop)
        self.addCleanup(version_patch.stop)
        self.addCleanup(platform_patch.stop)
        self.addCleanup(patches.stop)
        self.addCleanup(temp_dir.cleanup)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_sys_path))
        return package_root, wheel_root, manifest

    def test_non_target_platform_skips_without_loading(self):
        package_root, wheel_root, _ = self._runtime_environment(platform="linux")
        original_path = list(sys.path)

        dependency_bootstrap.ensure_bundled_dependencies()

        self.assertEqual(sys.path, original_path)
        self.assertFalse((wheel_root / "_runtime_deps").exists())

    def test_complete_wheels_and_marker_reuse_cache(self):
        package_root, wheel_root, _ = self._runtime_environment()

        dependency_bootstrap.ensure_bundled_dependencies()
        runtime_root = wheel_root / "_runtime_deps"
        marker = runtime_root / dependency_bootstrap._MARKER
        self.assertTrue(marker.is_file())
        marker_text = marker.read_text(encoding="utf-8")
        self.assertEqual(marker_text, dependency_bootstrap._marker_contents())

        with mock.patch.object(
            dependency_bootstrap,
            "_rebuild_runtime_cache",
            side_effect=AssertionError("cache should be reused"),
        ):
            dependency_bootstrap.ensure_bundled_dependencies()

        self.assertEqual(marker.read_text(encoding="utf-8"), marker_text)
        self.assertEqual(sys.path.count(str(runtime_root)), 1)

    def test_old_marker_triggers_rebuild(self):
        package_root, wheel_root, _ = self._runtime_environment()

        dependency_bootstrap.ensure_bundled_dependencies()
        marker = wheel_root / "_runtime_deps" / dependency_bootstrap._MARKER
        marker.write_text("etlp bundled runtime dependencies\n", encoding="utf-8")

        with mock.patch.object(
            dependency_bootstrap,
            "_rebuild_runtime_cache",
            wraps=dependency_bootstrap._rebuild_runtime_cache,
        ) as rebuild:
            dependency_bootstrap.ensure_bundled_dependencies()

        rebuild.assert_called_once()
        self.assertEqual(marker.read_text(encoding="utf-8"), dependency_bootstrap._marker_contents())

    def test_missing_wheel_returns_without_loading(self):
        package_root, wheel_root, manifest = self._runtime_environment()
        (wheel_root / next(iter(manifest))).unlink()
        original_path = list(sys.path)

        dependency_bootstrap.ensure_bundled_dependencies()

        self.assertEqual(sys.path, original_path)
        self.assertFalse((wheel_root / "_runtime_deps").exists())

    def test_corrupt_wheel_fails_closed_without_half_cache(self):
        package_root, wheel_root, manifest = self._runtime_environment()
        dependency_bootstrap.ensure_bundled_dependencies()
        runtime_root = wheel_root / "_runtime_deps"
        marker = runtime_root / dependency_bootstrap._MARKER
        marker_text = marker.read_text(encoding="utf-8")
        sys.path[:] = [entry for entry in sys.path if entry != str(runtime_root)]

        corrupt_wheel = wheel_root / next(iter(manifest))
        corrupt_wheel.write_bytes(b"corrupt wheel")
        original_path = list(sys.path)

        with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
            dependency_bootstrap.ensure_bundled_dependencies()

        self.assertEqual(sys.path, original_path)
        self.assertEqual(marker.read_text(encoding="utf-8"), marker_text)
        self.assertTrue(runtime_root.is_dir())
        self.assertFalse((wheel_root / "_runtime_deps.tmp").exists())

    def test_marker_fingerprint_is_deterministic_and_ordered(self):
        first_manifest = {
            "alpha.whl": "a" * 64,
            "bravo.whl": "b" * 64,
        }
        with mock.patch.object(dependency_bootstrap, "_WHEELS", first_manifest):
            first = dependency_bootstrap._manifest_fingerprint()
            second = dependency_bootstrap._manifest_fingerprint()
            marker = dependency_bootstrap._marker_contents()
        self.assertEqual(first, second)
        self.assertIn(f"fingerprint={first}\n", marker)

        reordered_manifest = {
            "bravo.whl": "b" * 64,
            "alpha.whl": "a" * 64,
        }
        with mock.patch.object(dependency_bootstrap, "_WHEELS", reordered_manifest):
            self.assertNotEqual(first, dependency_bootstrap._manifest_fingerprint())


if __name__ == "__main__":
    unittest.main()
