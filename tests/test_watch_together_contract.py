import configparser
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WatchTogetherUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = ROOT / "embyToLocalPlayer_config.ini"
        cls.userscript_path = ROOT / "user_script" / "embyToLocalPlayer.user.js"
        cls.readme_path = ROOT / "README.md"
        cls.functions_path = ROOT / "FUNCTIONS.md"
        cls.backend_path = ROOT / "utils" / "watch_together_coordinator.py"
        cls.config_text = cls.config_path.read_text(encoding="utf-8")
        cls.userscript = cls.userscript_path.read_text(encoding="utf-8")
        cls.readme = cls.readme_path.read_text(encoding="utf-8")
        cls.functions = cls.functions_path.read_text(encoding="utf-8")
        cls.backend = cls.backend_path.read_text(encoding="utf-8")

    def test_ini_template_is_disabled_and_has_only_expected_fields(self):
        self.assertEqual(self.config_text.lower().count("[watch_together]"), 1)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(self.config_text)
        self.assertTrue(parser.has_section("watch_together"))
        section = parser["watch_together"]
        self.assertEqual(
            set(section), {"enable", "admin_enable", "server_url", "admin_api_key"}
        )
        self.assertEqual(section.get("enable"), "no")
        self.assertEqual(section.get("admin_enable"), "no")
        self.assertEqual(section.get("server_url"), "")
        self.assertEqual(section.get("admin_api_key"), "")

    def test_userscript_metadata_menu_endpoints_and_secret_scope(self):
        self.assertRegex(self.userscript, r"@version\s+2026\.08\.04\.1\b")
        update_url = "https://raw.githubusercontent.com/hope140/embyToLocalPlayer/watch_together/user_script/embyToLocalPlayer.user.js"
        self.assertIn(f"// @updateURL    {update_url}", self.userscript)
        self.assertIn(f"// @downloadURL  {update_url}", self.userscript)
        self.assertIn("2026.08.04-watch_together.1", self.backend_path.parent.joinpath("tools.py").read_text(encoding="utf-8"))
        self.assertIn("同步观看房间", self.userscript)
        endpoints = (
            "/watch-together/auth",
            "/watch-together/rooms/list",
            "/watch-together/rooms/create",
            "/watch-together/rooms/action",
            "/watch-together/rooms/delete",
        )
        for endpoint in endpoints:
            self.assertIn(endpoint, self.userscript)
            self.assertIn(endpoint, self.backend)
        self.assertIn("X-ETLP-Watch-Token", self.userscript)
        self.assertNotIn("PartyService", self.userscript)

        start_marker = "// --- watch-together administrator UI"
        end_marker = "// --- end watch-together administrator UI ---"
        start = self.userscript.index(start_marker)
        end = self.userscript.index(end_marker, start)
        watch_code = self.userscript[start:end]
        # Watch credentials and the short token must never use existing script
        # persistence mechanisms or unsafe HTML rendering.
        self.assertNotRegex(watch_code, r"\blocalStorage\b|\bGM_setValue\b|\bGM_getValue\b")
        self.assertNotIn("innerHTML", watch_code)
        self.assertIn("textContent", watch_code)
        self.assertIn("Escape", watch_code)
        self.assertIn("event.target === overlay", watch_code)
        self.assertIn("parsed.username", watch_code)
        self.assertIn("parsed.password", watch_code)
        self.assertIn("code === 'server_mismatch'", watch_code)
        self.assertIn("当前 Emby 实际服务器与 INI", watch_code)

        render_match = re.search(
            r"function watchTogetherRenderUsers\(.*?(?=\n    function watchTogetherStatusLabel)",
            watch_code,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(render_match)
        self.assertNotIn("addEventListener", render_match.group(0))
        self.assertEqual(watch_code.count("state.userA.addEventListener('change'"), 1)
        self.assertEqual(watch_code.count("state.userB.addEventListener('change'"), 1)
        self.assertGreaterEqual(watch_code.count("watchTogetherBindUserSelects"), 2)

    def test_docs_cover_setup_scope_security_and_navigation(self):
        required_readme = (
            "同步观看（实验）",
            "requirements.txt",
            "websocket-client==1.8.0",
            "内置",
            "实际运行 etlp 的 Python",
            "enable = yes",
            "admin_enable = yes",
            "admin_api_key",
            "ItemId",
            "MediaSourceId",
            "mpv/IINA",
            "PartyService",
            "watch_together_rooms.json",
            "loopback",
            "admin key",
        )
        for phrase in required_readme:
            self.assertIn(phrase, self.readme)

        section_match = re.search(
            r"### 4\.12 同步观看房间(?P<section>.*?)(?=### 4\.13 )",
            self.functions,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(section_match)
        section = section_match.group("section")
        for phrase in (
            "[实验]",
            "[watch_together]",
            "仅 Emby",
            "mpv/IINA",
            "PartyService",
            "loopback",
            "watch_together_rooms.json",
        ):
            self.assertIn(phrase, section)
        self.assertIn("`[watch_together]`", self.functions)
        self.assertIn("修改同步观看房间", self.functions)


if __name__ == "__main__":
    unittest.main()
