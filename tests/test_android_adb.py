import subprocess
import tempfile
import unittest
from pathlib import Path

from android_adb import AndroidAdb
from jarvis_mark2 import Mark2Runtime


class FakeAdb:
    def __init__(self, foreground="com.example.notes", installed=True, launch_ok=True):
        self.calls = []
        self.foreground = foreground
        self.installed = installed
        self.launch_ok = launch_ok

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        tail = command[3:]
        if tail == ["get-state"]:
            return subprocess.CompletedProcess(command, 0, "device\n", "")
        if tail[:4] == ["shell", "pm", "list", "packages"]:
            return subprocess.CompletedProcess(command, 0, "package:com.example.notes\npackage:com.example.music\n", "")
        if tail[:3] == ["shell", "pm", "path"]:
            output = "package:/data/app/base.apk\n" if self.installed else ""
            return subprocess.CompletedProcess(command, 0, output, "")
        if tail[:3] == ["shell", "am", "start"]:
            output = "Starting: Intent" if self.launch_ok else "Error: Activity not started"
            return subprocess.CompletedProcess(command, 0, output, "")
        if tail == ["shell", "dumpsys", "activity", "activities"]:
            output = f"topResumedActivity=ActivityRecord{{123 u0 {self.foreground}/.MainActivity t4}}"
            return subprocess.CompletedProcess(command, 0, output, "")
        raise AssertionError(f"unexpected ADB command: {command}")


class AndroidAdbTests(unittest.TestCase):
    def test_unconfigured_phone_reports_truthfully_without_commands(self):
        fake = FakeAdb()
        bridge = AndroidAdb(adb_path="", serial="", runner=fake)
        self.assertIn("ADB Platform-Tools not found", bridge.status())
        self.assertIn("no action sent", bridge.launch_app("com.example.notes"))
        self.assertEqual(fake.calls, [])

    def test_exact_serial_and_installed_package_verified_after_launch(self):
        fake = FakeAdb()
        bridge = AndroidAdb(adb_path="adb.exe", serial="phone-123", runner=fake)
        self.assertIn("connected", bridge.status())
        self.assertIn("com.example.notes", bridge.find_apps("example"))
        self.assertEqual(bridge.launch_app("com.example.notes"),
                         "Verified Android foreground package com.example.notes.")
        self.assertTrue(all(call[1:3] == ["-s", "phone-123"] for call in fake.calls))

    def test_missing_app_and_malformed_input_never_launch(self):
        fake = FakeAdb(installed=False)
        bridge = AndroidAdb(adb_path="adb.exe", serial="phone-123", runner=fake)
        self.assertIn("no action sent", bridge.launch_app("com.bad;reboot"))
        self.assertIn("not installed", bridge.launch_app("com.example.notes"))
        self.assertFalse(any(call[3:6] == ["shell", "am", "start"] for call in fake.calls))

    def test_wrong_foreground_is_unverified_not_success(self):
        fake = FakeAdb(foreground="com.example.other")
        bridge = AndroidAdb(adb_path="adb.exe", serial="phone-123", runner=fake)
        self.assertIn("unverified", bridge.launch_app("com.example.notes"))

    def test_runtime_contract_and_audit_redact_android_details(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Mark2Runtime(Path(folder))
            runtime.android = AndroidAdb(adb_path="adb.exe", serial="phone-123", runner=FakeAdb())
            result = runtime.execute("launch_android_app", {"package": "com.example.notes"})
            self.assertIn("Verified Android foreground package", result)
            runtime.audit("launch_android_app", {"package": "com.example.notes"}, result)
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("com.example.notes", audit)
            self.assertNotIn("phone-123", audit)


if __name__ == "__main__":
    unittest.main()
