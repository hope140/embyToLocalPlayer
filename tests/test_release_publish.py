import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


def _write_embedded_runtime_fixture(parent: Path) -> Path:
    runtime = parent / "python_embed-source"
    (runtime / "Lib" / "site-packages").mkdir(parents=True)
    (runtime / "python.exe").write_bytes(b"embedded-python-fixture")
    (runtime / "python39.dll").write_bytes(b"embedded-python-dll-fixture")
    (runtime / "python39._pth").write_text(
        "python39.zip\n.\nLib\nLib/site-packages\n", encoding="ascii"
    )
    return runtime


class ReleasePublishTests(unittest.TestCase):
    def run_script(self, *arguments):
        shell = powershell()
        if shell is None:
            self.skipTest("PowerShell is required to execute release_publish.ps1")
        return subprocess.run(
            [
                shell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "release_publish.ps1"),
                *arguments,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def test_invalid_plan_is_rejected_before_remote_access(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = Path(temp) / "release-plan.json"
            plan.write_text(json.dumps({"schema": 1}), encoding="utf-8")
            result = self.run_script("-PlanPath", str(plan))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("缺少字段", result.stdout + result.stderr)

    def test_beta_dry_run_validates_a_real_prepared_plan(self):
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip()
        if branch != "beta":
            self.skipTest(
                f"successful prepare/publish smoke test requires the beta branch (current: {branch})"
            )

        shell = powershell()
        if shell is None:
            self.skipTest("PowerShell is required to execute release scripts")

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            runtime = _write_embedded_runtime_fixture(output)
            prepare = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "scripts" / "release_prepare.ps1"),
                    "-Version",
                    "2099.01.01.2-beta",
                    "-OutputDirectory",
                    str(output),
                    "-PythonEmbedDirectory",
                    str(runtime),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stdout + prepare.stderr)

            result = self.run_script(
                "-PlanPath", str(output / "release-plan.json")
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertIn("--prerelease", result.stdout)
            self.assertIn("no gh command", result.stdout)


if __name__ == "__main__":
    unittest.main()
