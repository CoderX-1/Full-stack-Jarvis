import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from windows_control import WindowsControl


class WindowsAppDiscoveryTests(unittest.TestCase):
    def test_missing_alias_is_not_listed_or_resolved_as_installed(self) -> None:
        control = object.__new__(WindowsControl)
        control._apps_cache = (0.0, [])
        control._run_powershell = Mock(return_value=json.dumps([
            {"Name": "Demo App", "AppID": "Demo.Package!App"},
        ]))
        control._available_alias_target = Mock(return_value="")
        apps = control._start_apps()
        self.assertEqual(apps, [{"name": "Demo App", "id": "Demo.Package!App"}])
        self.assertIn("No installed app matched", control.list_installed_apps("paint"))
        with self.assertRaisesRegex(ValueError, "not installed or was not found"):
            control._resolve_app("paint")

    def test_executable_alias_requires_a_resolved_path(self) -> None:
        with patch("windows_control.shutil.which", return_value=r"C:\Windows\System32\mspaint.exe"):
            self.assertEqual(
                WindowsControl._available_alias_target("mspaint.exe"),
                r"C:\Windows\System32\mspaint.exe",
            )
        with patch("windows_control.shutil.which", return_value=None):
            self.assertEqual(WindowsControl._available_alias_target("mspaint.exe"), "")

    def test_stale_shortcut_refuses_before_launch(self) -> None:
        control = object.__new__(WindowsControl)
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "not-installed.lnk"
            control._resolve_app = Mock(return_value=("Missing App", str(missing)))
            control.windows = Mock()
            with patch("windows_control.subprocess.Popen") as popen, patch("os.startfile", create=True) as startfile:
                result = control.launch_app("Missing App")
        self.assertIn("no launch sent", result)
        control.windows.assert_not_called()
        popen.assert_not_called()
        startfile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
