import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE_TEST = ROOT / "tests" / "user_script_security.test.cjs"


class UserScriptSecurityTests(unittest.TestCase):
    def test_node_security_suite(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for user-script security tests")
        result = subprocess.run(
            [node, "--test", str(NODE_TEST)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
