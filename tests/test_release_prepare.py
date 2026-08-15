import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_prepare.ps1"


def _powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


class ReleasePrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = _powershell()
        if cls.shell is None:
            raise unittest.SkipTest("PowerShell is required to execute release_prepare.ps1")
        cls.branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip()

    def _run(self, *arguments):
        return subprocess.run(
            [
                self.shell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SCRIPT),
                *arguments,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def test_wrong_branch_is_rejected(self):
        channel = "stable" if self.branch == "beta" else "beta"
        version = "2026.08.15" if channel == "stable" else "2026.08.15.1-beta"
        with tempfile.TemporaryDirectory() as temp:
            result = self._run(
                "-Version",
                version,
                "-Channel",
                channel,
                "-OutputDirectory",
                temp,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertTrue(
            "拒绝从分支" in combined or "无法从当前分支" in combined,
            combined,
        )

    def test_dirty_worktree_is_rejected_when_on_release_branch(self):
        if self.branch not in {"beta", "stable"}:
            self.skipTest("dirty-worktree check requires a beta or stable checkout")
        version = "2026.08.15.1-beta" if self.branch == "beta" else "2026.08.15"
        marker = ROOT / ".release-prepare-test-dirty"
        try:
            marker.write_text("temporary test marker\n", encoding="utf-8")
            result = self._run(
                "-Version",
                version,
                "-OutputDirectory",
                str(ROOT / "publish" / "release-prepare-dirty-test"),
            )
        finally:
            if marker.exists():
                marker.unlink()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("工作区不是干净状态", result.stdout + result.stderr)

    def test_beta_prepare_writes_plan_from_actual_archive(self):
        if self.branch != "beta":
            self.skipTest("successful beta smoke test requires the beta branch")
        if not list((ROOT / "third_party").glob("*.whl")):
            self.skipTest("bundled Python wheels are unavailable")

        with tempfile.TemporaryDirectory() as temp:
            result = self._run(
                "-Version",
                "2099.01.01.1-beta",
                "-OutputDirectory",
                temp,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            output = Path(temp)
            archive = output / "etlp-remote-control-beta.zip"
            checksum = Path(f"{archive}.sha256")
            plan_path = output / "release-plan.json"
            notes_path = output / "release-notes.md"
            self.assertTrue(archive.is_file())
            self.assertTrue(checksum.is_file())
            self.assertTrue(plan_path.is_file())
            self.assertTrue(notes_path.is_file())

            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(plan["schema"], 1)
            self.assertEqual(plan["channel"], "beta")
            self.assertEqual(plan["version"], "2099.01.01.1-beta")
            self.assertEqual(plan["branch"], "beta")
            self.assertEqual(plan["packageAsset"], archive.name)
            self.assertEqual(plan["checksumAsset"], checksum.name)
            self.assertEqual(plan["packageSha256"], digest)
            self.assertEqual(plan["packageSize"], archive.stat().st_size)
            self.assertIn("# ETLP Release 发布说明草稿", notes_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
