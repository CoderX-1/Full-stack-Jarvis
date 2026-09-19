"""Dependency-free Windows desktop control for JARVIS Mark II.

The public methods deliberately return verification-oriented text.  Input being
delivered is not treated as proof that an application accepted an operation.
"""

from __future__ import annotations

import ctypes
import base64
import json
import math
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse
from ctypes import wintypes
from pathlib import Path
from typing import Any


IS_WINDOWS = os.name == "nt"


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", wintypes.WPARAM),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


def _norm(value: str) -> str:
    # Preserve non-Latin labels while folding width/case variants. Restricting
    # matching to ASCII made otherwise visible localized app controls
    # impossible to address.
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class WindowsControl:
    """Discover apps and operate the interactive Windows desktop."""

    _ALIASES = {
        "calculator": "calc.exe",
        "command prompt": "cmd.exe",
        "cmd": "cmd.exe",
        "control panel": "control.exe",
        "file explorer": "explorer.exe",
        "explorer": "explorer.exe",
        "notepad": "notepad.exe",
        "paint": "mspaint.exe",
        "powershell": "powershell.exe",
        "settings": "ms-settings:",
        "task manager": "taskmgr.exe",
    }

    _VK = {
        "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
        "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
        "pause": 0x13, "capslock": 0x14, "escape": 0x1B, "esc": 0x1B,
        "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
        "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
        "down": 0x28, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
        "win": 0x5B, "windows": 0x5B, "apps": 0x5D,
        "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
        "media_next": 0xB0, "media_previous": 0xB1, "media_stop": 0xB2,
        "media_play_pause": 0xB3,
    }

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self._apps_cache: tuple[float, list[dict[str, str]]] = (0.0, [])
        if IS_WINDOWS:
            self.user32 = ctypes.windll.user32
            self.kernel32 = ctypes.windll.kernel32
            self._configure_win32()

    def _configure_win32(self) -> None:
        """Declare pointer-sized signatures; ctypes otherwise truncates HWNDs."""
        try:
            # Keep UIA, window rectangles, screenshots, and cursor coordinates
            # in the same per-monitor physical-pixel coordinate space.
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        self.user32.IsWindowEnabled.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user32.GetWindowTextLengthW.restype = ctypes.c_int
        self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.WindowFromPoint.argtypes = [wintypes.POINT]
        self.user32.WindowFromPoint.restype = wintypes.HWND
        self.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.user32.MonitorFromWindow.restype = wintypes.HANDLE
        self.user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsZoomed.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.SetFocus.argtypes = [wintypes.HWND]
        self.user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
        self.user32.VkKeyScanW.restype = ctypes.c_short
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    @staticmethod
    def _require_windows() -> None:
        if not IS_WINDOWS:
            raise RuntimeError("Windows desktop control is available only on Windows")

    def _inject_mouse(self, flags: int, data: int = 0) -> str:
        """Prefer SendInput and retain a legacy fallback for older environments."""
        event = _Input(type=0, mi=_MouseInput(
            dx=0, dy=0, mouseData=int(data) & 0xFFFFFFFF,
            dwFlags=int(flags), time=0, dwExtraInfo=0,
        ))
        try:
            if int(self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_Input))) == 1:
                return "SendInput"
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        self.user32.mouse_event(int(flags), 0, 0, int(data), 0)
        return "mouse_event"

    def _inject_click(self, down: int, up: int) -> str:
        """Inject an atomic button pair; never mix down/up backends."""
        events = (_Input * 2)(
            _Input(type=0, mi=_MouseInput(dx=0, dy=0, mouseData=0, dwFlags=int(down), time=0, dwExtraInfo=0)),
            _Input(type=0, mi=_MouseInput(dx=0, dy=0, mouseData=0, dwFlags=int(up), time=0, dwExtraInfo=0)),
        )
        try:
            if int(self.user32.SendInput(2, events, ctypes.sizeof(_Input))) == 2:
                return "SendInput"
        except (AttributeError, TypeError, ValueError, OSError):
            pass
        self.user32.mouse_event(int(down), 0, 0, 0, 0)
        self.user32.mouse_event(int(up), 0, 0, 0, 0)
        return "mouse_event"

    @staticmethod
    def _run_powershell(script: str, timeout: int = 15) -> str:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(
                detail or f"PowerShell failed with exit code {completed.returncode}"
            )
        return (completed.stdout or "").strip()

    def _start_apps(self, refresh: bool = False) -> list[dict[str, str]]:
        self._require_windows()
        cached_at, cached = self._apps_cache
        if not refresh and cached and time.monotonic() - cached_at < 300:
            return cached
        script = r"""
$out=@()
try {
  $out += @(Get-StartApps | ForEach-Object {
    [pscustomobject]@{Name=$_.Name; AppID=$_.AppID}
  })
} catch {}
$roots=@([Environment]::GetFolderPath('StartMenu'),[Environment]::GetFolderPath('CommonStartMenu'))
$links=@($roots | Where-Object {$_ -and (Test-Path -LiteralPath $_)} | ForEach-Object {
  Get-ChildItem -LiteralPath $_ -Recurse -Filter *.lnk -File -ErrorAction SilentlyContinue
})
$out += @($links | ForEach-Object {[pscustomobject]@{Name=$_.BaseName; AppID=$_.FullName}})
$out | ConvertTo-Json -Compress
"""
        raw = self._run_powershell(script)
        parsed: Any = json.loads(raw) if raw else []
        if isinstance(parsed, dict):
            parsed = [parsed]
        apps = [
            {"name": str(item.get("Name") or ""), "id": str(item.get("AppID") or "")}
            for item in parsed if isinstance(item, dict) and item.get("Name") and item.get("AppID")
        ]
        by_name: dict[str, dict[str, str]] = {}
        for app in apps:
            by_name.setdefault(_norm(app["name"]), app)
        for alias, app_id in self._ALIASES.items():
            by_name.setdefault(_norm(alias), {"name": alias.title(), "id": app_id})
        apps = list(by_name.values())
        apps.sort(key=lambda item: item["name"].casefold())
        self._apps_cache = (time.monotonic(), apps)
        return apps

    def list_installed_apps(self, search: str = "", limit: int = 50) -> str:
        apps = self._start_apps()
        needle = _norm(search)
        if needle:
            apps = [app for app in apps if needle in _norm(app["name"])]
        apps = apps[: max(1, min(int(limit), 100))]
        if not apps:
            return f"No installed app matched {search!r}."
        return "\n".join(app["name"] for app in apps)

    def known_folders(self) -> dict[str, Path]:
        self._require_windows()
        home = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
        defaults = {
            "home": home,
            "desktop": home / "Desktop",
            "documents": home / "Documents",
            "downloads": home / "Downloads",
            "pictures": home / "Pictures",
            "music": home / "Music",
            "videos": home / "Videos",
        }
        # OneDrive and customized shell folders may move Desktop/Documents.
        script = r"""
$key='HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders'
$names=@('Desktop','Personal','{374DE290-123F-4565-9164-39C4925E467B}','My Pictures','My Music','My Video')
$item=Get-ItemProperty -Path $key
$out=@{}
foreach($name in $names){if($null -ne $item.$name){$out[$name]=[Environment]::ExpandEnvironmentVariables($item.$name)}}
$out | ConvertTo-Json -Compress
"""
        try:
            raw = self._run_powershell(script)
            shell = json.loads(raw) if raw else {}
            keys = {
                "Desktop": "desktop", "Personal": "documents",
                "{374DE290-123F-4565-9164-39C4925E467B}": "downloads",
                "My Pictures": "pictures", "My Music": "music", "My Video": "videos",
            }
            for registry_name, friendly in keys.items():
                if shell.get(registry_name):
                    defaults[friendly] = Path(shell[registry_name]).resolve()
        except (RuntimeError, json.JSONDecodeError, OSError):
            pass
        return defaults

    def list_known_folders(self) -> str:
        return "\n".join(
            f"{name}: {path} | {'exists' if path.exists() else 'missing'}"
            for name, path in self.known_folders().items()
        )

    def find_files(self, query: str, location: str = "home", limit: int = 50) -> str:
        clean = query.strip()
        if not clean:
            raise ValueError("File search query is required")
        folders = self.known_folders()
        key = location.strip().casefold() or "home"
        root = folders.get(key)
        if root is None:
            root = Path(location).expanduser()
            if not root.is_absolute():
                root = self.base_dir / root
            root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"Search location does not exist: {root}")
        needle = clean.casefold()
        matches: list[str] = []
        scanned = 0
        for path in root.rglob("*"):
            scanned += 1
            if scanned > 50_000:
                break
            if needle in path.name.casefold():
                matches.append(str(path))
                if len(matches) >= max(1, min(int(limit), 200)):
                    break
        if not matches:
            return f"No files or folders matched {clean!r} under {root} (scanned {scanned})."
        return "\n".join(matches) + f"\nVerified {len(matches)} match(es) under {root}."

    def _resolve_app(self, name: str) -> tuple[str, str]:
        clean = name.strip()
        if not clean:
            raise ValueError("App name is required")
        wanted = _norm(clean)
        apps = self._start_apps()
        exact = [app for app in apps if _norm(app["name"]) == wanted]
        matches = exact or [app for app in apps if wanted in _norm(app["name"])]
        if len(matches) == 1:
            return matches[0]["name"], matches[0]["id"]
        if len(matches) > 1:
            names = ", ".join(app["name"] for app in matches[:8])
            raise ValueError(f"App name is ambiguous. Matches: {names}")
        alias = self._ALIASES.get(clean.casefold())
        if alias:
            return clean, alias
        raise ValueError(f"App is not installed or was not found: {clean}")

    def _process_path(self, pid: int) -> str:
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def windows(self) -> list[dict[str, Any]]:
        self._require_windows()
        rows: list[dict[str, Any]] = []
        # Windows can briefly report no foreground window while a desktop is
        # switching focus or a new process is creating its first window.
        foreground = int(self.user32.GetForegroundWindow() or 0)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def callback(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = self.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.strip()
            if not title:
                return True
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            exe_path = self._process_path(pid.value)
            rows.append({
                "handle": int(hwnd), "title": title, "pid": int(pid.value),
                "process": Path(exe_path).name if exe_path else "unknown",
                "foreground": int(hwnd) == foreground,
                "minimized": bool(self.user32.IsIconic(hwnd)),
                "maximized": bool(self.user32.IsZoomed(hwnd)),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
            })
            return True

        self.user32.EnumWindows(callback, 0)
        return rows

    def list_windows(self) -> str:
        rows = self.windows()
        if not rows:
            return "No visible application windows found."
        return "\n".join(
            f"{'*' if row['foreground'] else '-'} {row['title']} | {row['process']} | "
            f"PID {row['pid']} | selector=hwnd:{row['handle']}:pid:{row['pid']} | "
            f"{'minimized' if row['minimized'] else 'visible'} | rect={row['rect']}"
            for row in rows[:100]
        )

    def _find_window(self, query: str) -> dict[str, Any]:
        if query.strip().casefold().startswith("hwnd:"):
            selector = re.fullmatch(r"hwnd:(\d+):pid:(\d+)", query.strip(), re.I)
            if not selector:
                raise ValueError("Use the exact hwnd:<handle>:pid:<pid> selector from list_windows")
            handle, pid = map(int, selector.groups())
            matches = [r for r in self.windows() if r['handle'] == handle and r['pid'] == pid]
            if len(matches) != 1:
                raise ValueError("Window selector is stale or no longer available; refresh list_windows")
            return matches[0]
        wanted = _norm(query)
        if not wanted:
            rows = [row for row in self.windows() if row["foreground"]]
            if rows:
                return rows[0]
            raise ValueError("There is no foreground window")
        rows = self.windows()
        exact = [row for row in rows if wanted in {_norm(row["title"]), _norm(row["process"]), _norm(Path(row["process"]).stem)}]
        matches = exact or [
            row for row in rows
            if wanted in _norm(row["title"]) or wanted in _norm(row["process"])
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"No open window matched {query!r}")
        foreground = [row for row in matches if row["foreground"]]
        if foreground:
            return foreground[0]
        titles = ", ".join(f"{row['title']} (hwnd:{row['handle']}:pid:{row['pid']}, "
                           f"minimized={row.get('minimized', False)})" for row in matches[:6])
        raise ValueError(f"Window name is ambiguous. Matches: {titles}")

    def _focus_handle(self, hwnd: int) -> bool:
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        for _attempt in range(3):
            foreground = int(self.user32.GetForegroundWindow() or 0)
            current_thread = int(self.kernel32.GetCurrentThreadId())
            target_thread = int(self.user32.GetWindowThreadProcessId(hwnd, None) or 0)
            foreground_thread = int(
                self.user32.GetWindowThreadProcessId(foreground, None) or 0
            ) if foreground else 0
            attached: list[int] = []
            try:
                for thread_id in {target_thread, foreground_thread}:
                    if thread_id and thread_id != current_thread:
                        if self.user32.AttachThreadInput(current_thread, thread_id, True):
                            attached.append(thread_id)
                self.user32.ShowWindow(hwnd, 5)  # SW_SHOW
                self.user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
                self.user32.BringWindowToTop(hwnd)
                self.user32.SetForegroundWindow(hwnd)
                self.user32.SetFocus(hwnd)
                # Last-resort interactive activation for the rare state where
                # Windows reports no foreground window at all.
                if not self.user32.GetForegroundWindow():
                    self.user32.SwitchToThisWindow(hwnd, True)
            finally:
                for thread_id in reversed(attached):
                    self.user32.AttachThreadInput(current_thread, thread_id, False)
            time.sleep(0.12)
            if int(self.user32.GetForegroundWindow() or 0) == int(hwnd):
                return True
        return False

    def launch_app(self, name: str, wait_seconds: int = 8) -> str:
        self._require_windows()
        display, app_id = self._resolve_app(name)
        before = {(row["handle"], row["pid"]) for row in self.windows()}
        if app_id.startswith("ms-settings:") or Path(app_id).suffix.casefold() == ".lnk":
            os.startfile(app_id)  # type: ignore[attr-defined]
        elif Path(app_id).is_file():
            subprocess.Popen([app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif app_id.casefold().endswith(".exe") and "\\" not in app_id and "/" not in app_id:
            subprocess.Popen([app_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + max(1, min(int(wait_seconds), 20))
        wanted = _norm(display)
        latest: list[dict[str, Any]] = []
        started_at = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(0.25)
            latest = self.windows()
            new = [row for row in latest if (row["handle"], row["pid"]) not in before]
            related = [row for row in latest if wanted in _norm(row["title"]) or wanted in _norm(row["process"])]
            related_new = [
                row for row in new
                if wanted in _norm(row["title"]) or wanted in _norm(row["process"])
            ]
            if related_new:
                row = related_new[0]
                return f"Opened and verified {display}: window={row['title']!r}, PID={row['pid']}"
            # Apps that enforce one window may only foreground their existing
            # instance. Give a new window time to appear before accepting that.
            focused_related = [row for row in related if row["foreground"]]
            if focused_related and time.monotonic() - started_at >= 1.0:
                row = focused_related[0]
                return f"Opened and verified {display}: foreground window={row['title']!r}, PID={row['pid']}"
        # Some apps (for example Settings pages) reuse a process/window whose
        # title does not contain the Start-menu display name.
        foreground = [row for row in latest if row["foreground"]]
        if foreground:
            row = foreground[0]
            return f"Launch sent for {display}; foreground verification={row['title']!r}, PID={row['pid']}"
        return f"error: launch was sent for {display}, but no application window could be verified"

    def open_item(self, target: str) -> str:
        self._require_windows()
        clean = target.strip()
        is_windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", clean)) or clean.startswith("\\\\")
        parsed = urllib.parse.urlparse(clean) if not is_windows_path else urllib.parse.ParseResult("", "", clean, "", "", "")
        if parsed.scheme:
            if parsed.scheme.casefold() not in {"http", "https", "mailto", "ms-settings"}:
                raise ValueError("Only web, mailto, and Windows Settings links are supported")
            os.startfile(clean)  # type: ignore[attr-defined]
            return f"Opened {parsed.scheme} target and handed it to its registered Windows app"
        path = Path(clean).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        path = path.resolve()
        if not path.exists():
            raise ValueError(f"File or folder does not exist: {path}")
        os.startfile(str(path))  # type: ignore[attr-defined]
        return f"Opened existing item with its registered Windows app: {path}"

    def control_window(self, query: str, action: str) -> str:
        self._require_windows()
        row = self._find_window(query)
        hwnd = int(row["handle"])
        choice = action.casefold().strip().replace(" ", "_")
        if choice == "focus":
            ok = self._focus_handle(hwnd)
        elif choice == "minimize":
            self.user32.ShowWindow(hwnd, 6)
            time.sleep(0.2)
            ok = bool(self.user32.IsIconic(hwnd))
        elif choice == "maximize":
            self.user32.ShowWindow(hwnd, 3)
            time.sleep(0.2)
            ok = bool(self.user32.IsZoomed(hwnd))
        elif choice == "restore":
            self.user32.ShowWindow(hwnd, 9)
            time.sleep(0.2)
            ok = not bool(self.user32.IsIconic(hwnd)) and not bool(self.user32.IsZoomed(hwnd))
        elif choice == "close":
            self.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE, never force-kill
            deadline = time.monotonic() + 4
            while self.user32.IsWindow(hwnd) and time.monotonic() < deadline:
                time.sleep(0.1)
            ok = not bool(self.user32.IsWindow(hwnd))
            if not ok:
                return f"error: close requested for {row['title']!r}, but it remains open (it may be asking to save); inspect the dialog"
        elif choice in {"snap_left", "snap_right"}:
            return self.position_window(f"hwnd:{hwnd}:pid:{row['pid']}",
                                        0.0 if choice == "snap_left" else 0.5, 0.0, 0.5, 1.0)
        else:
            raise ValueError("Action must be focus, minimize, maximize, restore, close, snap_left, or snap_right")
        state = next((item for item in self.windows() if item["handle"] == hwnd), None)
        if not ok:
            return f"error: Windows did not verify {choice} for {row['title']!r}"
        detail = f", state={state['rect']}" if state else ""
        return f"Verified window action {choice} on {row['title']!r}{detail}"

    def _work_area(self, hwnd: int) -> tuple[int, int, int, int]:
        class MonitorInfo(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                        ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        monitor = self.user32.MonitorFromWindow(hwnd, 2)
        if not monitor or not self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            raise RuntimeError("Cannot determine target monitor work area")
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom

    def position_window(self, window: str, x: float, y: float, width: float, height: float) -> str:
        """Position any window in fractions of its monitor's usable work area."""
        self._require_windows()
        values = (x, y, width, height)
        if not all(math.isfinite(v) for v in values) or not (
            0 <= x < 1 and 0 <= y < 1 and width > 0 and height > 0
            and x + width <= 1.000001 and y + height <= 1.000001
        ):
            raise ValueError("Window region must fit within normalized monitor coordinates 0..1")
        row = self._find_window(window)
        hwnd, pid = int(row['handle']), int(row['pid'])
        left, top, right, bottom = self._work_area(hwnd)
        target = [round(left + x * (right-left)), round(top + y * (bottom-top)),
                  round(left + (x+width) * (right-left)), round(top + (y+height) * (bottom-top))]
        self.user32.ShowWindow(hwnd, 9)
        if not self.user32.SetWindowPos(hwnd, None, target[0], target[1],
                                       target[2]-target[0], target[3]-target[1], 0x0014):
            return "error: Windows rejected window positioning"
        time.sleep(0.25)
        current = next((r for r in self.windows() if r['handle'] == hwnd and r['pid'] == pid), None)
        if not current or any(abs(a-b) > 16 for a, b in zip(current['rect'], target)):
            return f"error: requested rectangle {target}; observed={current['rect'] if current else 'closed'}; app may enforce minimum size"
        return f"Verified window position for {row['title']!r}; rectangle={current['rect']}; selector=hwnd:{hwnd}:pid:{pid}"

    def _tap_vk(self, vk: int) -> None:
        self.user32.keybd_event(vk, 0, 0, 0)
        self.user32.keybd_event(vk, 0, 0x0002, 0)

    def send_keys(self, keys: str, window: str = "", interval_ms: int = 40) -> str:
        self._require_windows()
        target = self._find_window(window) if window else self._find_window("")
        target_handle = int(target["handle"])
        if not self._focus_handle(target_handle):
            return f"error: could not focus {target['title']!r}"
        if int(self.user32.GetForegroundWindow() or 0) != target_handle:
            return f"error: target {target['title']!r} did not retain foreground focus; no keys were sent"
        time.sleep(0.2)  # Avoid losing the first key to Windows' activation transition.
        chords = [part.strip() for part in keys.split(",") if part.strip()]
        if not chords or len(chords) > 20:
            raise ValueError("Provide between 1 and 20 comma-separated key chords")
        parsed_chords: list[list[int]] = []
        for chord in chords:
            names = [part.strip().casefold() for part in chord.split("+") if part.strip()]
            codes: list[int] = []
            for name in names:
                if len(name) == 1 and name.isalnum():
                    code = ord(name.upper())
                elif re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", name):
                    code = 0x70 + int(name[1:]) - 1
                else:
                    code = self._VK.get(name, 0)
                if not code:
                    raise ValueError(f"Unsupported key: {name}")
                codes.append(code)
            parsed_chords.append(codes)
        for codes in parsed_chords:
            if int(self.user32.GetForegroundWindow() or 0) != target_handle:
                return "error: target lost focus; remaining shortcuts stopped"
            held = []
            try:
                for code in codes:
                    held.append(code)
                    self.user32.keybd_event(code, 0, 0, 0)
            finally:
                for code in reversed(held):
                    self.user32.keybd_event(code, 0, 0x0002, 0)
            time.sleep(max(0, min(interval_ms, 1000)) / 1000)
        foreground_handle = int(self.user32.GetForegroundWindow() or 0)
        if foreground_handle != target_handle:
            return (
                f"error: delivered {len(chords)} key command(s), but target "
                f"{target['title']!r} lost foreground focus"
            )
        return f"Delivered {len(chords)} key command(s); target verified as {target['title']!r}"

    def _focused_text_state(self, hwnd: int) -> dict[str, Any]:
        """Read the focused control in memory so literal typing can be verified."""
        blocked = self._powershell_rectangles(self._native_password_rectangles(hwnd))
        script = f"""
Add-Type -AssemblyName UIAutomationClient
$el=[Windows.Automation.AutomationElement]::FocusedElement
$blocked=@({blocked})
if($null -eq $el){{[pscustomobject]@{{supported=$false;password=$false;pid=0;text=''}} | ConvertTo-Json -Compress; exit}}
$supported=$false; $text=''; $password=$false
try{{
 $password=$el.Current.IsPassword
 $r=$el.Current.BoundingRectangle; $cx=$r.X+($r.Width/2); $cy=$r.Y+($r.Height/2)
 foreach($b in $blocked){{if($cx -ge $b.l -and $cx -lt $b.right -and $cy -ge $b.t -and $cy -lt $b.bottom){{$password=$true;break}}}}
}}catch{{$password=$true}}
if(-not $password){{
 $pattern=$null
 try{{if($el.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern,[ref]$pattern)){{$text=$pattern.DocumentRange.GetText(50000);$supported=$true}}}}catch{{}}
 if(-not $supported){{try{{if($el.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)){{$text=$pattern.Current.Value;$supported=$true}}}}catch{{}}}}
 if(-not $supported){{try{{if($el.TryGetCurrentPattern([Windows.Automation.LegacyIAccessiblePattern]::Pattern,[ref]$pattern)){{$text=$pattern.Current.Value;$supported=$true}}}}catch{{}}}}
}}
[pscustomobject]@{{supported=$supported;password=$password;pid=$el.Current.ProcessId;text=$text}} | ConvertTo-Json -Compress
"""
        raw = self._run_powershell(script, timeout=10)
        parsed = json.loads(raw) if raw else {}
        if not isinstance(parsed, dict):
            raise RuntimeError("Focused-control inspection returned invalid data")
        return parsed

    def type_text(self, text: str, window: str = "", interval_ms: int = 5) -> str:
        self._require_windows()
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            raise ValueError("Text cannot be empty")
        if len(text) > 10_000:
            raise ValueError("Text is limited to 10,000 characters per action")
        # Validate the complete input before delivering any of it.
        for char in text:
            if char not in {"\n", "\t"}:
                encoded = int(self.user32.VkKeyScanW(char))
                if encoded in {-1, 0xFFFF}:
                    raise ValueError(f"Unsupported keyboard character: U+{ord(char):04X}; no text was typed")
        target = self._find_window(window) if window else self._find_window("")
        target_handle = int(target["handle"])
        if not self._focus_handle(target_handle):
            return f"error: could not focus {target['title']!r}"
        if int(self.user32.GetForegroundWindow() or 0) != target_handle:
            return f"error: target {target['title']!r} did not retain foreground focus; no text was typed"
        time.sleep(0.2)  # Avoid losing the first character during foreground activation.
        before_state = self._focused_text_state(target_handle)
        before_pid = int(before_state.get("pid") or 0)
        if before_pid and before_pid != int(target.get("pid") or 0):
            return "error: the focused control belongs to another process; no text was typed"
        before_window_text = ""
        window_baseline_valid = False
        if not before_state.get("supported") and not before_state.get("password"):
            try:
                before_window_text = "\n".join(
                    str(item.get("name") or "")
                    for item in self.ui_elements(target_handle, 500)
                    if item.get("name")
                )
                window_baseline_valid = True
            except Exception:
                # Some apps temporarily rebuild their accessibility tree while
                # receiving focus. The post-state still gets one safe retry.
                before_window_text = ""
        typed = 0
        for char in text:
            if int(self.user32.GetForegroundWindow() or 0) != target_handle:
                return f"error: target lost focus after {typed} character(s); remaining input stopped"
            if char == "\n":
                self._tap_vk(0x0D)
            elif char == "\t":
                self._tap_vk(0x09)
            else:
                encoded = int(self.user32.VkKeyScanW(char))
                if encoded == -1 or encoded == 0xFFFF:
                    raise ValueError(f"Character cannot be typed by the active Windows keyboard layout: U+{ord(char):04X}")
                vk, modifiers = encoded & 0xFF, (encoded >> 8) & 0xFF
                held = []
                try:
                    for mask, code in ((1, 0x10), (2, 0x11), (4, 0x12)):
                        if modifiers & mask:
                            held.append(code)
                            self.user32.keybd_event(code, 0, 0, 0)
                    self._tap_vk(vk)
                finally:
                    for code in reversed(held):
                        self.user32.keybd_event(code, 0, 0x0002, 0)
            typed += 1
            if interval_ms:
                time.sleep(max(0, min(interval_ms, 250)) / 1000)
        time.sleep(0.1)
        foreground_handle = int(self.user32.GetForegroundWindow() or 0)
        if foreground_handle != target_handle:
            foreground = next(
                (row for row in self.windows() if row["handle"] == foreground_handle),
                None,
            )
            observed = foreground["title"] if foreground else "no foreground window"
            return (
                f"error: typed input was delivered, but focus left {target['title']!r} "
                f"for {observed!r}; content is not verified"
            )
        state = self._focused_text_state(target_handle)
        if int(state.get("pid") or 0) != int(target.get("pid") or 0):
            return "error: typed input was delivered, but the focused control belongs to another process"
        expected = text.replace("\r\n", "\n").replace("\r", "\n")
        before_text = str(before_state.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        if state.get("password"):
            return (
                f"Typed {typed} character(s) into a password-protected control in "
                f"{target['title']!r}; target focus verified, content deliberately not read"
            )
        if not state.get("supported"):
            try:
                observed_window_text = "\n".join(
                    str(item.get("name") or "")
                    for item in self.ui_elements(target_handle, 500)
                    if item.get("name")
                )
            except Exception:
                observed_window_text = ""
            if (
                window_baseline_valid
                and expected in observed_window_text
                and observed_window_text.count(expected) > before_window_text.count(expected)
            ):
                return (
                    f"Typed and verified {typed} character(s) in {target['title']!r}; "
                    "foreground, process, and window accessibility text all match"
                )
            return (
                f"error: typed {typed} character(s) into {target['title']!r}, but its focused "
                "control exposes no readable value; content is not verified"
            )
        observed_text = str(state.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        if expected not in observed_text or (
            before_state.get("supported")
            and observed_text.count(expected) <= before_text.count(expected)
        ):
            return (
                f"error: typed {typed} character(s) into {target['title']!r}, but the focused "
                "control did not expose the expected text; content is not verified"
            )
        return (
            f"Typed and verified {typed} character(s) in {target['title']!r}; "
            "foreground, process, and focused-control content all match"
        )

    def mouse_action(
        self, action: str, x: int | None = None, y: int | None = None,
        button: str = "left", amount: int = 3,
    ) -> str:
        self._require_windows()
        choice = action.casefold().strip().replace(" ", "_")
        virtual_x = self.user32.GetSystemMetrics(76)
        virtual_y = self.user32.GetSystemMetrics(77)
        width = self.user32.GetSystemMetrics(78) or self.user32.GetSystemMetrics(0)
        height = self.user32.GetSystemMetrics(79) or self.user32.GetSystemMetrics(1)
        if x is not None or y is not None:
            if (
                x is None or y is None
                or not (virtual_x <= int(x) < virtual_x + width)
                or not (virtual_y <= int(y) < virtual_y + height)
            ):
                raise ValueError(
                    f"Coordinates must be inside the virtual desktop "
                    f"({virtual_x},{virtual_y},{width},{height})"
                )
            if not self.user32.SetCursorPos(int(x), int(y)):
                return "error: Windows rejected the cursor movement"
            positioned = wintypes.POINT()
            self.user32.GetCursorPos(ctypes.byref(positioned))
            if positioned.x != int(x) or positioned.y != int(y):
                return (
                    f"error: cursor verification failed; requested ({int(x)},{int(y)}) "
                    f"but observed ({positioned.x},{positioned.y})"
                )
        flags = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
        if choice == "move":
            pass
        elif choice in {"click", "double_click"}:
            if button not in flags:
                raise ValueError("Button must be left, right, or middle")
            down, up = flags[button]
            for _ in range(2 if choice == "double_click" else 1):
                self.user32.mouse_event(down, 0, 0, 0, 0)
                self.user32.mouse_event(up, 0, 0, 0, 0)
                time.sleep(0.08)
        elif choice == "scroll":
            self.user32.mouse_event(0x0800, 0, 0, int(amount) * 120, 0)
        else:
            raise ValueError("Action must be move, click, double_click, or scroll")
        point = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(point))
        foreground = next((row for row in self.windows() if row["foreground"]), None)
        title = foreground["title"] if foreground else "unknown"
        return f"Delivered mouse {choice}; cursor verified at ({point.x},{point.y}); foreground={title!r}"

    def guarded_click(
        self, hwnd: int, x: int, y: int, expected_rect: list[int], button: str = "left",
    ) -> str:
        """Deliver one click only while the observed window geometry/ownership is still valid."""
        self._require_windows()
        handle = int(hwnd)
        x, y = int(x), int(y)
        if button not in {"left", "right", "middle"}:
            raise ValueError("Button must be left, right, or middle")
        if len(expected_rect) != 4:
            raise ValueError("expected_rect must contain left, top, right, bottom")
        virtual_x = self.user32.GetSystemMetrics(76)
        virtual_y = self.user32.GetSystemMetrics(77)
        width = self.user32.GetSystemMetrics(78) or self.user32.GetSystemMetrics(0)
        height = self.user32.GetSystemMetrics(79) or self.user32.GetSystemMetrics(1)
        if not (virtual_x <= x < virtual_x + width and virtual_y <= y < virtual_y + height):
            return "error: guarded click target is outside the virtual desktop"

        def safety_error() -> str:
            if not self.user32.IsWindow(handle):
                return "target window closed"
            if int(self.user32.GetForegroundWindow() or 0) != handle:
                return "foreground window changed"
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                return "target window rectangle is unavailable"
            if [rect.left, rect.top, rect.right, rect.bottom] != [int(value) for value in expected_rect]:
                return "target window moved or resized"
            point = wintypes.POINT(x, y)
            hit = int(self.user32.WindowFromPoint(point) or 0)
            root = int(self.user32.GetAncestor(hit, 2) or 0) if hit else 0
            if root != handle:
                return "another window covers the visual target"
            return ""

        problem = safety_error()
        if problem:
            return f"error: guarded click refused because {problem}; no click was delivered"
        if not self.user32.SetCursorPos(x, y):
            return "error: Windows rejected guarded cursor positioning; no click was delivered"
        cursor = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(cursor)) or (cursor.x, cursor.y) != (x, y):
            return "error: guarded cursor verification failed; no click was delivered"
        # Give the target UI thread one short message-pump turn to process the
        # cursor move before mouse-down, then revalidate every guard again.
        time.sleep(0.03)
        problem = safety_error()
        if problem:
            return f"error: guarded click refused after cursor positioning because {problem}; no click was delivered"
        flags = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }
        down, up = flags[button]
        backend = self._inject_click(down, up)
        return (
            f"Delivered guarded {button} click at ({x},{y}) via {backend}; "
            "window, geometry, foreground, and occlusion reverified"
        )

    def guarded_scroll(
        self, hwnd: int, x: int, y: int, expected_rect: list[int], amount: int,
    ) -> str:
        """Scroll only while the intended visible window still owns the target point."""
        self._require_windows()
        handle = int(hwnd)
        x, y, delta = int(x), int(y), max(-20, min(int(amount), 20))
        if not delta:
            raise ValueError("Scroll amount must not be zero")
        if len(expected_rect) != 4:
            raise ValueError("expected_rect must contain left, top, right, bottom")
        virtual_x = self.user32.GetSystemMetrics(76)
        virtual_y = self.user32.GetSystemMetrics(77)
        width = self.user32.GetSystemMetrics(78) or self.user32.GetSystemMetrics(0)
        height = self.user32.GetSystemMetrics(79) or self.user32.GetSystemMetrics(1)
        if not (virtual_x <= x < virtual_x + width and virtual_y <= y < virtual_y + height):
            return "error: guarded scroll point is outside the virtual desktop; no scroll was delivered"

        def safety_error() -> str:
            if not self.user32.IsWindow(handle):
                return "target window closed"
            if int(self.user32.GetForegroundWindow() or 0) != handle:
                return "foreground window changed"
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                return "target window rectangle is unavailable"
            if [rect.left, rect.top, rect.right, rect.bottom] != [int(value) for value in expected_rect]:
                return "target window moved or resized"
            point = wintypes.POINT(x, y)
            hit = int(self.user32.WindowFromPoint(point) or 0)
            root = int(self.user32.GetAncestor(hit, 2) or 0) if hit else 0
            if root != handle:
                return "another window covers the scroll point"
            return ""

        problem = safety_error()
        if problem:
            return f"error: guarded scroll refused because {problem}; no scroll was delivered"
        if not self.user32.SetCursorPos(x, y):
            return "error: Windows rejected guarded cursor positioning; no scroll was delivered"
        cursor = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(cursor)) or (cursor.x, cursor.y) != (x, y):
            return "error: guarded scroll cursor verification failed; no scroll was delivered"
        problem = safety_error()
        if problem:
            return f"error: guarded scroll refused after cursor positioning because {problem}; no scroll was delivered"
        backend = self._inject_mouse(0x0800, delta * 120)
        return (
            f"Delivered guarded scroll amount={delta} at ({x},{y}); window, geometry, "
            f"foreground, and occlusion reverified via {backend}"
        )

    def guarded_invoke_at_point(
        self, hwnd: int, x: int, y: int, expected_rect: list[int],
    ) -> str:
        """Invoke an accessible control at a verified visual point without guessing its name."""
        self._require_windows()
        handle, x, y = int(hwnd), int(x), int(y)
        if len(expected_rect) != 4:
            raise ValueError("expected_rect must contain left, top, right, bottom")

        def safety_error() -> str:
            if not self.user32.IsWindow(handle):
                return "target window closed"
            if int(self.user32.GetForegroundWindow() or 0) != handle:
                return "foreground window changed"
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(handle, ctypes.byref(rect)):
                return "target window rectangle is unavailable"
            if [rect.left, rect.top, rect.right, rect.bottom] != [int(value) for value in expected_rect]:
                return "target window moved or resized"
            point = wintypes.POINT(x, y)
            hit = int(self.user32.WindowFromPoint(point) or 0)
            root = int(self.user32.GetAncestor(hit, 2) or 0) if hit else 0
            if root != handle:
                return "another window covers the visual target"
            return ""

        problem = safety_error()
        if problem:
            return f"error: guarded invoke refused because {problem}; no action was delivered"
        native_buttons: list[tuple[int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def inspect_child(child: int, _lparam: int) -> bool:
            rect = wintypes.RECT()
            class_buffer = ctypes.create_unicode_buffer(256)
            if not self.user32.GetWindowRect(child, ctypes.byref(rect)):
                return True
            if not (rect.left <= x < rect.right and rect.top <= y < rect.bottom):
                return True
            self.user32.GetClassNameW(child, class_buffer, 256)
            style = int(self.user32.GetWindowLongW(child, -16))
            if (
                "button" in class_buffer.value.casefold() and not style & 0x20
                and self.user32.IsWindowVisible(child) and self.user32.IsWindowEnabled(child)
            ):
                native_buttons.append((max(1, (rect.right - rect.left) * (rect.bottom - rect.top)), int(child)))
            return True

        self.user32.EnumChildWindows(handle, inspect_child, 0)
        if native_buttons:
            child = min(native_buttons)[1]
            result = ctypes.c_size_t()
            delivered = self.user32.SendMessageTimeoutW(
                child, 0x00F5, 0, 0, 0x0002, 1000, ctypes.byref(result),  # BM_CLICK, SMTO_ABORTIFHUNG
            )
            if delivered:
                return f"Delivered guarded native button invoke at ({x},{y}); child_hwnd={child}"
        row = next((item for item in self.windows() if int(item.get("handle") or 0) == handle), None)
        expected_pid = int(row.get("pid") or 0) if row else 0
        script = f"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName WindowsBase
$point=[Windows.Point]::new({x},{y})
$el=[Windows.Automation.AutomationElement]::FromPoint($point)
if($null -eq $el){{throw 'No accessible element at visual point'}}
$walker=[Windows.Automation.TreeWalker]::ControlViewWalker
$selected=$null; $pattern=$null; $blockedReason=''
for($depth=0; $depth -lt 8 -and $null -ne $el; $depth++){{
 try{{
  $r=$el.Current.BoundingRectangle
  if(-not $r.Contains($point)){{$blockedReason='Accessible bounds do not contain visual point';break}}
  if($el.Current.IsPassword -or $el.Current.IsOffscreen -or -not $el.Current.IsEnabled){{$blockedReason='Accessible element is protected, offscreen, or disabled';break}}
  if({expected_pid} -gt 0 -and $el.Current.ProcessId -ne {expected_pid}){{$blockedReason='Accessible element belongs to another process';break}}
  if($el.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)){{$selected=$el;break}}
 }}catch{{break}}
 $el=$walker.GetParent($el)
}}
if($null -eq $selected){{
 [pscustomobject]@{{invoked=$false;blocked=[bool]$blockedReason;reason=if($blockedReason){{$blockedReason}}else{{'InvokePattern unavailable at point or safe ancestors'}}}} | ConvertTo-Json -Compress
 exit
}}
$pattern.Invoke()
[pscustomobject]@{{invoked=$true;type=$selected.Current.ControlType.ProgrammaticName}} | ConvertTo-Json -Compress
"""
        try:
            result = json.loads(self._run_powershell(script, timeout=8))
        except (RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return f"unsupported: guarded accessibility invoke unavailable ({str(exc)[:120]})"
        if result.get("blocked"):
            reason = str(result.get("reason") or "accessible invoke was blocked")
            if reason.casefold() == "accessible element belongs to another process":
                # Packaged Windows apps commonly host their visible HWND in
                # ApplicationFrameHost while UIA reports the child app PID.
                # No action was delivered, and guarded_click will independently
                # recheck the top-level HWND, foreground, rectangle, point
                # ownership, and occlusion before physical input.
                return "unsupported: accessible element belongs to a hosted child process; no action was delivered"
            return f"error: guarded accessibility invoke refused because {reason}; no action was delivered"
        if not result.get("invoked"):
            return f"unsupported: {result.get('reason') or 'control is not invokable'}"
        problem = safety_error()
        if problem and problem != "target window closed":
            return f"error: accessible action was delivered but post-invoke guard observed {problem}"
        return f"Delivered guarded accessibility invoke at ({x},{y}); element={result.get('type') or 'control'}"

    def media_control(self, action: str, steps: int = 1) -> str:
        self._require_windows()
        choice = action.casefold().strip().replace(" ", "_")
        mapping = {
            "volume_up": "volume_up", "volume_down": "volume_down", "mute": "volume_mute",
            "play_pause": "media_play_pause", "next": "media_next",
            "previous": "media_previous", "stop": "media_stop",
        }
        if choice not in mapping:
            raise ValueError("Unsupported media action")
        count = max(1, min(int(steps), 50)) if choice in {"volume_up", "volume_down"} else 1
        for _ in range(count):
            self._tap_vk(self._VK[mapping[choice]])
            time.sleep(0.03)
        return f"Delivered Windows media action {choice}" + (f" ({count} steps)" if count > 1 else "")

    def _native_password_rectangles(self, hwnd: int) -> list[tuple[int, int, int, int]]:
        """Return screen-coordinate rectangles for native ES_PASSWORD controls."""
        password_rectangles: list[tuple[int, int, int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def inspect_child(child: int, _lparam: int) -> bool:
            class_buffer = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(child, class_buffer, 256)
            style = int(self.user32.GetWindowLongW(child, -16))
            if "edit" in class_buffer.value.casefold() and style & 0x20:  # ES_PASSWORD
                rect = wintypes.RECT()
                if self.user32.GetWindowRect(child, ctypes.byref(rect)):
                    password_rectangles.append((rect.left, rect.top, rect.right, rect.bottom))
            return True

        self.user32.EnumChildWindows(int(hwnd), inspect_child, 0)
        return password_rectangles

    @staticmethod
    def _powershell_rectangles(rectangles: list[tuple[int, int, int, int]]) -> str:
        return ",".join(
            f"[pscustomobject]@{{l={left};t={top};right={right};bottom={bottom}}}"
            for left, top, right, bottom in rectangles
        )

    def ui_elements(self, hwnd: int, limit: int = 500) -> list[dict[str, Any]]:
        """Return visible, non-password UIA elements with screen rectangles."""
        max_items = max(1, min(int(limit), 1000))
        password_rectangles = self._native_password_rectangles(hwnd)
        blocked = self._powershell_rectangles(password_rectangles)
        script = f"""
Add-Type -AssemblyName UIAutomationClient
function ConvertTo-UiBase64($value){{
 [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$value))
}}
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(hwnd)})
$all=$root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
$walker=[Windows.Automation.TreeWalker]::ControlViewWalker
$blocked=@({blocked})
$out=@()
$failures=@()
foreach($el in $all){{
 if($out.Count -ge {max_items}){{break}}
 try{{
  if($el.Current.IsPassword -or $el.Current.IsOffscreen){{continue}}
  $r=$el.Current.BoundingRectangle; $cx=$r.X+($r.Width/2); $cy=$r.Y+($r.Height/2)
  $secret=$false
  foreach($b in $blocked){{
   if($cx -ge $b.l -and $cx -lt $b.right -and $cy -ge $b.t -and $cy -lt $b.bottom){{$secret=$true;break}}
  }}
  if($secret){{continue}}
  $name=$el.Current.Name; $id=$el.Current.AutomationId; $help=$el.Current.HelpText
  $accessKey=$el.Current.AccessKey; $className=$el.Current.ClassName
  $runtimeId=''; $parentRuntimeId=''
  try{{$runtimeId=($el.GetRuntimeId() -join '.')}}catch{{}}
  try{{$parent=$walker.GetParent($el);if($null -ne $parent){{$parentRuntimeId=($parent.GetRuntimeId() -join '.')}}}}catch{{}}
  $selected=$null; $toggleState=$null; $expandState=$null; $valuePresent=$false
  $rangeValue=$null; $rangeMin=$null; $rangeMax=$null
  $isModal=$false; $interactionState=$null; $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern,[ref]$statePattern)){{$selected=[bool]$statePattern.Current.IsSelected}}}}catch{{}}
  $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern,[ref]$statePattern)){{$toggleState=$statePattern.Current.ToggleState.ToString()}}}}catch{{}}
  $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern,[ref]$statePattern)){{$expandState=$statePattern.Current.ExpandCollapseState.ToString()}}}}catch{{}}
  $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$statePattern)){{$valuePresent=([string]$statePattern.Current.Value).Length -gt 0}}}}catch{{}}
  $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.RangeValuePattern]::Pattern,[ref]$statePattern)){{$rangeValue=[double]$statePattern.Current.Value;$rangeMin=[double]$statePattern.Current.Minimum;$rangeMax=[double]$statePattern.Current.Maximum}}}}catch{{}}
  $statePattern=$null
  try{{if($el.TryGetCurrentPattern([Windows.Automation.WindowPattern]::Pattern,[ref]$statePattern)){{$isModal=[bool]$statePattern.Current.IsModal;$interactionState=$statePattern.Current.WindowInteractionState.ToString()}}}}catch{{}}
  $actionable=$false; $pattern=$null
  foreach($patternId in @([Windows.Automation.InvokePattern]::Pattern,[Windows.Automation.TogglePattern]::Pattern,[Windows.Automation.SelectionItemPattern]::Pattern,[Windows.Automation.ExpandCollapsePattern]::Pattern)){{
   try{{if($el.TryGetCurrentPattern($patternId,[ref]$pattern)){{$actionable=$true;break}}}}catch{{}}
  }}
  if(($name -or $id -or $help -or $accessKey -or $actionable) -and $r.Width -gt 0 -and $r.Height -gt 0){{
    $out += [pscustomobject]@{{name_b64=(ConvertTo-UiBase64 $name);id_b64=(ConvertTo-UiBase64 $id);help_b64=(ConvertTo-UiBase64 $help);access_key_b64=(ConvertTo-UiBase64 $accessKey);class_b64=(ConvertTo-UiBase64 $className);type=$el.Current.ControlType.ProgrammaticName;enabled=$el.Current.IsEnabled;focusable=$el.Current.IsKeyboardFocusable;focused=$el.Current.HasKeyboardFocus;actionable=$actionable;runtime_id=$runtimeId;parent_runtime_id=$parentRuntimeId;selected=$selected;toggle_state=$toggleState;expand_state=$expandState;value_present=$valuePresent;range_value=$rangeValue;range_min=$rangeMin;range_max=$rangeMax;is_modal=$isModal;interaction_state=$interactionState;x=[int]$r.X;y=[int]$r.Y;width=[int]$r.Width;height=[int]$r.Height}}
  }}
 }}catch{{$failures += $_.Exception.Message}}
}}
if($out.Count -eq 0 -and $failures.Count -gt 0){{
  [pscustomobject]@{{__uia_error_b64=(ConvertTo-UiBase64 (($failures | Select-Object -First 3) -join '; '))}} | ConvertTo-Json -Compress
}}else{{
 $out | ConvertTo-Json -Compress
}}
"""
        raw = self._run_powershell(script, timeout=15)
        # Windows PowerShell 5.1's ConvertTo-Json can emit literal C0 bytes
        # found in third-party accessibility labels (observed in VS Code).
        # One malformed label must not discard the entire window state. Parse
        # that legacy output tolerantly, then remove the unsafe characters
        # before any label reaches matching, formatting, or persistence.
        parsed: Any = json.loads(raw, strict=False) if raw else []
        if isinstance(parsed, dict):
            parsed = [parsed]
        parsed = [
            {
                key: (
                    re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
                    if isinstance(value, str)
                    else value
                )
                for key, value in item.items()
            }
            for item in parsed
            if isinstance(item, dict)
        ]
        for item in parsed:
            for field in ("name", "id", "help", "access_key", "class", "__uia_error"):
                encoded = item.pop(f"{field}_b64", None)
                if encoded is None:
                    continue
                try:
                    decoded = base64.b64decode(str(encoded), validate=True).decode("utf-8", errors="replace")
                except (ValueError, UnicodeError):
                    decoded = ""
                item[field] = re.sub(r"[\x00-\x1f\x7f]+", " ", decoded).strip()
        diagnostic = next((item.get("__uia_error") for item in parsed if isinstance(item, dict) and item.get("__uia_error")), None)
        if diagnostic:
            raise RuntimeError(f"UI Automation state inspection failed: {str(diagnostic)[:500]}")
        return parsed

    def inspect_ui(self, window: str, limit: int = 80) -> str:
        self._require_windows()
        row = self._find_window(window)
        max_items = max(1, min(int(limit), 200))
        items = self.ui_elements(int(row["handle"]), max_items)
        if not items:
            return f"No named UI controls found in {row['title']!r}."
        return "\n".join(
            f"{item.get('type','').replace('ControlType.','')}: {item.get('name') or '(unnamed)'}"
            + (f" | id={item.get('id')}" if item.get("id") else "")
            + (" | disabled" if item.get("enabled") is False else "")
            for item in items
        )

    def read_window_text(self, window: str, max_chars: int = 20_000) -> str:
        """Read document/value text exposed by UI Automation, skipping passwords."""
        self._require_windows()
        row = self._find_window(window)
        cap = max(100, min(int(max_chars), 50_000))
        native_values: list[str] = []
        child_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @child_callback_type
        def read_child(hwnd: int, _lparam: int) -> bool:
            if sum(len(item) for item in native_values) >= cap:
                return False
            # ES_PASSWORD controls must never be read, even when the caller asks.
            style = int(self.user32.GetWindowLongW(hwnd, -16))
            if style & 0x20:
                return True
            class_buffer = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, class_buffer, 256)
            class_name = class_buffer.value.casefold()
            if not any(marker in class_name for marker in ("edit", "richedit", "text")):
                return True
            length = ctypes.c_size_t()
            if not self.user32.SendMessageTimeoutW(hwnd, 0x000E, 0, 0, 0x0002, 500, ctypes.byref(length)):
                return True
            if not 0 < length.value <= cap:
                return True
            buffer = ctypes.create_unicode_buffer(length.value + 1)
            result = ctypes.c_size_t()
            self.user32.SendMessageTimeoutW(
                hwnd, 0x000D, length.value + 1, ctypes.cast(buffer, ctypes.c_void_p).value,
                0x0002, 500, ctypes.byref(result),
            )
            value = buffer.value.strip()
            if value and value not in native_values:
                native_values.append(value)
            return True

        self.user32.EnumChildWindows(int(row["handle"]), read_child, 0)
        blocked = self._powershell_rectangles(
            self._native_password_rectangles(int(row["handle"]))
        )
        script = f"""
