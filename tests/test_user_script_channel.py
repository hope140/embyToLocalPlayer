import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "user_script" / "embyToLocalPlayer.user.js"
LAUNCHER = ROOT / "utils" / "others" / "embyToLocalPlayer_debug.bat"
PACKAGER = ROOT / "scripts" / "package_release.ps1"


class UserScriptChannelTests(unittest.TestCase):
    def test_stable_source_metadata_is_explicit_and_complete(self):
        source = SCRIPT.read_text(encoding="utf-8")
        metadata = {}
        for name in ("updateURL", "downloadURL", "homepageURL", "supportURL"):
            matches = re.findall(rf"^// @{name}[ \t]+([^\r\n]+)$", source, re.MULTILINE)
            self.assertEqual(len(matches), 1, name)
            metadata[name] = matches[0].strip()

        self.assertEqual(
            metadata["updateURL"],
            "https://raw.githubusercontent.com/hope140/embyToLocalPlayer/stable/user_script/embyToLocalPlayer.user.js",
        )
        self.assertEqual(metadata["downloadURL"], metadata["updateURL"])
        self.assertEqual(
            metadata["homepageURL"],
            "https://github.com/hope140/embyToLocalPlayer/tree/stable",
        )
        self.assertEqual(
            metadata["supportURL"],
            "https://github.com/hope140/embyToLocalPlayer/tree/stable#faq",
        )
        self.assertNotIn("releases/latest", source)
        self.assertNotIn("/main/", source)
        self.assertNotIn("/beta/", source)

    def test_launcher_updates_the_current_channel(self):
        launcher = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn("update current release channel", launcher)
        self.assertNotIn("update to latest version", launcher)

    def test_packager_normalises_all_channel_metadata_and_fails_closed(self):
        packager = PACKAGER.read_text(encoding="utf-8")
        for name in ("@updateURL", "@downloadURL", "@homepageURL", "@supportURL"):
            self.assertIn(f"Name = '{name}'", packager)
        self.assertIn("must occur exactly once", packager)
        self.assertIn("$Channel/user_script/embyToLocalPlayer.user.js", packager)
        self.assertIn("tree/$Channel#faq", packager)


if __name__ == "__main__":
    unittest.main()
