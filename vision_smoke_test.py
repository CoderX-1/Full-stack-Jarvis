"""Isolated end-to-end smoke test for the JARVIS Vision-Control Engine."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from windows_control import WindowsControl
from windows_vision import WindowsVision


def _launch_test_surface(title: str) -> subprocess.Popen[str]:
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form=New-Object Windows.Forms.Form
$form.Text='{title}'
$form.Size=New-Object Drawing.Size(900,500)
$form.StartPosition='Manual'
$form.Location=New-Object Drawing.Point(300,200)
$form.BackColor=[Drawing.Color]::White
$form.FormBorderStyle='FixedSingle'
$form.TopMost=$true
$form.Add_Shown({{$form.Activate(); $form.BringToFront()}})
$state=New-Object Windows.Forms.Label
$state.Text='VISION ONLINE'; $state.Font=New-Object Drawing.Font('Arial',30,[Drawing.FontStyle]::Bold)
$state.AutoSize=$true; $state.Location=New-Object Drawing.Point(280,70)
$next=New-Object Windows.Forms.Button
$next.Text='NEXT STATE'; $next.Font=New-Object Drawing.Font('Arial',20)
$next.Size=New-Object Drawing.Size(240,65); $next.Location=New-Object Drawing.Point(320,165)
$next.Add_Click({{$state.Text='VISION VERIFIED'; $state.ForeColor=[Drawing.Color]::DarkGreen}})
$password=New-Object Windows.Forms.TextBox
$password.UseSystemPasswordChar=$true; $password.Text='NEVER_EXPOSE_VISION_SECRET'
$password.Font=New-Object Drawing.Font('Arial',18); $password.Size=New-Object Drawing.Size(300,40)
$password.Location=New-Object Drawing.Point(290,250)
$close=New-Object Windows.Forms.Button
$close.Text='CLOSE TEST'; $close.Font=New-Object Drawing.Font('Arial',15)
$close.Size=New-Object Drawing.Size(200,50); $close.Location=New-Object Drawing.Point(340,315)
$close.Add_Click({{$form.Close()}})
$footer=New-Object Windows.Forms.Label
$footer.Text='LOCAL OCR TEST SURFACE'; $footer.Font=New-Object Drawing.Font('Arial',14)
$footer.AutoSize=$true; $footer.Location=New-Object Drawing.Point(320,390)
$form.Controls.AddRange([Windows.Forms.Control[]]@($state,$next,$password,$close,$footer))
$form.Add_Shown({{$password.Select()}})
[Windows.Forms.Application]::Run($form)
"""
    return subprocess.Popen(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Sta", "-Command", script],
        text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> None:
    title = f"JARVIS VISION TEST {os.getpid()}"
    process = _launch_test_surface(title)
    control = WindowsControl()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(title == row["title"] for row in control.windows()):
                break
            if process.poll() is not None:
                raise RuntimeError("Native test surface exited during startup")
            time.sleep(0.2)
        else:
            raise RuntimeError("Test surface did not appear")

        with tempfile.TemporaryDirectory() as tmp:
            vision = WindowsVision(control, Path(tmp))
            print(vision.status(), flush=True)
            row = next(item for item in control.windows() if item["title"] == title)
            left, top, _right, _bottom = row["rect"]
            detected_passwords = vision._password_rectangles(row["handle"], left, top)
            if not detected_passwords:
                raise AssertionError("Native password field detector returned no secure rectangles")
            print(f"PASSWORD DETECTOR VERIFIED fields={len(detected_passwords)}", flush=True)
            before = vision.observe(title)
            print(f"CAPTURE {before.width}x{before.height} hash={before.image_hash}", flush=True)
            if "VISION ONLINE" not in before.text:
                raise AssertionError(f"OCR missed initial state: {before.text!r}")
            if before.redacted_password_fields < 1:
                raise AssertionError("Native password field was not detected and redacted")
            if "NEVER_EXPOSE_VISION_SECRET" in before.text:
                raise AssertionError("Password content reached OCR output")
            print(
                f"PASSWORD REDACTION VERIFIED fields={before.redacted_password_fields}",
                flush=True,
            )
            matches = vision.locate(before, "NEXT STATE")
            if len(matches) != 1:
                raise AssertionError(f"Expected one NEXT STATE target, found {len(matches)}")
            print(f"LOCATE {matches[0]}", flush=True)
            clicked = vision.click_visual_text(title, "NEXT STATE")
            print(clicked, flush=True)
            if clicked.startswith("error:"):
                raise AssertionError(clicked)
            after = vision.observe(title)
            if "VISION VERIFIED" not in after.text:
                raise AssertionError(f"OCR missed changed state: {after.text!r}")
            print(f"POST-CLICK OCR VERIFIED hash={after.image_hash}", flush=True)
            closed = vision.click_visual_text(title, "CLOSE TEST")
            print(closed, flush=True)
            if closed.startswith("error:"):
                raise AssertionError(closed)
            process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
            title == row["title"] for row in control.windows()
        ):
            time.sleep(0.1)
        if any(title == row["title"] for row in control.windows()):
            raise RuntimeError("Test surface process exited but its window remained")
        print(f"TEST WINDOW CLOSED VERIFIED: {title}", flush=True)


if __name__ == "__main__":
    main()
