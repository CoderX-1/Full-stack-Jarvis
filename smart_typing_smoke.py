"""Native Windows smoke test for Gate 5 Smart Typing Component 1."""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from windows_control import WindowsControl
from windows_vision import WindowsVision


def _wait_for(control: WindowsControl, predicate, seconds: float = 10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        match = next((row for row in control.windows() if predicate(row)), None)
        if match:
            return match
        time.sleep(0.2)
    return None


def main() -> None:
    control = WindowsControl(Path.cwd())
    if any(str(row.get("process") or "").casefold() == "notepad.exe" for row in control.windows()):
        raise RuntimeError("Close existing Notepad windows before this isolated smoke test")

    subprocess.Popen(["notepad.exe"], close_fds=True)
    row = _wait_for(
        control,
        lambda item: str(item.get("process") or "").casefold() == "notepad.exe",
    )
    if row is None:
        raise RuntimeError("A fresh Notepad test window did not appear")

    hwnd, pid = int(row["handle"]), int(row["pid"])
    selector = f"hwnd:{hwnd}:pid:{pid}"
    payload = "Unicode: café Ω ✓ 🙂 | Roman Urdu: main tayyar hoon"
    test_error: BaseException | None = None
    try:
        control.begin_guarded_action()
        result = control.type_text(payload, selector, 0)
        print(result)
        if not result.startswith("Typed and verified"):
            raise RuntimeError(result)
        if "clipboard=untouched" not in result or "unicode_fallback=0" in result:
            raise RuntimeError("Unicode fallback or clipboard invariant was not evidenced")
        print(f"PASS selector={selector} characters={len(payload)}")
    except BaseException as exc:
        test_error = exc
    finally:
        control.control_window(selector, "close")
        dialog = _wait_for(
            control,
            lambda item: int(item.get("pid") or 0) == pid
            and int(item.get("handle") or 0) != hwnd
            and str(item.get("process") or "").casefold() == "notepad.exe",
            seconds=5,
        )
        if dialog:
            dialog_selector = f"hwnd:{int(dialog['handle'])}:pid:{pid}"
            with tempfile.TemporaryDirectory(prefix="jarvis-smart-typing-") as state:
                cleanup = WindowsVision(control, Path(state)).click_visual_text(
                    dialog_selector,
                    "Don't Save",
                    expect_absent_text="Don't Save",
                    timeout_seconds=5,
                )
            print(f"CLEANUP {cleanup}")
        remaining = _wait_for(
            control,
            lambda item: int(item.get("handle") or 0) == hwnd,
            seconds=2,
        )
        if remaining and test_error is None:
            test_error = RuntimeError(
                "The disposable Notepad document remains open; close it manually without saving"
            )
    if test_error:
        raise test_error


if __name__ == "__main__":
    main()
