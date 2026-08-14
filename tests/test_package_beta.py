import hashlib
import ast
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


class BetaPackageTests(unittest.TestCase):
    def test_package_script_contains_only_runtime_manifest(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            self.skipTest("PowerShell is required to execute scripts/package_beta.ps1")
        current_branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            text=True,
        ).strip()
        if current_branch != "beta":
            self.skipTest(
                f"runtime packaging is guarded to the beta branch (current: {current_branch})"
            )

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "package_beta.ps1"),
                    "-OutputDirectory",
                    str(output),
                    "-ReleaseVersion",
                    "2026.08.12.2-beta",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            archive_path = output / "etlp-remote-control-beta.zip"
            checksum_path = Path(f"{archive_path}.sha256")
            self.assertTrue(archive_path.is_file())
            self.assertTrue(checksum_path.is_file())

            expected = {
                "embyToLocalPlayer.py",
                "embyToLocalPlayer_config.ini",
                "embyToLocalPlayer_debug.bat",
                "LICENSE",
                "requirements.txt",
            }

            utils_root = ROOT / "utils"
            for path in utils_root.rglob("*.py"):
                relative = path.relative_to(utils_root)
                if "others" not in relative.parts:
                    expected.add(Path("utils", *relative.parts).as_posix())

            for path in (ROOT / "user_script").rglob("*.js"):
                expected.add(path.relative_to(ROOT).as_posix())

            for path in (ROOT / "third_party").glob("*.whl"):
                expected.add(path.relative_to(ROOT).as_posix())

            with ZipFile(archive_path) as archive:
                actual = {
                    info.filename.rstrip("/")
                    for info in archive.infolist()
                    if not info.is_dir()
                }
                release_info_source = archive.read("utils/release_info.py").decode("utf-8")

            self.assertFalse(release_info_source.startswith("\ufeff"))
            assignments = {}
            for node in ast.parse(release_info_source).body:
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    if isinstance(target, ast.Name) and target.id in {
                            "RELEASE_VERSION", "RELEASE_COMMIT", "RELEASE_CHANNEL"}:
                        assignments[target.id] = ast.literal_eval(node.value)

            self.assertEqual(actual, expected)
            self.assertEqual(assignments["RELEASE_VERSION"], "2026.08.12.2-beta")
            self.assertEqual(assignments["RELEASE_COMMIT"], subprocess.check_output(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=ROOT,
                text=True,
            ).strip())
            self.assertEqual(assignments["RELEASE_CHANNEL"], "beta")
            self.assertNotIn("etlp_release.json", actual)
            self.assertTrue(any(name.endswith(".whl") for name in actual))
            self.assertFalse(any(name.casefold().endswith(".md") for name in actual))
            self.assertFalse(any(name.endswith(".proto") for name in actual))

            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8").strip().lower(),
                f"{digest}  {archive_path.name}",
            )

            missing_version_output = output / "missing-version"
            missing_version = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "package_beta.ps1"),
                    "-OutputDirectory",
                    str(missing_version_output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotEqual(missing_version.returncode, 0)
            self.assertFalse(
                (missing_version_output / "etlp-remote-control-beta.zip").exists())

            stable_output = output / "stable-from-beta"
            stable = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "package_stable.ps1"),
                    "-OutputDirectory",
                    str(stable_output),
                    "-ReleaseVersion",
                    "2026.08.14",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotEqual(stable.returncode, 0)
            self.assertFalse((stable_output / "etlp-remote-control-stable.zip").exists())


if __name__ == "__main__":
    unittest.main()