Add-Type -AssemblyName UIAutomationClient
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(row['handle'])})
$items=@($root)+@($root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition))
$blocked=@({blocked})
$out=New-Object System.Collections.Generic.List[string]
$seen=@{{}}; $remaining={cap}
foreach($el in $items){{
 if($remaining -le 0){{break}}
 try{{
  if($el.Current.IsPassword){{continue}}
  $r=$el.Current.BoundingRectangle; $cx=$r.X+($r.Width/2); $cy=$r.Y+($r.Height/2)
  $secret=$false
  foreach($b in $blocked){{if($cx -ge $b.l -and $cx -lt $b.right -and $cy -ge $b.t -and $cy -lt $b.bottom){{$secret=$true;break}}}}
  if($secret){{continue}}
 }}catch{{continue}}
 $text=''; $pattern=$null
 try{{
  if($el.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern,[ref]$pattern)){{$text=$pattern.DocumentRange.GetText($remaining)}}
  elseif($el.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)){{$text=$pattern.Current.Value}}
  elseif($el.TryGetCurrentPattern([Windows.Automation.LegacyIAccessiblePattern]::Pattern,[ref]$pattern)){{$text=$pattern.Current.Value}}
 }}catch{{}}
 $text=($text -replace "`0",'').Trim()
 if($text -and -not $seen.ContainsKey($text)){{$seen[$text]=$true; $out.Add($text); $remaining-=$text.Length}}
}}
$out | ConvertTo-Json -Compress
"""
        raw = self._run_powershell(script, timeout=20)
        values: Any = json.loads(raw) if raw else []
        if isinstance(values, str):
            values = [values]
        combined = native_values + [str(value) for value in values if value]
        text = "\n".join(dict.fromkeys(combined)).strip()
        if not text:
            return f"No readable document text was exposed by {row['title']!r}."
        return f"Visible text from {row['title']!r}:\n{text[:cap]}"

    def interact_ui(self, window: str, control: str, action: str, value: str = "") -> str:
        self._require_windows()
        row = self._find_window(window)
        choice = action.casefold().strip().replace(" ", "_")
        if choice not in {"click", "focus", "set_text", "toggle", "select", "expand", "collapse"}:
            raise ValueError("Unsupported UI action")
        blocked = self._powershell_rectangles(
            self._native_password_rectangles(int(row["handle"]))
        )
        script = f"""
