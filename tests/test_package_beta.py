import hashlib
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
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
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

            self.assertEqual(actual, expected)
            self.assertTrue(any(name.endswith(".whl") for name in actual))
            self.assertFalse(any(name.casefold().endswith(".md") for name in actual))
            self.assertFalse(any(name.endswith(".proto") for name in actual))

            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            self.assertEqual(
                checksum_path.read_text(encoding="utf-8").strip().lower(),
                f"{digest}  {archive_path.name}",
            )


if __name__ == "__main__":
    unittest.main()
