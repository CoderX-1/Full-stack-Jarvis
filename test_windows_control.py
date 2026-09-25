import ctypes
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis_mark2 import MARK2_TOOL_NAMES, Mark2Runtime
from verification_engine import VerificationEngine
from windows_control import WindowsControl, _norm, _ps_quote
from windows_vision import VisionObservation, VisionWord, WindowsVision


class WindowsControlContractTests(unittest.TestCase):
    def test_closed_visual_target_satisfies_action_verification_contract(self):
        result = (
            "Verified visual action: target window closed after click on 'Don\'t Save'; "
            "selector=uia; attempts=1"
        )
        engine = VerificationEngine()
        enforced = engine.enforce(
            "click_visual_text",
            {"expect_absent_text": "Don't Save"},
            result,
        )
        decision = engine.evaluate(
            "click_visual_text",
            {"expect_absent_text": "Don't Save"},
            enforced,
        )
        self.assertEqual(enforced, result)
        self.assertEqual(decision.status, "verified")
        self.assertTrue(decision.goal_verified)

    def test_focus_does_not_send_alt_or_other_keys(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control.kernel32 = Mock()
        control.user32.IsIconic.return_value = False
        control.user32.GetForegroundWindow.side_effect = [5, 10, 10]
        control.user32.GetWindowThreadProcessId.side_effect = [20, 30]
        control.user32.AttachThreadInput.return_value = True
        control.kernel32.GetCurrentThreadId.return_value = 40
        with patch("time.sleep"):
            self.assertTrue(control._focus_handle(10))
        control.user32.keybd_event.assert_not_called()

    def test_type_text_requires_same_target_and_content_verification(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={
            "handle": 10, "pid": 20, "title": "Untitled - Notepad",
        })
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.VkKeyScanW.side_effect = lambda char: ord(char.upper())
        control._focused_text_state = Mock(side_effect=[
            {"pid": 20, "password": False, "supported": True, "text": ""},
            {"pid": 20, "password": False, "supported": True, "text": "hello Ayaan"},
        ])
        with patch("time.sleep"):
            result = control.type_text("hello Ayaan", "Notepad", 0)
        self.assertIn("Typed and verified", result)
        self.assertIn("focused-control content", result)

    def test_type_text_rejects_unchanged_preexisting_content(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={
            "handle": 10, "pid": 20, "title": "Untitled - Notepad",
        })
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.VkKeyScanW.side_effect = lambda char: ord(char.upper())
        unchanged = {"pid": 20, "password": False, "supported": True, "text": "hello"}
        control._focused_text_state = Mock(side_effect=[unchanged, unchanged])
        with patch("time.sleep"):
            result = control.type_text("hello", "Notepad", 0)
        self.assertIn("not verified", result)

    def test_type_text_uses_safe_window_accessibility_fallback(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={
            "handle": 10, "pid": 20, "title": "Untitled - Notepad",
        })
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.VkKeyScanW.side_effect = lambda char: ord(char.upper())
        unsupported = {"pid": 20, "password": False, "supported": False, "text": ""}
        control._focused_text_state = Mock(side_effect=[unsupported, unsupported])
        control.ui_elements = Mock(side_effect=[
            [{"name": "", "id": "15"}],
            [{"name": "hello Ayaan", "id": "15"}],
        ])
        with patch("time.sleep"):
            result = control.type_text("hello Ayaan", "Notepad", 0)
        self.assertIn("Typed and verified", result)
        self.assertIn("window accessibility text", result)

    def test_type_text_uses_clipboard_free_unicode_sendinput_fallback(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={
            "handle": 10, "pid": 20, "title": "Untitled - Notepad",
        })
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.VkKeyScanW.return_value = -1
        control.user32.SendInput.side_effect = lambda count, _events, _size: count
        control._focused_text_state = Mock(side_effect=[
            {"pid": 20, "password": False, "supported": True, "text": ""},
            {"pid": 20, "password": False, "supported": True, "text": "Ω🙂"},
        ])
        with patch("time.sleep"):
            result = control.type_text("Ω🙂", "Notepad", 0)
        self.assertIn("Typed and verified 2 character(s)", result)
        self.assertIn("unicode_fallback=2", result)
        self.assertIn("clipboard=untouched", result)
        self.assertEqual(control.user32.SendInput.call_count, 2)
        self.assertEqual(control.user32.SendInput.call_args_list[0].args[0], 2)
        self.assertEqual(control.user32.SendInput.call_args_list[1].args[0], 4)
        control.user32.keybd_event.assert_not_called()

    def test_type_text_rejects_non_printing_control_before_delivery(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        with self.assertRaisesRegex(ValueError, "Unsupported control character"):
            control.type_text("safe\x00unsafe", "Notepad", 0)
        control.user32.SendInput.assert_not_called()
        control.user32.keybd_event.assert_not_called()

    def test_type_text_routes_vk_scan_argument_error_to_unicode(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={
            "handle": 10, "pid": 20, "title": "Untitled - Notepad",
        })
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.VkKeyScanW.side_effect = ctypes.ArgumentError("UTF-16 unit required")
        control.user32.SendInput.side_effect = lambda count, _events, _size: count
        control._focused_text_state = Mock(side_effect=[
            {"pid": 20, "password": False, "supported": True, "text": ""},
            {"pid": 20, "password": False, "supported": True, "text": "🙂"},
        ])
        with patch("time.sleep"):
            result = control.type_text("🙂", "Notepad", 0)
        self.assertIn("Typed and verified 1 character(s)", result)
        self.assertIn("unicode_fallback=1", result)
        self.assertEqual(control.user32.SendInput.call_args.args[0], 4)

    def test_send_keys_validates_every_chord_before_input(self):
        control = object.__new__(WindowsControl)
        control.user32 = Mock()
        control._find_window = Mock(return_value={"handle": 10, "title": "Notepad"})
        control._focus_handle = Mock(return_value=True)
        control.user32.GetForegroundWindow.return_value = 10
        with self.assertRaisesRegex(ValueError, "Unsupported key"):
            control.send_keys("ctrl+s, literal words", "Notepad")
        control.user32.keybd_event.assert_not_called()

    def test_app_discovery_merges_start_shortcuts_aliases_and_deduplicates(self):
        control = object.__new__(WindowsControl)
        control._apps_cache = (0.0, [])
        control._run_powershell = Mock(return_value=json.dumps([
            {"Name": "Visual Studio Code", "AppID": "shell-app-id"},
            {"Name": "Visual Studio Code", "AppID": "C:\\Start\\Code.lnk"},
            {"Name": "Discord", "AppID": "C:\\Start\\Discord.lnk"},
        ]))
        apps = control._start_apps()
        code = [item for item in apps if item["name"] == "Visual Studio Code"]
        self.assertEqual(code, [{"name": "Visual Studio Code", "id": "shell-app-id"}])
        self.assertTrue(any(item["name"] == "Discord" for item in apps))
        self.assertTrue(any(item["name"] == "Calculator" for item in apps))

    def test_launch_app_uses_shell_for_shortcuts(self):
        control = object.__new__(WindowsControl)
        control._resolve_app = Mock(return_value=("Demo", "C:\\Start\\Demo.lnk"))
        control.windows = Mock(side_effect=[[], [{
            "handle": 10, "pid": 20, "title": "Demo", "process": "demo.exe",
            "foreground": True,
        }]])
        with patch("os.startfile", create=True) as startfile:
            result = control.launch_app("Demo", wait_seconds=1)
        startfile.assert_called_once_with("C:\\Start\\Demo.lnk")
        self.assertIn("Opened and verified Demo", result)

    def test_schema_resolution_routing_and_audit_redaction(self):
        expected = {
            "list_installed_apps", "list_windows", "launch_app", "open_item",
            "control_window", "send_keys", "type_text", "mouse_action",
            "media_control", "inspect_ui", "interact_ui",
            "read_window_text", "list_known_folders", "find_files",
            "vision_status", "observe_screen", "find_visual_text",
            "click_visual_text", "save_diagnostic_snapshot",
        }
        self.assertTrue(expected.issubset(MARK2_TOOL_NAMES))
        self.assertEqual(_norm("Visual Studio-Code!"), "visualstudiocode")
        self.assertEqual(_ps_quote("Ayaan's app"), "'Ayaan''s app'")

        control = object.__new__(WindowsControl)
        control._start_apps = Mock(return_value=[
            {"name": "Visual Studio Code", "id": "code"},
            {"name": "Visual Studio Installer", "id": "installer"},
        ])
        self.assertEqual(control._resolve_app("Visual Studio Code"), ("Visual Studio Code", "code"))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            control._resolve_app("Visual Studio")

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mark2Runtime(Path(tmp))
            runtime.windows_control = Mock()
            runtime.windows_control.launch_app.return_value = "opened and verified"
            self.assertEqual(
                runtime.execute("launch_app", {"name": "Notepad", "wait_seconds": 3}),
                "opened and verified",
            )
            runtime.windows_control.launch_app.assert_called_once_with("Notepad", 3)
            runtime.audit("type_text", {"text": "private words", "value": "secret value"}, "ok")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            event = json.loads(audit)
            self.assertEqual(event["args"]["text"], "[REDACTED]")
            self.assertEqual(event["args"]["value"], "[REDACTED]")
            self.assertNotIn("private words", audit)
            runtime.audit("read_window_text", {"window": "Notes"}, "private visible text")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private visible text", audit)
            self.assertIn("[REDACTED: visible window text]", audit)
            runtime.audit("click_visual_text", {"text": "private label"}, "clicked private label")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private label", audit)
            runtime.audit("interact_ui", {"value": "private UI value"}, "observed private UI value")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private UI value", audit)
            runtime.audit("send_keys", {"keys": "private shortcut payload"}, "rejected")
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("private shortcut payload", audit)

    def test_vision_locates_exact_and_fuzzy_screen_text(self):
        observation = VisionObservation(
            title="Demo", handle=1, origin_x=100, origin_y=200,
            width=800, height=600, image_hash="abc", text="Open Settings",
            lines=[[
                VisionWord("Open", 20, 30, 50, 20),
                VisionWord("Settings", 80, 30, 90, 20),
            ]],
            image=None,
        )
        exact = WindowsVision.locate(observation, "Open Settings")
        self.assertEqual(exact[0]["score"], 1.0)
        self.assertEqual((exact[0]["screen_x"], exact[0]["screen_y"]), (195, 240))
        fuzzy = WindowsVision.locate(observation, "Setings")
        self.assertGreaterEqual(fuzzy[0]["score"], 0.8)

    def test_vision_short_targets_do_not_match_substrings(self):
        observation = VisionObservation(
            title="Calculator", handle=1, origin_x=0, origin_y=0,
            width=100, height=100, image_hash="abc", text="17",
            lines=[[VisionWord("17", 10, 10, 20, 20)]], image=None,
        )
        self.assertEqual(WindowsVision.locate(observation, "7"), [])
        observation.lines.append([VisionWord("7", 40, 40, 20, 20)])
        matches = WindowsVision.locate(observation, "7")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["text"], "7")

    def test_vision_tolerates_ocr_word_split(self):
        observation = VisionObservation(
            title="Login", handle=1, origin_x=0, origin_y=0,
            width=200, height=100, image_hash="abc", text="Sign in",
            lines=[[
                VisionWord("Sign", 10, 10, 35, 20),
                VisionWord("in", 50, 10, 15, 20),
            ]], image=None,
        )
        matches = WindowsVision.locate(observation, "Sign-in")
        self.assertEqual(matches[0]["text"], "Sign in")
        self.assertEqual(matches[0]["score"], 1.0)

    def test_vision_uses_uia_bounds_when_ocr_misses_calculator_digit(self):
        observation = VisionObservation(
            title="Calculator", handle=10, origin_x=100, origin_y=200,
            width=500, height=700, image_hash="abc", text="Calculator Standard",
            lines=[], image=None, pid=20, process="CalculatorApp.exe",
        )
        control = Mock()
        control.ui_elements.return_value = [
            {
                "name": name, "id": f"num{digit}Button", "enabled": True,
                "x": 120 + digit * 35, "y": 600, "width": 30, "height": 60,
            }
            for digit, name in enumerate(
                ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
            )
        ]
        vision = WindowsVision(control, Path("."))
        matches, source = vision._locate_with_fallback(observation, "7")
        self.assertEqual(source, "uia-fallback")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["text"], "Seven")
        self.assertEqual((matches[0]["screen_x"], matches[0]["screen_y"]), (380, 630))

    def test_visual_click_can_verify_uia_digit_fallback(self):
        before = VisionObservation(
            title="Calculator", handle=10, origin_x=100, origin_y=200,
            width=500, height=700, image_hash="before", text="Display is 0",
            lines=[], image=None, pid=20, process="CalculatorApp.exe",
        )
        after = VisionObservation(
            title="Calculator", handle=10, origin_x=100, origin_y=200,
            width=500, height=700, image_hash="after", text="Display is 7",
            lines=[], image=None, pid=20, process="CalculatorApp.exe",
        )
        current = {
            "handle": 10, "pid": 20, "process": "CalculatorApp.exe",
            "title": "Calculator", "rect": [100, 200, 600, 900], "foreground": True,
        }
        control = Mock()
        control.ui_elements.return_value = [{
            "name": "Seven", "id": "num7Button", "enabled": True,
            "x": 220, "y": 600, "width": 80, "height": 60,
        }]
        control.windows.side_effect = [[current], [current]]
        control.user32.GetForegroundWindow.return_value = 10
        control.user32.WindowFromPoint.return_value = 11
        control.user32.GetAncestor.return_value = 10
        control.mouse_action.return_value = "mouse verified"
        vision = WindowsVision(control, Path("."))
        vision.observe = Mock(side_effect=[before, before, after])
        vision._change_ratio = Mock(return_value=0.02)
        with patch("time.sleep"):
            result = vision.click_visual_text("Calculator", "7")
        self.assertIn("via uia-fallback", result)
        self.assertIn("ocr_changed=True", result)
        control.mouse_action.assert_called_once_with("click", 260, 630, "left")

    def test_vision_does_not_expand_exact_target_into_neighbor_words(self):
        observation = VisionObservation(
            title="Demo", handle=1, origin_x=0, origin_y=0,
            width=200, height=100, image_hash="abc", text="Open Settings",
            lines=[[
                VisionWord("Open", 10, 10, 35, 20),
                VisionWord("Settings", 50, 10, 70, 20),
            ]], image=None,
        )
        matches = WindowsVision.locate(observation, "Settings")
        self.assertEqual([item["text"] for item in matches], ["Settings"])

    def test_scaled_ocr_boxes_restore_native_coordinates(self):
        restored = WindowsVision._restore_coordinates(
            [[VisionWord("Button", 100, 50, 40, 10)]], 0.5
        )
        self.assertEqual(restored[0][0].rect, (200, 100, 280, 120))

    def test_visual_click_refuses_stale_foreground(self):
        observation = VisionObservation(
            title="Demo", handle=10, origin_x=0, origin_y=0,
            width=100, height=100, image_hash="abc", text="Go",
            lines=[[VisionWord("Go", 10, 10, 20, 20)]], image=None,
            pid=20, process="demo.exe",
        )

        class User32:
            @staticmethod
            def GetForegroundWindow():
                return 99

        class Control:
            user32 = User32()
            clicked = False

            @staticmethod
            def windows():
                return [{"handle": 10, "rect": [0, 0, 100, 100]}]

            def mouse_action(self, *_args):
                self.clicked = True
                return "clicked"

        control = Control()
        vision = WindowsVision(control, Path("."))
        vision.observe = Mock(return_value=observation)
        result = vision.click_visual_text("Demo", "Go")
        self.assertIn("foreground window changed", result)
        self.assertFalse(control.clicked)

    def test_visual_click_refuses_out_of_window_ocr_coordinates(self):
        observation = VisionObservation(
            title="Demo", handle=10, origin_x=100, origin_y=100,
            width=100, height=100, image_hash="abc", text="Go",
            lines=[[VisionWord("Go", 200, 10, 20, 20)]], image=None,
            pid=20, process="demo.exe",
        )

        class Control:
            clicked = False

            def mouse_action(self, *_args):
                self.clicked = True
                return "clicked"

        control = Control()
        vision = WindowsVision(control, Path("."))
        vision.observe = Mock(return_value=observation)
        result = vision.click_visual_text("Demo", "Go")
        self.assertIn("outside the observed window", result)
        self.assertFalse(control.clicked)

    def test_visual_click_refuses_occluded_target(self):
        observation = VisionObservation(
            title="Demo", handle=10, origin_x=0, origin_y=0,
            width=100, height=100, image_hash="abc", text="Go",
            lines=[[VisionWord("Go", 10, 10, 20, 20)]], image=None,
            pid=20, process="demo.exe",
        )

        class User32:
            @staticmethod
            def GetForegroundWindow():
                return 10

            @staticmethod
            def WindowFromPoint(_point):
                return 99

            @staticmethod
            def GetAncestor(handle, _flag):
                return handle

        class Control:
            user32 = User32()
            clicked = False

            @staticmethod
            def windows():
                return [{"handle": 10, "rect": [0, 0, 100, 100]}]

            def mouse_action(self, *_args):
                self.clicked = True
                return "clicked"

        control = Control()
        vision = WindowsVision(control, Path("."))
        vision.observe = Mock(return_value=observation)
        result = vision.click_visual_text("Demo", "Go")
        self.assertIn("another window covers", result)
        self.assertFalse(control.clicked)

    def test_mouse_coordinates_support_negative_virtual_desktop(self):
        class User32:
            point = (-100, 200)

            @staticmethod
            def GetSystemMetrics(index):
                return {76: -1920, 77: 0, 78: 3840, 79: 1080, 0: 1920, 1: 1080}[index]

            def SetCursorPos(self, x, y):
                self.point = (x, y)
                return True

            def GetCursorPos(self, pointer):
                pointer._obj.x, pointer._obj.y = self.point
                return True

        control = object.__new__(WindowsControl)
        control.user32 = User32()
        control.windows = Mock(return_value=[])
        result = control.mouse_action("move", -100, 200)
        self.assertIn("(-100,200)", result)

    def test_window_inventory_tolerates_no_foreground_window(self):
        class User32:
            @staticmethod
            def GetForegroundWindow():
                return None

            @staticmethod
            def EnumWindows(_callback, _lparam):
                return True

        control = object.__new__(WindowsControl)
        control.user32 = User32()
        self.assertEqual(control.windows(), [])

    def test_known_folders_uses_valid_registry_script_and_custom_paths(self):
        control = object.__new__(WindowsControl)
        control._run_powershell = Mock(return_value=json.dumps({
            "Desktop": "C:\\Cloud\\Desktop",
            "{374DE290-123F-4565-9164-39C4925E467B}": "C:\\Cloud\\Downloads",
        }))
        folders = control.known_folders()
        script = control._run_powershell.call_args.args[0]
        self.assertIn("$out=@{}", script)
        self.assertNotIn("@{{}}", script)
        self.assertEqual(folders["desktop"], Path("C:/Cloud/Desktop"))
        self.assertEqual(folders["downloads"], Path("C:/Cloud/Downloads"))


if __name__ == "__main__":
    unittest.main()