Add-Type -AssemblyName UIAutomationClient
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{int(row['handle'])})
$needle={_ps_quote(control)}
$all=$root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
$blocked=@({blocked}); $safe=@()
foreach($candidate in $all){{
 try{{
  if($candidate.Current.IsPassword){{continue}}
  $r=$candidate.Current.BoundingRectangle; $cx=$r.X+($r.Width/2); $cy=$r.Y+($r.Height/2); $secret=$false
  foreach($b in $blocked){{if($cx -ge $b.l -and $cx -lt $b.right -and $cy -ge $b.t -and $cy -lt $b.bottom){{$secret=$true;break}}}}
  if(-not $secret){{$safe += $candidate}}
 }}catch{{}}
}}
$matches=@($safe | Where-Object {{ $_.Current.Name -ieq $needle -or $_.Current.AutomationId -ieq $needle }})
if($matches.Count -eq 0){{$matches=@($safe | Where-Object {{ $_.Current.Name -like ('*'+$needle+'*') }})}}
if($matches.Count -eq 0){{throw 'UI control not found'}}
if($matches.Count -gt 1){{throw ('UI control is ambiguous: '+$matches.Count+' matches')}}
$el=$matches[0]; $labelName=$el.Current.Name; $labelId=$el.Current.AutomationId; $action={_ps_quote(choice)}; $value={_ps_quote(value)}
$beforeSignature=($safe | ForEach-Object {{ $_.Current.Name+'|'+$_.Current.AutomationId+'|'+$_.Current.ControlType.ProgrammaticName }}) -join "`n"
$verified=$false; $observed=''
switch($action){{
 'focus' {{$el.SetFocus(); Start-Sleep -Milliseconds 100; $verified=$el.Current.HasKeyboardFocus; $observed='keyboard focus='+$verified}}
 'set_text' {{$p=$el.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern); $p.SetValue($value); Start-Sleep -Milliseconds 100; $actual=$p.Current.Value; $verified=($actual -ceq $value); $observed=if($verified){{'value matched'}}else{{'value did not match'}}}}
 'toggle' {{$p=$el.GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern); $before=$p.Current.ToggleState; $p.Toggle(); Start-Sleep -Milliseconds 100; $observed=$p.Current.ToggleState.ToString(); $verified=($p.Current.ToggleState -ne $before)}}
 'select' {{$p=$el.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern); $p.Select(); Start-Sleep -Milliseconds 100; $verified=$p.Current.IsSelected; $observed='selected='+$verified}}
 'expand' {{$p=$el.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern); $p.Expand(); Start-Sleep -Milliseconds 100; $observed=$p.Current.ExpandCollapseState.ToString(); $verified=($observed -eq 'Expanded')}}
 'collapse' {{$p=$el.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern); $p.Collapse(); Start-Sleep -Milliseconds 100; $observed=$p.Current.ExpandCollapseState.ToString(); $verified=($observed -eq 'Collapsed')}}
 'click' {{$p=$el.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern); $p.Invoke()}}
}}
if($action -eq 'click'){{
 Start-Sleep -Milliseconds 500
 try{{
  $after=$root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition); $afterSafe=@()
  foreach($candidate in $after){{
   try{{
    if($candidate.Current.IsPassword){{continue}}
    $r=$candidate.Current.BoundingRectangle; $cx=$r.X+($r.Width/2); $cy=$r.Y+($r.Height/2); $secret=$false
    foreach($b in $blocked){{if($cx -ge $b.l -and $cx -lt $b.right -and $cy -ge $b.t -and $cy -lt $b.bottom){{$secret=$true;break}}}}
    if(-not $secret){{$afterSafe += $candidate}}
   }}catch{{}}
  }}
  $afterSignature=($afterSafe | ForEach-Object {{ $_.Current.Name+'|'+$_.Current.AutomationId+'|'+$_.Current.ControlType.ProgrammaticName }}) -join "`n"
  $verified=($afterSignature -cne $beforeSignature); $observed=if($verified){{'UI state changed'}}else{{'no observable UI state change'}}
 }}catch{{$verified=$true; $observed='target window or dialog closed'}}
}}
[pscustomobject]@{{name=$labelName; id=$labelId; action=$action; verified=$verified; observed=$observed}} | ConvertTo-Json -Compress
"""
        raw = self._run_powershell(script)
        result = json.loads(raw)
        label = result.get("name") or result.get("id")
        if not result.get("verified"):
            return (
                f"error: UI action {result.get('action')} was delivered to {label!r}, "
                f"but success was not observable ({result.get('observed')})"
            )
        return (
            f"Verified UI action {result.get('action')} on {label!r} in {row['title']!r}; "
            f"observed={result.get('observed')}"
        )
