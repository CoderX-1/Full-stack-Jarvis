"""Bounded Android ADB adapter; never treats a sent command as success.

No raw shell, install, uninstall, force-stop, typing, or permission changes are
exposed. Mutations require one explicitly configured device serial and an
exact installed package, then observe the foreground package after launch.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable


_SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,99}\Z")
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\Z")
_FOREGROUND = re.compile(
    r"(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[:=]\s*[^\n]*?\b([A-Za-z][A-Za-z0-9_.]+)/[A-Za-z0-9_.$]+"
)


def _adb_path() -> str:
    configured = os.environ.get("JARVIS_ADB_PATH", "").strip()
    if configured:
        path = Path(configured)
        return str(path.resolve()) if path.is_file() else ""
    found = shutil.which("adb")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidate = Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        if candidate.is_file():
            return str(candidate)
    return ""


class AndroidAdb:
    def __init__(
        self,
        adb_path: str | None = None,
        serial: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.adb = _adb_path() if adb_path is None else adb_path
        self.serial = os.environ.get("JARVIS_ANDROID_SERIAL", "").strip() if serial is None else serial
        self._runner = runner or subprocess.run

    def _run(self, *parts: str, timeout: float = 8.0) -> tuple[int, str]:
        if not self.adb or not _SERIAL.fullmatch(self.serial):
            raise ValueError("ADB or exact Android serial is not configured")
        command = [self.adb, "-s", self.serial, *parts]
        try:
            result = self._runner(
                command, capture_output=True, text=True, timeout=timeout,
                check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(type(exc).__name__) from exc
        return int(result.returncode), (str(result.stdout or "") + "\n" + str(result.stderr or ""))[:100_000]

    def _connected(self) -> bool:
        code, output = self._run("get-state", timeout=4.0)
        return code == 0 and output.strip() == "device"

    def status(self) -> str:
        if not self.adb:
            return "Android unavailable: official ADB Platform-Tools not found. No phone was contacted."
        if not _SERIAL.fullmatch(self.serial):
            return "Android unpaired: set one exact JARVIS_ANDROID_SERIAL; no phone was contacted."
        try:
            if not self._connected():
                return "Android offline or unauthorized for the configured serial."
        except (RuntimeError, ValueError):
            return "Android connection unavailable for the configured serial."
        return "Android device connected under the configured exact serial; app actions still require verification."

    def find_apps(self, query: str, limit: int = 30) -> str:
        term = str(query).strip().lower()
        if not 1 <= len(term) <= 80 or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", term):
            return "error: use a short package-name search term"
        if not self.adb or not _SERIAL.fullmatch(self.serial):
            return "error: ADB and one exact Android serial are required; no phone contacted"
        try:
            if not self._connected():
                return "error: configured Android device is offline or unauthorized"
            code, output = self._run("shell", "pm", "list", "packages", term, timeout=12.0)
        except (RuntimeError, ValueError):
            return "error: Android package query unavailable"
        if code != 0:
            return "error: Android package query failed"
        packages = sorted({line[8:] for line in output.splitlines()
                           if line.startswith("package:") and _PACKAGE.fullmatch(line[8:])
                           and term in line[8:].lower()})
        if not packages:
            return "No installed Android package matched that term on the configured device."
        capped = packages[:max(1, min(limit, 50))]
        suffix = f"; {len(packages) - len(capped)} more omitted" if len(packages) > len(capped) else ""
        return "Installed Android packages: " + ", ".join(capped) + suffix

    def launch_app(self, package: str) -> str:
        target = str(package).strip()
        if not _PACKAGE.fullmatch(target):
            return "error: specify one exact Android package identifier; no action sent"
        if not self.adb or not _SERIAL.fullmatch(self.serial):
            return "error: ADB and one exact Android serial are required; no action sent"
        observed = ""
        try:
            if not self._connected():
                return "error: configured Android device is offline or unauthorized; no action sent"
            code, output = self._run("shell", "pm", "path", target, timeout=8.0)
            if code != 0 or not any(line.startswith("package:") for line in output.splitlines()):
                return "error: Android app is not installed for this device; no launch sent"
            code, output = self._run(
                "shell", "am", "start", "-W", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER", target, timeout=14.0,
            )
            if code != 0 or re.search(r"\b(?:Error|Exception|unable to resolve)\b", output, re.I):
                return "error: installed Android app has no launchable activity or launch failed"
            for _ in range(3):
                code, state = self._run("shell", "dumpsys", "activity", "activities", timeout=7.0)
                if code == 0:
                    match = _FOREGROUND.search(state)
                    if match and match.group(1) == target:
                        return f"Verified Android foreground package {target}."
                    if match:
                        observed = match.group(1)
                time.sleep(0.35)
        except (RuntimeError, ValueError):
            return "unknown: Android launch or verification unavailable; inspect phone before retry"
        if observed:
            return f"unverified: Android foreground is {observed}, not requested app; do not retry blindly"
        return "unverified: Android launch was sent but foreground package could not be observed; do not retry blindly"
