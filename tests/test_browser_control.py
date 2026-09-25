import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_control import BrowserControl


class BrowserControlSafetyTests(unittest.TestCase):
    def test_normalize_url_search_and_default(self):
        self.assertEqual(BrowserControl.normalize_target(""), "https://www.google.com/")
        self.assertEqual(BrowserControl.normalize_target("example.com/docs"), "https://example.com/docs")
        self.assertEqual(
            BrowserControl.normalize_target("jarvis browser verification"),
            "https://www.google.com/search?q=jarvis+browser+verification",
        )

    def test_unsafe_urls_and_credentials_are_rejected(self):
        for value in ("file:///c:/secret.txt", "javascript:alert(1)", "data:text/plain,x"):
            with self.assertRaises(ValueError):
                BrowserControl._validate_url(value)
        with self.assertRaises(ValueError):
            BrowserControl._validate_url("https://user:pass@example.com/")

    def test_browser_child_environment_drops_secrets(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "never-in-browser",
            "FISH_API_KEY": "never-in-browser",
            "PATH": "safe-path",
        }, clear=True):
            child = BrowserControl._browser_env()
        self.assertEqual(child["PATH"], "safe-path")
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("FISH_API_KEY", child)

    def test_youtube_selection_ignores_shorts_and_canonicalizes(self):
        result = BrowserControl.first_watch_url(
            "https://www.youtube.com/results?search_query=test",
            ["/shorts/abc12345", "/watch?v=good_ID-123&list=private", "/watch?v=later123"],
        )
        self.assertEqual(result, "https://www.youtube.com/watch?v=good_ID-123")

    def test_status_does_not_launch_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = BrowserControl(Path(tmp), executable_path=str(Path(tmp) / "missing.exe"))
            status = engine.status()
            self.assertIn("session_active=false", status)
            self.assertIn("dedicated_profile=true", status)
            self.assertIsNone(engine._thread)

    def test_browser_command_parsing_and_engine_detection(self):
        executable = BrowserControl._parse_executable(
            '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --single-argument %1'
        )
        self.assertEqual(executable.name, "chrome.exe")
        self.assertEqual(BrowserControl._engine_for(executable, "ChromeHTML"), "chromium")
        self.assertEqual(
            BrowserControl._engine_for(Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"), "FirefoxURL"),
            "firefox-system",
        )

    def test_default_browser_has_its_own_isolated_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "msedge.exe"
            executable.touch()
            engine = BrowserControl(Path(tmp), executable_path=str(executable))
            self.assertEqual(engine.browser.display_name, "Microsoft Edge")
            self.assertEqual(engine.profile_dir.name, "msedge")
            self.assertIn("system_default=false", engine.status())


if __name__ == "__main__":
    unittest.main()
