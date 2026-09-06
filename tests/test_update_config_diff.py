import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path

from utils.update import check_ini_diff


class CheckIniDiffTests(unittest.TestCase):
    def _run_diff(self, old_text, new_text):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        old_path = root / "old.ini"
        new_path = root / "new.ini"
        diff_path = root / "diff.ini"
        old_path.write_text(old_text, encoding="utf-8")
        new_path.write_text(new_text, encoding="utf-8")
        check_ini_diff(old_path, new_path, diff_path)
        return diff_path

    def _read_diff(self, diff_path):
        diff = ConfigParser(allow_no_value=True)
        diff.read(diff_path, encoding="utf-8-sig")
        return diff

    def test_new_nonempty_section_is_written(self):
        diff_path = self._run_diff(
            "[existing]\nunchanged = keep\n",
            "[existing]\nunchanged = keep\n[new_section]\nnew_key = new_value\n",
        )

        self.assertTrue(diff_path.exists())
        diff = self._read_diff(diff_path)
        self.assertEqual(diff.sections(), ["new_section"])
        self.assertEqual(dict(diff["new_section"]), {"new_key": "new_value"})

    def test_new_section_and_existing_change_are_written(self):
        diff_path = self._run_diff(
            "[existing]\nchanged = old\nunchanged = keep\n",
            "[existing]\nchanged = new\nunchanged = keep\n[new_section]\nnew_key = new_value\n",
        )

        diff = self._read_diff(diff_path)
        self.assertEqual(diff.sections(), ["existing", "new_section"])
        self.assertEqual(dict(diff["existing"]), {"changed": "new"})
        self.assertEqual(dict(diff["new_section"]), {"new_key": "new_value"})

    def test_existing_key_change_is_written(self):
        diff_path = self._run_diff(
            "[existing]\nchanged = old\n",
            "[existing]\nchanged = new\n",
        )

        diff = self._read_diff(diff_path)
        self.assertEqual(dict(diff["existing"]), {"changed": "new"})

    def test_unchanged_input_does_not_create_diff(self):
        diff_path = self._run_diff(
            "[existing]\nunchanged = keep\n",
            "[existing]\nunchanged = keep\n",
        )

        self.assertFalse(diff_path.exists())

    def test_existing_unchanged_keys_are_omitted(self):
        diff_path = self._run_diff(
            "[existing]\nchanged = old\nunchanged = keep\n",
            "[existing]\nchanged = new\nunchanged = keep\n",
        )

        diff = self._read_diff(diff_path)
        self.assertEqual(dict(diff["existing"]), {"changed": "new"})


if __name__ == "__main__":
    unittest.main()
