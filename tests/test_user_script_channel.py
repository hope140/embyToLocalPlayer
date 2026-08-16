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
        self.assertNotIn("tree/beta", source)
        self.assertIn("tree/stable#faq", source)

    def test_playback_paths_force_refresh_cached_metadata(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "async function apiClientGetWithCache(itemId, cacheList, funName, forceRefresh = false)",
            source,
        )
        self.assertIn("if (!forceRefresh) {", source)
        self.assertIn("cache[itemId] = resInfo;", source)

        playback_match = re.search(
            r"async function dealWithPlaybackInfo\(.*?(?=\n    async function deailWithItemInfo)",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(playback_match)
        playback_body = playback_match.group(0)
        self.assertGreaterEqual(playback_body.count("getPlaybackWithCace(itemId, true)"), 2)
        self.assertGreaterEqual(playback_body.count("getItemInfoWithCace(itemId, true)"), 2)

        item_match = re.search(
            r"async function deailWithItemInfo\(.*?(?=\n    document\.addEventListener)",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(item_match)
        item_body = item_match.group(0)
        self.assertIn("getItemInfoWithCace(itemId, true)", item_body)
        self.assertIn("getPlaybackWithCace(itemId, true)", item_body)

        self.assertIn(
            "resumeIds.map(id => getFun(id))",
            source,
        )

    def test_launcher_updates_the_current_channel(self):
        launcher = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn("update current release channel", launcher)
        self.assertNotIn("update to latest version", launcher)

    def test_launcher_uses_safe_command_quoting_and_choice_syntax(self):
        launcher = LAUNCHER.read_text(encoding="utf-8-sig")
        self.assertIn('set "pythonPath=python"', launcher)
        self.assertIn('set "pythonEmbed=%~dp0python_embed\\python.exe"', launcher)
        self.assertIn('if exist "%pythonEmbed%"', launcher)
        self.assertIn('for /F "usebackq tokens=*" %%A in (`"%pythonPath%" --version', launcher)
        self.assertIn('if errorlevel 6 goto six', launcher.lower())
        self.assertNotIn("IF ERRORLEVEL ==6", launcher)
        self.assertIn('"%pythonPath%" "%~dp0utils\\update.py"', launcher)
        self.assertIn('"%pythonPath%" "%~dp0embyToLocalPlayer.py"', launcher)

    def test_packager_normalises_all_channel_metadata_and_fails_closed(self):
        packager = PACKAGER.read_text(encoding="utf-8")
        for name in ("@updateURL", "@downloadURL", "@homepageURL", "@supportURL"):
            self.assertIn(f"Name = '{name}'", packager)
        self.assertIn("must occur exactly once", packager)
        self.assertIn("$Channel/user_script/embyToLocalPlayer.user.js", packager)
        self.assertIn("tree/$Channel#faq", packager)


if __name__ == "__main__":
    unittest.main()
