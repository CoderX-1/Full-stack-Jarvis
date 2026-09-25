"""Local-first screen vision for JARVIS Windows Control.

Screenshots stay in memory unless the caller explicitly requests a diagnostic
snapshot. OCR uses the Windows.Media.Ocr engine through winocr; no image is sent
to an external service.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import ctypes
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from ctypes import wintypes

from florence_client import FlorenceClient
from selector_engine import SelectorEngine
from ui_state_graph import UIStateGraph
from windows_control import IS_WINDOWS, WindowsControl, _norm


@dataclass(frozen=True)
class VisionWord:
    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height


@dataclass
class VisionObservation:
    title: str
    handle: int
    origin_x: int
    origin_y: int
    width: int
    height: int
    image_hash: str
    text: str
    lines: list[list[VisionWord]]
    image: Any
    redacted_password_fields: int = 0
    ocr_scale: float = 1.0
    pid: int = 0
    process: str = ""


class WindowsVision:
    """Capture, OCR, locate, and visually verify Windows UI actions."""

    _BLOCKED_PROCESSES = {
        "credentialuibroker.exe", "lockapp.exe", "logonui.exe",
        "securityhealthsystray.exe", "1password.exe", "keepass.exe",
        "keepassxc.exe", "bitwarden.exe",
    }
    _BLOCKED_TITLE_WORDS = {
        "windows security", "user account control", "enter password",
        "sign in options", "credential", "password manager",
    }
    _NUMBER_FORMS = {
        "0": {"0", "zero", "num0button"},
        "1": {"1", "one", "num1button"},
        "2": {"2", "two", "num2button"},
        "3": {"3", "three", "num3button"},
        "4": {"4", "four", "num4button"},
        "5": {"5", "five", "num5button"},
        "6": {"6", "six", "num6button"},
        "7": {"7", "seven", "num7button"},
        "8": {"8", "eight", "num8button"},
        "9": {"9", "nine", "num9button"},
    }
    _ROLE_ALIASES = {
        "button": "button", "icon": "button", "link": "hyperlink",
        "tab": "tabitem", "menu": "menuitem", "menuitem": "menuitem",
        "checkbox": "checkbox", "check": "checkbox", "radio": "radiobutton",
        "textbox": "edit", "input": "edit", "field": "edit",
        "list": "list", "listitem": "listitem", "item": "listitem",
        "treeitem": "treeitem", "slider": "slider", "spinner": "spinner",
    }
    _POSITION_WORDS = {"top", "bottom", "left", "right", "center", "middle"}
    _QUERY_FILLER = {"the", "a", "an", "at", "in", "on", "near", "visual", "control"}
    _COLOR_WORDS = {"red", "orange", "yellow", "green", "blue", "purple", "pink", "black", "white", "gray", "grey"}
    _SHAPE_WORDS = {"circle", "round", "square", "wide", "tall"}

    def __init__(self, control: WindowsControl, state_dir: Path) -> None:
        self.control = control
        self.state_dir = state_dir
        self.state_graph = UIStateGraph(state_dir / "ui-state-graph.json")
        self.selector_engine = SelectorEngine(state_dir / "selector-memory.json")
        self.template_dir = state_dir / "vision-targets"
        self.florence = FlorenceClient()

    def _action_abort_requested(self) -> bool:
        check = getattr(self.control, "action_abort_requested", None)
        # Test doubles and third-party control adapters can expose dynamic mock
        # attributes. Only the literal boolean True is an abort request.
        return check() is True if callable(check) else False

    @staticmethod
    def _dependencies() -> tuple[Any, Any, Any]:
        try:
            from PIL import ImageChops, ImageDraw, ImageGrab
            import winocr
        except ImportError as exc:
            raise RuntimeError(
                "Vision dependencies are missing. Install Pillow and winocr in the JARVIS environment."
            ) from exc
        return (ImageGrab, ImageDraw, ImageChops), winocr, __import__("PIL.Image", fromlist=["Image"])

    def status(self) -> str:
        if not IS_WINDOWS:
            return "error: Vision-Control Engine requires Windows"
        try:
            (_, _, _), winocr, _image = self._dependencies()
            version = getattr(winocr, "__version__", "installed")
            screens = self.control.user32.GetSystemMetrics(80)
            florence = self.florence.health()
            vlm_status = "ready-local-offline" if florence.get("ready") else "standby-unavailable"
            return (
                "Vision-Control Engine ready | capture=Pillow ImageGrab | "
                f"ocr=Windows.Media.Ocr ({version}) | displays={screens or 1} | "
                "targeting=primary-OCR+enriched-UIA+semantic-role-position-color-grounding+"
                "adaptive-local-OCR+learned-local-templates+Florence-semantic-fallback+bounded-scroll-search | "
                "selectors=ranked-evidence+stable-identity+context-anchor+fresh-repair+decaying-private-memory | "
                f"vlm={vlm_status} | "
                "verification=motion-stability+two-pass-target+guarded-input+foreground+geometry+occlusion+"
                "polled-postconditions+OCR+UIA+target-local-diff | "
                "privacy=screenshots-memory-only,password-fields-redacted,OCR-local,VLM-localhost-offline; "
                "recognized text is returned to the selected brain when a vision tool is used"
            )
        except RuntimeError as exc:
            return f"error: {exc}"

    @classmethod
    def _assert_safe_target(cls, row: dict[str, Any]) -> None:
        process = str(row.get("process") or "").casefold()
        title = str(row.get("title") or "").casefold()
        if process in cls._BLOCKED_PROCESSES or any(word in title for word in cls._BLOCKED_TITLE_WORDS):
            raise ValueError("Vision is disabled for authentication, password-manager, lock, and Windows Security surfaces")

    def _password_rectangles(self, hwnd: int, origin_x: int, origin_y: int) -> list[tuple[int, int, int, int]]:
        """Return screen-derived password boxes as window-relative rectangles."""
        rectangles: list[tuple[int, int, int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def inspect_child(child: int, _lparam: int) -> bool:
            class_buffer = ctypes.create_unicode_buffer(256)
            self.control.user32.GetClassNameW(child, class_buffer, 256)
            style = int(self.control.user32.GetWindowLongW(child, -16))
            if "edit" in class_buffer.value.casefold() and style & 0x20:  # ES_PASSWORD
                rect = wintypes.RECT()
                if self.control.user32.GetWindowRect(child, ctypes.byref(rect)):
                    rectangles.append((
                        rect.left - origin_x, rect.top - origin_y,
                        rect.right - origin_x, rect.bottom - origin_y,
                    ))
            return True

        self.control.user32.EnumChildWindows(hwnd, inspect_child, 0)
        script = f"""
Add-Type -AssemblyName UIAutomationClient
$root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd})
$all=$root.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
$out=@()
foreach($el in $all){{
 try{{if($el.Current.IsPassword){{$r=$el.Current.BoundingRectangle; $out += [pscustomobject]@{{x=[int]$r.X;y=[int]$r.Y;width=[int]$r.Width;height=[int]$r.Height}}}}}}catch{{}}
}}
$out | ConvertTo-Json -Compress
"""
        try:
            raw = self.control._run_powershell(script, timeout=8)
            parsed: Any = json.loads(raw) if raw else []
            if isinstance(parsed, dict):
                parsed = [parsed]
            rectangles.extend([
                (int(item["x"]) - origin_x, int(item["y"]) - origin_y,
                 int(item["x"]) - origin_x + int(item["width"]),
                 int(item["y"]) - origin_y + int(item["height"]))
                for item in parsed if int(item.get("width") or 0) > 0 and int(item.get("height") or 0) > 0
            ])
            return list(dict.fromkeys(rectangles))
        except (RuntimeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Password-field inspection failed; refusing visual OCR rather than risking secret capture"
            ) from exc

    @staticmethod
    def _parse_ocr(result: dict[str, Any]) -> tuple[str, list[list[VisionWord]]]:
        lines: list[list[VisionWord]] = []
        for line in result.get("lines") or []:
            words: list[VisionWord] = []
            for item in line.get("words") or []:
                box = item.get("bounding_rect") or {}
                text = str(item.get("text") or "").strip()
                if text:
                    words.append(VisionWord(
                        text=text,
                        x=round(float(box.get("x") or 0)),
                        y=round(float(box.get("y") or 0)),
                        width=max(1, round(float(box.get("width") or 1))),
                        height=max(1, round(float(box.get("height") or 1))),
                    ))
            if words:
                lines.append(words)
        text = str(result.get("text") or "").strip()
        if not text:
            text = "\n".join(" ".join(word.text for word in line) for line in lines)
        return text, lines

    @staticmethod
    def _restore_coordinates(
        lines: list[list[VisionWord]], scale: float,
    ) -> list[list[VisionWord]]:
        if scale == 1.0:
            return lines
        return [[
            VisionWord(
                word.text,
                round(word.x / scale), round(word.y / scale),
                max(1, round(word.width / scale)),
                max(1, round(word.height / scale)),
            )
            for word in line
        ] for line in lines]

    def observe(
        self, window: str = "", language: str = "en", *, focus: bool = True,
    ) -> VisionObservation:
        if not IS_WINDOWS:
            raise RuntimeError("Vision-Control Engine requires Windows")
        (ImageGrab, ImageDraw, _ImageChops), winocr, _Image = self._dependencies()
        row = self.control._find_window(window)
        self._assert_safe_target(row)
        if row.get("minimized"):
            raise ValueError("Target window is minimized; restore it before visual observation")
        hwnd = int(row["handle"])
        if focus:
            if not self.control._focus_handle(hwnd):
                raise RuntimeError(f"Could not focus {row['title']!r} for a reliable screenshot")
            time.sleep(0.2)
        elif int(self.control.user32.GetForegroundWindow() or 0) != hwnd:
            raise RuntimeError("Refusing an unfocused capture because another window could occlude the target")
        current = next((item for item in self.control.windows() if item["handle"] == hwnd), row)
        left, top, right, bottom = [int(value) for value in current["rect"]]
        if right <= left or bottom <= top:
            raise RuntimeError("Target window has an invalid capture rectangle")
        password_boxes = self._password_rectangles(hwnd, left, top)
        image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
        if image.size != (right - left, bottom - top):
            raise RuntimeError(
                "Screenshot geometry did not match the physical-pixel window rectangle; "
                "refusing coordinates from a mismatched capture"
            )
        if password_boxes:
            draw = ImageDraw.Draw(image)
            for box in password_boxes:
                draw.rectangle(box, fill="black")
        ocr_image = image
        ocr_scale = 1.0
        max_dimension = 2600
        if max(image.width, image.height) > max_dimension:
            ocr_scale = max_dimension / max(image.width, image.height)
            resized = (
                max(1, round(image.width * ocr_scale)),
                max(1, round(image.height * ocr_scale)),
            )
            ocr_image = image.resize(resized, _Image.Resampling.LANCZOS)
        try:
            result = winocr.recognize_pil_sync(ocr_image, language or "en")
        except Exception as exc:
            raise RuntimeError("Local Windows OCR failed; no visual coordinates are trusted") from exc
        text, lines = self._parse_ocr(result)
        lines = self._restore_coordinates(lines, ocr_scale)
        digest = hashlib.sha256(image.tobytes()).hexdigest()[:16]
        return VisionObservation(
            title=str(current["title"]), handle=hwnd, origin_x=left, origin_y=top,
            width=image.width, height=image.height, image_hash=digest,
            text=text, lines=lines, image=image,
            redacted_password_fields=len(password_boxes),
            ocr_scale=ocr_scale, pid=int(current.get("pid") or 0),
            process=str(current.get("process") or ""),
        )

    @staticmethod
    def locate(observation: VisionObservation, query: str) -> list[dict[str, Any]]:
        wanted_words = [part for part in re.split(r"\s+", query.strip()) if part]
        if not wanted_words:
            raise ValueError("Visual text query is required")
        wanted = _norm(" ".join(wanted_words))
        if not wanted:
            raise ValueError("Visual text query must include at least one letter or number")
        candidates: list[dict[str, Any]] = []
        for line_number, line in enumerate(observation.lines, start=1):
            # OCR can split one visual label into extra words (for example,
            # "Sign-in" -> "Sign in") or merge adjacent words. Search a
            # narrow span around the requested length without scanning whole
            # lines as one permissive fuzzy target.
            span = len(wanted_words)
            sizes = sorted({size for size in (span - 1, span, span + 1) if 1 <= size <= len(line)})
            for size in sizes:
                for start in range(0, len(line) - size + 1):
                    group = line[start:start + size]
                    combined = " ".join(word.text for word in group)
                    normalized = _norm(combined)
                    score = 1.0 if normalized == wanted else SequenceMatcher(None, wanted, normalized).ratio()
                    if len(wanted) >= 6 and size == span and wanted in normalized:
                        score = max(score, 0.96)
                    # Short labels are common desktop controls and dangerous
                    # fuzzy targets (for example Open vs OpenAI). Prefer a
                    # safe miss over clicking an adjacent or similarly named
                    # control when OCR confidence cannot establish identity.
                    threshold = 1.0 if len(wanted) <= 3 else 0.90 if len(wanted) <= 5 else 0.80
                    if score < threshold:
                        continue
                    left = min(word.x for word in group)
                    top = min(word.y for word in group)
                    right = max(word.x + word.width for word in group)
                    bottom = max(word.y + word.height for word in group)
                    candidates.append({
                        "text": combined, "score": round(score, 3), "line": line_number,
                        "source": "ocr", "role": "text",
                        "x": left, "y": top, "width": right - left, "height": bottom - top,
                        "screen_x": observation.origin_x + (left + right) // 2,
                        "screen_y": observation.origin_y + (top + bottom) // 2,
                    })
        unique: dict[tuple[int, int, int, int], dict[str, Any]] = {}
        selected: list[dict[str, Any]] = []
        for item in sorted(candidates, key=lambda value: (-value["score"], value["line"])):
            key = (item["x"], item["y"], item["width"], item["height"])
            if key in unique:
                continue
            item_right = item["x"] + item["width"]
            item_bottom = item["y"] + item["height"]
            overlaps_better = False
            for better in selected:
                if better["score"] <= item["score"]:
                    continue
                intersection_width = max(
                    0, min(item_right, better["x"] + better["width"])
                    - max(item["x"], better["x"]),
                )
                intersection_height = max(
                    0, min(item_bottom, better["y"] + better["height"])
                    - max(item["y"], better["y"]),
                )
                smaller_area = min(
                    item["width"] * item["height"],
                    better["width"] * better["height"],
                )
                if intersection_width * intersection_height >= 0.8 * smaller_area:
                    overlaps_better = True
                    break
            if not overlaps_better:
                unique[key] = item
                selected.append(item)
        return selected

    @classmethod
    def _query_parts(cls, query: str) -> dict[str, Any]:
        raw_words = [_norm(part) for part in re.findall(r"[^\W_]+", query.casefold(), re.UNICODE)]
        words = [word for word in raw_words if word]
        roles = {cls._ROLE_ALIASES[word] for word in words if word in cls._ROLE_ALIASES}
        positions = {word for word in words if word in cls._POSITION_WORDS}
        colors = {word.replace("grey", "gray") for word in words if word in cls._COLOR_WORDS}
        shapes = {word for word in words if word in cls._SHAPE_WORDS}
        ignored = set(cls._ROLE_ALIASES) | cls._POSITION_WORDS | cls._COLOR_WORDS | cls._SHAPE_WORDS | cls._QUERY_FILLER
        content = [word for word in words if word not in ignored]
        return {
            "roles": roles, "positions": positions, "colors": colors,
            "shapes": shapes, "content": content, "wanted": _norm(" ".join(content)),
        }

    @staticmethod
    def _position_matches(
        positions: set[str], cx: float, cy: float, width: int, height: int,
    ) -> bool:
        if not positions:
            return True
        nx = cx / max(1, width)
        ny = cy / max(1, height)
        return all((
            word == "left" and nx <= 0.50
            or word == "right" and nx >= 0.50
            or word == "top" and ny <= 0.50
            or word == "bottom" and ny >= 0.50
            or word in {"center", "middle"} and 0.25 <= nx <= 0.75 and 0.25 <= ny <= 0.75
        ) for word in positions)

    @staticmethod
    def _dominant_color(image: Any, box: tuple[int, int, int, int]) -> str:
        """Return a conservative basic color name for a visible target crop."""
        try:
            crop = image.crop(box).convert("RGB").resize((16, 16))
            getter = getattr(crop, "get_flattened_data", None)
            pixels = list(getter() if callable(getter) else crop.getdata())
            colorful = [pixel for pixel in pixels if max(pixel) - min(pixel) >= 28]
            sample = colorful or pixels
            red = sum(pixel[0] for pixel in sample) / max(1, len(sample))
            green = sum(pixel[1] for pixel in sample) / max(1, len(sample))
            blue = sum(pixel[2] for pixel in sample) / max(1, len(sample))
            high, low = max(red, green, blue), min(red, green, blue)
            if high < 48:
                return "black"
            if low > 215 and high - low < 22:
                return "white"
            if high - low < 24:
                return "gray"
            if red > green * 1.35 and red > blue * 1.25:
                return "orange" if green > red * 0.48 else "red"
            if red > 150 and blue > 110 and green < min(red, blue) * 0.82:
                return "pink" if red > blue * 1.22 else "purple"
            if green > red * 1.20 and green > blue * 1.12:
                return "green"
            if blue > red * 1.18 and blue > green * 1.10:
                return "blue"
            if red > 150 and green > 125 and blue < min(red, green) * 0.72:
                return "yellow" if abs(red - green) < 65 else "orange"
            return "gray"
        except Exception:
            return "unknown"

    @classmethod
    def _semantic_matches(
        cls, observation: VisionObservation, query: str,
        elements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fuse UIA names, roles, metadata, position, and visible color."""
        wanted_full = _norm(query)
        if not wanted_full:
            raise ValueError("Visual target query is required")
        parts = cls._query_parts(query)
        semantic_wanted = parts["wanted"] or wanted_full
        forms = set(cls._NUMBER_FORMS.get(semantic_wanted, {semantic_wanted}))
        strict_number = semantic_wanted in cls._NUMBER_FORMS
        for digit, equivalents in cls._NUMBER_FORMS.items():
            if semantic_wanted in equivalents:
                forms.update(equivalents)
                forms.add(digit)
                strict_number = True
                break
        descriptor_only = bool(parts["roles"] or parts["positions"] or parts["colors"] or parts["shapes"]) and not parts["content"]
        matches: list[dict[str, Any]] = []
        for item in elements:
            if item.get("enabled") is False:
                continue
            name = str(item.get("name") or "").strip()
            automation_id = str(item.get("id") or "").strip()
            metadata = [
                name, automation_id, str(item.get("help") or ""),
                str(item.get("access_key") or ""), str(item.get("class") or ""),
            ]
            labels = {_norm(value) for value in metadata if value and _norm(value)}
            role = _norm(str(item.get("type") or "").replace("ControlType.", ""))
            class_role = _norm(str(item.get("class") or ""))
            # Some native/framework bridges (notably WinForms) expose real
            # buttons as ControlType.Pane. Recover the semantic role from the
            # native class without weakening identity or geometry checks.
            if role in {"", "pane", "custom"}:
                inferred = next((
                    canonical for marker, canonical in (
                        ("button", "button"), ("edit", "edit"),
                        ("checkbox", "checkbox"), ("radiobutton", "radiobutton"),
                        ("combobox", "combobox"), ("listbox", "list"),
                    ) if marker in class_role
                ), "")
                if inferred:
                    role = inferred
            if parts["roles"] and not any(required in role for required in parts["roles"]):
                continue
            exact = bool(forms & labels)
            if strict_number and not exact:
                continue
            content_score = 1.0 if exact else max(
                (SequenceMatcher(None, form, label).ratio() for form in forms for label in labels),
                default=0.0,
            )
            if not exact and parts["content"]:
                token_coverage = max((
                    sum(token in label for token in parts["content"]) / len(parts["content"])
                    for label in labels
                ), default=0.0)
                content_score = max(content_score, token_coverage)
                threshold = 1.0 if len(semantic_wanted) <= 3 else 0.88 if len(semantic_wanted) <= 5 else 0.72
                if content_score < threshold:
                    continue
            elif not exact and not descriptor_only and content_score < 0.88:
                continue
            screen_left = int(item.get("x") or 0)
            screen_top = int(item.get("y") or 0)
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            left = screen_left - observation.origin_x
            top = screen_top - observation.origin_y
            right = left + width
            bottom = top + height
            if (
                width <= 0 or height <= 0 or right <= 0 or bottom <= 0
                or left >= observation.width or top >= observation.height
            ):
                continue
            relative_cx = left + width / 2
            relative_cy = top + height / 2
            if not cls._position_matches(parts["positions"], relative_cx, relative_cy, observation.width, observation.height):
                continue
            nx = relative_cx / max(1, observation.width)
            ny = relative_cy / max(1, observation.height)
            position_quality = min((
                nx if word == "right" else 1.0 - nx if word == "left"
                else ny if word == "bottom" else 1.0 - ny if word == "top"
                else max(0.0, 1.0 - 2.0 * (abs(nx - 0.5) + abs(ny - 0.5)))
                for word in parts["positions"]
            ), default=0.0)
            color = cls._dominant_color(
                observation.image,
                (max(0, left), max(0, top), min(observation.width, right), min(observation.height, bottom)),
            ) if parts["colors"] else ""
            if parts["colors"] and color not in parts["colors"]:
                continue
            aspect = width / max(1, height)
            if parts["shapes"]:
                shape_ok = all((
                    shape in {"circle", "round", "square"} and 0.72 <= aspect <= 1.38
                    or shape == "wide" and aspect >= 1.6
                    or shape == "tall" and aspect <= 0.63
                ) for shape in parts["shapes"])
                if not shape_ok:
                    continue
            descriptor_score = (
                (0.18 if parts["roles"] else 0.0)
                + (0.16 * position_quality if parts["positions"] else 0.0)
                + (0.12 if parts["colors"] else 0.0)
                + (0.08 if parts["shapes"] else 0.0)
            )
            score = 1.0 if exact else min(0.99, max(content_score, 0.52) + descriptor_score)
            source = "uia-fallback" if exact and not any((parts["positions"], parts["colors"], parts["shapes"])) else "semantic-grounding"
            matches.append({
                "text": name or automation_id or str(item.get("help") or "") or f"unnamed {role or 'control'}",
                "automation_id": automation_id, "role": role, "color": color,
                "runtime_id": str(item.get("runtime_id") or ""),
                "parent_runtime_id": str(item.get("parent_runtime_id") or ""),
                "access_key": str(item.get("access_key") or ""),
                "class": str(item.get("class") or ""),
                "source": source, "score": round(score, 3), "line": 0,
                "x": max(0, left), "y": max(0, top),
                "width": min(observation.width, right) - max(0, left),
                "height": min(observation.height, bottom) - max(0, top),
                "screen_x": screen_left + width // 2,
                "screen_y": screen_top + height // 2,
            })
        unique: dict[tuple[int, int, int, int], dict[str, Any]] = {}
        for item in sorted(matches, key=lambda value: -value["score"]):
            key = (item["x"], item["y"], item["width"], item["height"])
            unique.setdefault(key, item)
        ranked = list(unique.values())
        if descriptor_only and parts["positions"] and len(ranked) > 1:
            # A spatial superlative may safely disambiguate only with a clear
            # confidence margin; near-ties remain explicit ambiguity.
            if ranked[0]["score"] - ranked[1]["score"] >= 0.08:
                return ranked[:1]
        return ranked

    @staticmethod
    def _connected_boxes(mask: Any) -> list[tuple[int, int, int, int]]:
        """Find bounded connected regions in a small boolean image without OpenCV."""
        import numpy as np

        height, width = mask.shape
        visited = np.zeros(mask.shape, dtype=np.bool_)
        boxes: list[tuple[int, int, int, int]] = []
        for start_y, start_x in zip(*np.nonzero(mask & ~visited)):
            if visited[start_y, start_x]:
                continue
            stack = [(int(start_x), int(start_y))]
            visited[start_y, start_x] = True
            left = right = int(start_x)
            top = bottom = int(start_y)
            count = 0
            while stack:
                x, y = stack.pop()
                count += 1
                left, right = min(left, x), max(right, x)
                top, bottom = min(top, y), max(bottom, y)
                for ny in range(max(0, y - 1), min(height, y + 2)):
                    for nx in range(max(0, x - 1), min(width, x + 2)):
                        if mask[ny, nx] and not visited[ny, nx]:
                            visited[ny, nx] = True
                            stack.append((nx, ny))
            box_width, box_height = right - left + 1, bottom - top + 1
            if count >= 6 and box_width >= 3 and box_height >= 3:
                boxes.append((left, top, right + 1, bottom + 1))
            if len(boxes) >= 500:
                break
        return boxes

    @classmethod
    def _visual_descriptor_matches(
        cls, observation: VisionObservation, query: str,
    ) -> list[dict[str, Any]]:
        """Ground descriptor-only canvas targets from local pixels, never semantics."""
        parts = cls._query_parts(query)
        if parts["content"] or observation.image is None:
            return []
        descriptor_count = sum(bool(parts[key]) for key in ("positions", "colors", "shapes", "roles"))
        if descriptor_count < 2:
            return []
        if observation.redacted_password_fields and parts["colors"] & {"black", "gray"}:
            return []
        try:
            import numpy as np
            from PIL import Image

            scale = min(1.0, 320 / max(1, observation.width, observation.height))
            small = observation.image.resize(
                (max(1, round(observation.width * scale)), max(1, round(observation.height * scale))),
                Image.Resampling.BILINEAR,
            ).convert("RGB")
            rgb = np.asarray(small, dtype=np.int16)
            red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
            high = rgb.max(axis=2)
            low = rgb.min(axis=2)
            if parts["colors"]:
                masks = []
                for color in parts["colors"]:
                    if color == "red":
                        masks.append((red > 90) & (red > green * 1.28) & (red > blue * 1.20))
                    elif color == "orange":
                        masks.append((red > 120) & (green > 55) & (red > green * 1.10) & (green > blue * 1.25))
                    elif color == "yellow":
                        masks.append((red > 135) & (green > 115) & (blue < np.minimum(red, green) * 0.72))
                    elif color == "green":
                        masks.append((green > 75) & (green > red * 1.15) & (green > blue * 1.10))
                    elif color == "blue":
                        masks.append((blue > 75) & (blue > red * 1.15) & (blue > green * 1.08))
                    elif color in {"purple", "pink"}:
                        masks.append((red > 90) & (blue > 75) & (green < np.minimum(red, blue) * 0.88))
                    elif color == "black":
                        masks.append(high < 55)
                    elif color == "white":
                        masks.append(low > 215)
                    else:
                        masks.append((high - low < 24) & (high >= 55) & (low <= 215))
                mask = np.logical_or.reduce(masks)
            else:
                gray = rgb.mean(axis=2)
                edge = np.zeros(gray.shape, dtype=np.bool_)
                edge[:, 1:] |= np.abs(gray[:, 1:] - gray[:, :-1]) > 38
                edge[1:, :] |= np.abs(gray[1:, :] - gray[:-1, :]) > 38
                mask = edge.copy()
            # Join nearby boundary/color pixels while retaining separate icons.
            for _ in range(2):
                expanded = mask.copy()
                expanded[1:, :] |= mask[:-1, :]
                expanded[:-1, :] |= mask[1:, :]
                expanded[:, 1:] |= mask[:, :-1]
                expanded[:, :-1] |= mask[:, 1:]
                mask = expanded
            matches: list[dict[str, Any]] = []
            for small_left, small_top, small_right, small_bottom in cls._connected_boxes(mask):
                left = max(0, round(small_left / scale) - 3)
                top = max(0, round(small_top / scale) - 3)
                right = min(observation.width, round(small_right / scale) + 3)
                bottom = min(observation.height, round(small_bottom / scale) + 3)
                width, height = right - left, bottom - top
                if width < 8 or height < 8 or width * height > observation.width * observation.height * 0.18:
                    continue
                cx, cy = left + width / 2, top + height / 2
                if not cls._position_matches(parts["positions"], cx, cy, observation.width, observation.height):
                    continue
                color = cls._dominant_color(observation.image, (left, top, right, bottom)) if parts["colors"] else ""
                if parts["colors"] and color not in parts["colors"]:
                    continue
                aspect = width / max(1, height)
                if parts["shapes"] and not all((
                    shape in {"circle", "round", "square"} and 0.68 <= aspect <= 1.47
                    or shape == "wide" and aspect >= 1.6
                    or shape == "tall" and aspect <= 0.63
                ) for shape in parts["shapes"]):
                    continue
                score = min(0.97, 0.64 + 0.10 * descriptor_count)
                matches.append({
                    "text": "local pixel region", "score": round(score, 3), "line": 0,
                    "source": "local-pixel-grounding", "role": "canvas-region", "color": color,
                    "x": left, "y": top, "width": width, "height": height,
                    "screen_x": observation.origin_x + round(cx),
                    "screen_y": observation.origin_y + round(cy),
                })
            unique: list[dict[str, Any]] = []
            for item in sorted(matches, key=lambda value: (-value["score"], value["y"], value["x"])):
                if any(
                    abs(item["screen_x"] - saved["screen_x"]) < max(item["width"], saved["width"]) * 0.5
                    and abs(item["screen_y"] - saved["screen_y"]) < max(item["height"], saved["height"]) * 0.5
                    for saved in unique
                ):
                    continue
                unique.append(item)
            return unique[:20]
        except Exception:
            return []

    def _app_version_fingerprint(self, observation: VisionObservation) -> str:
        """Hash executable metadata without persisting its path."""
        try:
            resolver = getattr(self.control, "_process_path", None)
            path = resolver(observation.pid) if callable(resolver) and observation.pid else ""
            stat = os.stat(path) if path else None
            payload = f"{observation.process}|{stat.st_size}|{stat.st_mtime_ns}" if stat else observation.process
        except (OSError, RuntimeError, TypeError, ValueError):
            payload = observation.process
        return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:20]

    def _rank_selector_candidates(
        self, observation: VisionObservation, query: str,
        candidates: list[dict[str, Any]], elements: list[dict[str, Any]], *,
        role: str = "", anchor: str = "",
    ) -> list[dict[str, Any]]:
        return self.selector_engine.rank(
            candidates,
            process=observation.process,
            version_fingerprint=self._app_version_fingerprint(observation),
            query=query,
            role=role,
            anchor=anchor,
            elements=elements,
        )

    def _locate_details(
        self, observation: VisionObservation, query: str, language: str = "en", *,
        allow_vlm: bool = True, role: str = "", anchor: str = "",
    ) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
        if query.casefold().strip().startswith("template:"):
            matches = self._template_matches(observation, query.split(":", 1)[1].strip())
            ranked = self._rank_selector_candidates(
                observation, query, matches, [], role=role, anchor=anchor,
            )
            return ranked, "local-template" if ranked else "none", []
        candidates = self.locate(observation, query)
        # A unique exact OCR label is already a complete low-latency selector.
        # UIA remains the fresh-observation repair path if that label later
        # disappears, and is collected immediately when role/anchor context or
        # ambiguity requires evidence fusion.
        if (
            len(candidates) == 1
            and float(candidates[0].get("score") or 0.0) >= 0.995
            and not role and not anchor
        ):
            ranked = self._rank_selector_candidates(
                observation, query, candidates, [], role=role, anchor=anchor,
            )
            return ranked, "ocr", []
        elements: list[dict[str, Any]] = []
        try:
            elements = self.control.ui_elements(observation.handle)
            candidates.extend(self._semantic_matches(observation, query, elements))
        except (AttributeError, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
            pass
        candidates.extend(self._visual_descriptor_matches(observation, query))
        if not candidates:
            candidates.extend(self._enhanced_ocr_matches(observation, query, language))
        if not candidates and allow_vlm:
            candidates.extend(self._florence_matches(observation, query, elements))
        ranked = self._rank_selector_candidates(
            observation, query, candidates, elements, role=role, anchor=anchor,
        )
        source = str(ranked[0].get("source") or "none") if ranked else "none"
        return ranked, source, elements

    def _florence_matches(
        self, observation: VisionObservation, query: str,
        elements: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Use the isolated local VLM only after every deterministic locator misses."""
        if observation.image is None or observation.redacted_password_fields:
            return []
        if not _norm(query) or query.casefold().strip().startswith("template:"):
            return []
        try:
            rows = self.florence.ground(observation.image, query)
        except Exception:
            return []
        matches: list[dict[str, Any]] = []
        for row in rows[:20]:
            try:
                left, top, right, bottom = [int(value) for value in row.get("box", [])]
            except (TypeError, ValueError):
                continue
            left, top = max(0, left), max(0, top)
            right, bottom = min(observation.width, right), min(observation.height, bottom)
            width, height = right - left, bottom - top
            if width < 4 or height < 4:
                continue
            if width * height > observation.width * observation.height * 0.35:
                continue
            query_tokens = {token for token in _norm(query).split() if len(token) >= 3}
            conflicting_labels: list[str] = []
            for line in observation.lines:
                for word in line:
                    cx, cy = word.x + word.width / 2, word.y + word.height / 2
                    label = _norm(word.text)
                    if label and left <= cx <= right and top <= cy <= bottom:
                        conflicting_labels.append(label)
            for item in elements or []:
                item_left = int(item.get("x") or 0) - observation.origin_x
                item_top = int(item.get("y") or 0) - observation.origin_y
                item_width = int(item.get("width") or 0)
                item_height = int(item.get("height") or 0)
                cx, cy = item_left + item_width / 2, item_top + item_height / 2
                label = _norm(" ".join((
                    str(item.get("name") or ""), str(item.get("id") or ""),
                    str(item.get("help") or ""),
                )))
                if label and left <= cx <= right and top <= cy <= bottom:
                    conflicting_labels.append(label)
            if any(
                not (query_tokens & set(label.split()))
                for label in conflicting_labels
            ):
                # A model box covering a differently-labelled control is a
                # semantic contradiction, not a usable click proposal.
                continue
            matches.append({
                "text": str(row.get("label") or query)[:300],
                "score": 0.84,
                "line": 0,
                "source": "local-vlm",
                "role": "semantic-visual-region",
                "x": left,
                "y": top,
                "width": width,
                "height": height,
                "screen_x": observation.origin_x + left + width // 2,
                "screen_y": observation.origin_y + top + height // 2,
            })
        return matches

    @staticmethod
    def _template_slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
        if not slug:
            raise ValueError("Template name must include at least one ASCII letter or number")
        return slug[:80]

    def _template_path(self, name: str) -> Path:
        return self.template_dir / f"{self._template_slug(name)}.png"

    def learn_visual_target(
        self, window: str, name: str, source: str, occurrence: int = 0,
        language: str = "en",
    ) -> str:
        """Explicitly store one small redacted target crop for future local matching."""
        if source.casefold().strip().startswith("template:"):
            raise ValueError("A new template must be learned from visible text or a semantic target")
        observation = self.observe(window, language)
        matches, located_by, _elements = self._locate_details(observation, source, language)
        target, error = self._select_target(matches, int(occurrence))
        if target is None:
            return f"error: cannot learn visual target {name!r}: {error}; no template was saved"
        padding = 4
        box = (
            max(0, int(target["x"]) - padding),
            max(0, int(target["y"]) - padding),
            min(observation.width, int(target["x"]) + int(target["width"]) + padding),
            min(observation.height, int(target["y"]) + int(target["height"]) + padding),
        )
        width, height = box[2] - box[0], box[3] - box[1]
        if width < 8 or height < 8:
            return "error: selected visual target is too small to learn reliably; no template was saved"
        if width * height > min(observation.width * observation.height * 0.25, 120_000):
            return "error: selected region is too large for a safe target template; no template was saved"
        path = self._template_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        observation.image.crop(box).save(path, "PNG")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        return (
            f"Learned local visual target {name!r} from {source!r} via {located_by}; "
            f"template={path}; size={width}x{height}; hash={digest}; source screenshot was password-redacted"
        )

    def _template_matches(
        self, observation: VisionObservation, name: str,
    ) -> list[dict[str, Any]]:
        """Match a learned target locally with normalized correlation and NMS."""
        path = self._template_path(name)
        if not path.is_file() or observation.image is None:
            return []
        try:
            import numpy as np
            from PIL import Image

            screen_image = observation.image.convert("L")
            template_image = Image.open(path).convert("L")
            scale = min(1.0, 1200 / max(1, screen_image.width, screen_image.height))
            if scale < 1.0:
                screen_image = screen_image.resize(
                    (max(1, round(screen_image.width * scale)), max(1, round(screen_image.height * scale))),
                    Image.Resampling.BILINEAR,
                )
                template_image = template_image.resize(
                    (max(1, round(template_image.width * scale)), max(1, round(template_image.height * scale))),
                    Image.Resampling.BILINEAR,
                )
            screen = np.asarray(screen_image, dtype=np.float32)
            template = np.asarray(template_image, dtype=np.float32)
            th, tw = template.shape
            sh, sw = screen.shape
            if min(th, tw) < 5 or th > sh or tw > sw:
                return []
            centered_template = template - float(template.mean())
            template_norm = float(np.sqrt(np.square(centered_template).sum()))
            stride = max(2, min(th, tw) // 8)
            coarse: list[tuple[float, int, int]] = []
            for y in range(0, sh - th + 1, stride):
                row = np.lib.stride_tricks.sliding_window_view(
                    screen[y:y + th, :], (th, tw)
                )[0, ::stride]
                if template_norm < 1e-5:
                    scores = 1.0 - np.abs(row - template).mean(axis=(1, 2)) / 255.0
                else:
                    centered = row - row.mean(axis=(1, 2), keepdims=True)
                    norms = np.sqrt(np.square(centered).sum(axis=(1, 2))) * template_norm
                    scores = np.divide(
                        (centered * centered_template).sum(axis=(1, 2)), norms,
                        out=np.full(norms.shape, -1.0, dtype=np.float32), where=norms > 1e-5,
                    )
                for index in np.argpartition(scores, -min(3, len(scores)))[-min(3, len(scores)):]:
                    coarse.append((float(scores[index]), int(index) * stride, y))
            refined: list[tuple[float, int, int]] = []
            for _score, coarse_x, coarse_y in sorted(coarse, reverse=True)[:30]:
                for y in range(max(0, coarse_y - stride), min(sh - th, coarse_y + stride) + 1):
                    for x in range(max(0, coarse_x - stride), min(sw - tw, coarse_x + stride) + 1):
                        patch = screen[y:y + th, x:x + tw]
                        if template_norm < 1e-5:
                            score = 1.0 - float(np.abs(patch - template).mean()) / 255.0
                        else:
                            centered = patch - float(patch.mean())
                            norm = float(np.sqrt(np.square(centered).sum())) * template_norm
                            score = float((centered * centered_template).sum() / norm) if norm > 1e-5 else -1.0
                        refined.append((score, x, y))
            if not refined:
                return []
            best = max(score for score, _x, _y in refined)
            if best < 0.86:
                return []
            candidates: list[dict[str, Any]] = []
            for score, x, y in sorted(refined, reverse=True):
                if score < max(0.86, best - 0.025):
                    break
                native_x, native_y = round(x / scale), round(y / scale)
                native_w, native_h = round(tw / scale), round(th / scale)
                center_x, center_y = native_x + native_w // 2, native_y + native_h // 2
                if any(
                    abs(center_x - item["x"] - item["width"] // 2) < native_w * 0.45
                    and abs(center_y - item["y"] - item["height"] // 2) < native_h * 0.45
                    for item in candidates
                ):
                    continue
                candidates.append({
                    "text": f"template:{name}", "score": round(score, 3), "line": 0,
                    "source": "local-template", "x": native_x, "y": native_y,
                    "width": native_w, "height": native_h,
                    "screen_x": observation.origin_x + center_x,
                    "screen_y": observation.origin_y + center_y,
                })
                if len(candidates) >= 20:
                    break
            return candidates
        except Exception:
            return []

    def _enhanced_ocr_matches(
        self, observation: VisionObservation, query: str, language: str,
    ) -> list[dict[str, Any]]:
        """Retry a miss with one bounded local contrast/scale OCR pass."""
        if observation.image is None:
            return []
        try:
            from PIL import ImageFilter, ImageOps

            (_capture, _draw, _chops), winocr, image_module = self._dependencies()
            grayscale = ImageOps.autocontrast(observation.image.convert("L"))
            grayscale = grayscale.filter(ImageFilter.SHARPEN)
            max_dimension = max(grayscale.width, grayscale.height)
            scale = min(1.6, 2600 / max(1, max_dimension))
            if abs(scale - 1.0) > 0.01:
                enhanced = grayscale.resize(
                    (max(1, round(grayscale.width * scale)), max(1, round(grayscale.height * scale))),
                    image_module.Resampling.LANCZOS,
                )
            else:
                enhanced = grayscale
            result = winocr.recognize_pil_sync(enhanced.convert("RGB"), language or "en")
            _text, lines = self._parse_ocr(result)
            lines = self._restore_coordinates(lines, scale)
            retry = VisionObservation(
                title=observation.title, handle=observation.handle,
                origin_x=observation.origin_x, origin_y=observation.origin_y,
                width=observation.width, height=observation.height,
                image_hash=observation.image_hash, text=_text, lines=lines,
                image=observation.image, redacted_password_fields=observation.redacted_password_fields,
                ocr_scale=scale, pid=observation.pid, process=observation.process,
            )
            matches = self.locate(retry, query)
            for item in matches:
                item["source"] = "ocr-enhanced"
            return matches
        except Exception:
            # This is a best-effort local fallback after primary OCR and UIA
            # already missed. Its failure must not invent a target or weaken
            # the normal fail-closed path.
            return []

    def _locate_with_fallback(
        self, observation: VisionObservation, query: str, language: str = "en",
    ) -> tuple[list[dict[str, Any]], str]:
        matches, source, _elements = self._locate_details(observation, query, language)
        return matches, source

    @staticmethod
    def _semantic_signature(elements: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
        return tuple(sorted(
            (
                str(item.get("name") or ""),
                str(item.get("id") or ""),
                str(item.get("type") or ""),
                bool(item.get("enabled", True)),
                int(item.get("x") or 0),
                int(item.get("y") or 0),
                int(item.get("width") or 0),
                int(item.get("height") or 0),
            )
            for item in elements
        ))

    def _record_observation(
        self, observation: VisionObservation, elements: list[dict[str, Any]] | None = None,
    ) -> str:
        try:
            return self.state_graph.observe(observation, elements)
        except Exception:
            # State memory must never turn a safe read or click result into a
            # desktop failure. Its own status exposes persistence problems.
            return "unavailable"

    def _record_transition(
        self, before: VisionObservation, after: VisionObservation | None, *,
        text: str, source: str, verified: bool, outcome: str,
        before_elements: list[dict[str, Any]] | None = None,
        after_elements: list[dict[str, Any]] | None = None,
        contract: dict[str, Any] | None = None,
        contract_result: dict[str, Any] | None = None,
    ) -> str:
        try:
            return self.state_graph.transition(
                before, after, action="click_visual_text", target=text,
                source=source, verified=verified, outcome=outcome,
                before_elements=before_elements, after_elements=after_elements,
                contract=contract, contract_result=contract_result,
            )
        except Exception:
            return "unavailable"

    def _record_selector_result(self, target: dict[str, Any], verified: bool) -> None:
        """Best-effort strategy learning; never change an action's truth value."""
        try:
            self.selector_engine.record_result(target, verified)
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    def inspect_ui_state(self, window: str = "", language: str = "en") -> str:
        """Return a fresh structured UI world-state without delivering input."""
        observation = self.observe(window, language)
        try:
            elements = self.control.ui_elements(observation.handle)
        except (AttributeError, RuntimeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            elements = []
            warning = f"\nUI Automation warning: {type(exc).__name__}: {str(exc)[:160]}"
        else:
            warning = ""
        state_id = self._record_observation(observation, elements)
        return f"Observed state_id={state_id}.\n{self.state_graph.format_live(observation, elements)}{warning}"

    def verify_ui_state(
        self, window: str, expected: dict[str, Any], language: str = "en",
    ) -> str:
        """Evaluate an expected-state contract against one fresh, read-only observation."""
        observation = self.observe(window, language)
        try:
            elements = self.control.ui_elements(observation.handle)
        except (AttributeError, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
            elements = []
        state_id = self._record_observation(observation, elements)
        result = self.state_graph.evaluate_contract(observation, elements, expected)
        if result.get("error"):
            return f"error: {result['error']}; state_id={state_id}; no input was delivered"
        checks = "; ".join(
            f"{item.get('field')}={item.get('passed')} (expected={item.get('expected')!r}, actual={item.get('actual')!r})"
            for item in result.get("checks", [])
        ) or "no checks"
        prefix = "Verified UI state contract" if result.get("satisfied") else "error: UI state contract not satisfied"
        return f"{prefix}; {checks}; state_id={state_id}; no input was delivered"

    def observe_screen(self, window: str = "", language: str = "en", max_words: int = 120) -> str:
        observation = self.observe(window, language)
        state_id = self._record_observation(observation)
        words = [word for line in observation.lines for word in line]
        rows = [
            f"Vision observation: {observation.title!r} | {observation.width}x{observation.height} | hash={observation.image_hash} | state_id={state_id}",
            f"OCR text:\n{observation.text[:12_000] or '(no text recognized)'}",
            "Word boxes (window-relative):",
        ]
        rows.extend(
            f"{word.text!r} x={word.x} y={word.y} width={word.width} height={word.height}"
            for word in words[: max(1, min(int(max_words), 300))]
        )
        if observation.redacted_password_fields:
            rows.append(f"Privacy: redacted {observation.redacted_password_fields} password field(s) before OCR")
        if observation.ocr_scale != 1.0:
            rows.append(f"OCR optimization: image scaled to {observation.ocr_scale:.3f}; coordinates restored to native pixels")
        return "\n".join(rows)

    def find_visual_text(self, window: str, text: str, language: str = "en") -> str:
        observation = self.observe(window, language)
        matches, source, elements = self._locate_details(
            observation, text, language, allow_vlm=False,
        )
        state_id = self._record_observation(observation, elements)
        if not matches:
            return f"No visual text matched {text!r} in {observation.title!r}; state_id={state_id}."
        return f"Observed state_id={state_id}.\n" + "\n".join(
            f"{index}. {item['text']!r} score={item['score']} source={source} window_box=({item['x']},{item['y']},{item['width']},{item['height']}) screen_center=({item['screen_x']},{item['screen_y']})"
            for index, item in enumerate(matches[:20], start=1)
        )

    def find_visual_target(
        self, window: str, target: str, language: str = "en", *,
        role: str = "", anchor: str = "",
    ) -> str:
        """Find text, semantic UI descriptions, or an explicit learned template."""
        observation = self.observe(window, language)
        matches, source, elements = self._locate_details(
            observation, target, language, role=role, anchor=anchor,
        )
        state_id = self._record_observation(observation, elements)
        if not matches:
            if target.casefold().strip().startswith("template:"):
                name = target.split(":", 1)[1].strip()
                if not self._template_path(name).is_file():
                    return f"No learned local template named {name!r}; state_id={state_id}."
            return f"No visual target matched {target!r} in {observation.title!r}; state_id={state_id}."
        return f"Observed state_id={state_id}.\n" + "\n".join(
            f"{index}. {item['text']!r} score={item['score']} source={item.get('source') or source} "
            f"selector={item.get('selector_strategy') or 'unranked'} selector_score={item.get('selector_score', item['score'])} "
            f"role={item.get('role') or 'visual'} color={item.get('color') or 'unspecified'} "
            f"window_box=({item['x']},{item['y']},{item['width']},{item['height']}) "
            f"screen_center=({item['screen_x']},{item['screen_y']})"
            for index, item in enumerate(matches[:20], start=1)
        )

    def click_visual_target(
        self, window: str, target: str, occurrence: int = 0,
        button: str = "left", language: str = "en", *,
        max_scrolls: int = 0, direction: str = "down",
        expect_text: str = "", expect_absent_text: str = "",
        expect_window: str = "", timeout_seconds: float = 2.0,
        expected_state: dict[str, Any] | None = None,
        role: str = "", anchor: str = "",
    ) -> str:
        """Boundedly scroll for a grounded target, then use the verified click pipeline."""
        scroll_limit = max(0, min(int(max_scrolls), 12))
        if not _norm(target):
            return "error: visual target must include at least one letter or number; no input was delivered"
        if target.casefold().strip().startswith("template:") and not target.split(":", 1)[1].strip():
            return "error: template target requires a learned template name; no input was delivered"
        scroll_direction = direction.casefold().strip()
        if scroll_direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        seen_hashes: set[str] = set()
        for scroll_count in range(scroll_limit + 1):
            if self._action_abort_requested():
                return "error: action deadline expired during visual search; no click was delivered"
            observation = self.observe(window, language)
            matches, _source, _elements = self._locate_details(
                observation, target, language, role=role, anchor=anchor,
            )
            if matches:
                result = self.click_visual_text(
                    window, target, occurrence, button, language,
                    expect_text=expect_text, expect_absent_text=expect_absent_text,
                    expect_window=expect_window, timeout_seconds=timeout_seconds,
                    expected_state=expected_state,
                    role=role, anchor=anchor,
                )
                if result.casefold().startswith("error:"):
                    return f"error: scroll_search={scroll_count}; {result[6:].lstrip()}"
                if result.casefold().startswith("denied"):
                    return f"denied: scroll_search={scroll_count}; {result}"
                return f"scroll_search={scroll_count}; {result}"
            if observation.image_hash in seen_hashes:
                return (
                    f"error: visual scroll search reached a repeated state after {scroll_count} scroll(s); "
                    f"target {target!r} was not found; no click was delivered"
                )
            seen_hashes.add(observation.image_hash)
            if scroll_count >= scroll_limit:
                break
            expected_rect = [
                observation.origin_x, observation.origin_y,
                observation.origin_x + observation.width,
                observation.origin_y + observation.height,
            ]
            x = observation.origin_x + observation.width // 2
            y = observation.origin_y + observation.height // 2
            amount = -4 if scroll_direction == "down" else 4
            guarded_scroll = getattr(type(self.control), "guarded_scroll", None)
            if callable(guarded_scroll):
                delivered = guarded_scroll(
                    self.control, observation.handle, x, y, expected_rect, amount,
                )
            else:
                delivered = self.control.mouse_action("scroll", x, y, amount=amount)
            if delivered.lower().startswith(("error:", "denied")):
                return delivered
            time.sleep(0.25)
        return (
            f"error: no visual target matched {target!r} after {scroll_limit} bounded scroll(s); "
            "no click was delivered"
        )

    @staticmethod
    def _change_metrics(
        before: VisionObservation, after: VisionObservation,
        target: dict[str, Any] | None = None,
    ) -> tuple[float, float]:
        if before.width != after.width or before.height != after.height:
            return 1.0, 1.0
        try:
            from PIL import ImageChops
            difference_rgb = ImageChops.difference(before.image, after.image).convert("RGB")
            bands = difference_rgb.split()
            difference = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
            histogram = difference.histogram()
            changed = sum(count for level, count in enumerate(histogram) if level >= 12)
            global_ratio = changed / max(1, before.width * before.height)
            if target is None:
                return global_ratio, 0.0
            padding = 8
            left = max(0, int(target["x"]) - padding)
            top = max(0, int(target["y"]) - padding)
            right = min(before.width, int(target["x"]) + int(target["width"]) + padding)
            bottom = min(before.height, int(target["y"]) + int(target["height"]) + padding)
            if right <= left or bottom <= top:
                return global_ratio, 0.0
            local = difference.crop((left, top, right, bottom))
            local_changed = sum(
                count for level, count in enumerate(local.histogram()) if level >= 12
            )
            return global_ratio, local_changed / max(1, local.width * local.height)
        except Exception:
            # Never turn missing/corrupt image evidence into a false success.
            return 0.0, 0.0

    @staticmethod
    def _change_ratio(before: VisionObservation, after: VisionObservation) -> float:
        return WindowsVision._change_metrics(before, after)[0]

    @staticmethod
    def _select_target(
        matches: list[dict[str, Any]], occurrence: int,
    ) -> tuple[dict[str, Any] | None, str]:
        if not matches:
            return None, "no visual target matched"
        if occurrence <= 0 and len(matches) != 1:
            choices = ", ".join(
                f"{index}:{item['text']}" for index, item in enumerate(matches[:8], 1)
            )
            return None, (
                f"visual target is ambiguous ({len(matches)} matches): {choices}; "
                "specify occurrence"
            )
        index = 0 if occurrence <= 0 else occurrence - 1
        if index >= len(matches):
            return None, f"occurrence {occurrence} exceeds {len(matches)} visual match(es)"
        return matches[index], ""

    @staticmethod
    def _target_moved(first: dict[str, Any], second: dict[str, Any]) -> bool:
        """Detect meaningful target motion while tolerating small OCR-box jitter."""
        first_center = (float(first["screen_x"]), float(first["screen_y"]))
        second_center = (float(second["screen_x"]), float(second["screen_y"]))
        distance = math.hypot(first_center[0] - second_center[0], first_center[1] - second_center[1])
        tolerance = max(8.0, min(
            float(first.get("width") or 1), float(first.get("height") or 1),
            float(second.get("width") or 1), float(second.get("height") or 1),
        ) * 0.30)
        first_box = (
            int(first["x"]), int(first["y"]),
            int(first["x"]) + int(first["width"]), int(first["y"]) + int(first["height"]),
        )
        second_box = (
            int(second["x"]), int(second["y"]),
            int(second["x"]) + int(second["width"]), int(second["y"]) + int(second["height"]),
        )
        intersection = max(0, min(first_box[2], second_box[2]) - max(first_box[0], second_box[0])) * max(
            0, min(first_box[3], second_box[3]) - max(first_box[1], second_box[1])
        )
        union = (
            max(1, int(first["width"]) * int(first["height"]))
            + max(1, int(second["width"]) * int(second["height"])) - intersection
        )
        iou = intersection / max(1, union)
        return distance > tolerance or iou < 0.45

    def _text_present(
        self, observation: VisionObservation, query: str,
        elements: list[dict[str, Any]] | None = None,
    ) -> bool:
        if not query.strip():
            return False
        if self.locate(observation, query):
            return True
        # Postconditions need identity, not coordinates. Windows OCR may wrap
        # one button label across adjacent lines; an exact normalized phrase in
        # the full OCR stream is valid positive evidence without inventing a
        # click location.
        wanted = _norm(query)
        if len(wanted) >= 4 and wanted in _norm(observation.text):
            return True
        return bool(self._semantic_matches(observation, query, elements or []))

    @staticmethod
    def _window_matches(observation: VisionObservation, expected: str) -> bool:
        wanted = _norm(expected)
        if not wanted:
            return False
        identities = (_norm(observation.title), _norm(observation.process))
        return any(wanted == value or wanted in value for value in identities if value)

    def wait_for_visual_text(
        self, window: str, text: str, condition: str = "present",
        timeout_seconds: float = 5.0, language: str = "en",
    ) -> str:
        """Wait for a visual label without delivering keyboard or mouse input."""
        desired = condition.casefold().strip()
        if desired not in {"present", "absent"}:
            raise ValueError("condition must be present or absent")
        requested_timeout = float(timeout_seconds)
        if not math.isfinite(requested_timeout):
            raise ValueError("timeout_seconds must be finite")
        timeout = min(max(requested_timeout, 0.2), 10.0)
        deadline = time.monotonic() + timeout
        absence_streak = 0
        attempts = 0
        last_state = "unavailable"
        while True:
            attempts += 1
            observation = self.observe(window, language)
            matches, source, elements = self._locate_details(
                observation, text, language, allow_vlm=False,
            )
            last_state = self._record_observation(observation, elements)
            present = bool(matches)
            if desired == "present" and present:
                return (
                    f"Verified visual text {text!r} is present in {observation.title!r}; "
                    f"matches={len(matches)}; source={source}; attempts={attempts}; state_id={last_state}"
                )
            if desired == "absent":
                absence_streak = absence_streak + 1 if not present else 0
                # Negative OCR is weaker evidence than a positive match, so
                # require two independent observations before claiming absence.
                if absence_streak >= 2:
                    return (
                        f"Verified visual text {text!r} is absent from {observation.title!r} "
                        f"across two observations; attempts={attempts}; state_id={last_state}"
                    )
            if time.monotonic() >= deadline:
                state = "present" if present else "absent"
                return (
                    f"error: timed out waiting for {text!r} to become {desired}; "
                    f"last_observation={state}; attempts={attempts}; state_id={last_state}"
                )
            time.sleep(min(0.25, max(0.02, deadline - time.monotonic())))

    def click_visual_text(
        self, window: str, text: str, occurrence: int = 0,
        button: str = "left", language: str = "en", *,
        expect_text: str = "", expect_absent_text: str = "",
        expect_window: str = "", timeout_seconds: float = 2.0,
        expected_state: dict[str, Any] | None = None,
        role: str = "", anchor: str = "",
    ) -> str:
        if not _norm(text):
            return "error: visual target must include at least one letter or number; no click was delivered"
        for label, value in (
            ("expect_text", expect_text),
            ("expect_absent_text", expect_absent_text),
            ("expect_window", expect_window),
            ("role", role),
            ("anchor", anchor),
        ):
            if value and not _norm(value):
                return f"error: {label} must include at least one letter or number; no click was delivered"
        requested_timeout = float(timeout_seconds)
        if not math.isfinite(requested_timeout):
            return "error: timeout_seconds must be finite; no click was delivered"
        state_contract = expected_state or {}
        contract_validation = self.state_graph.evaluate_contract(None, None, state_contract) if state_contract else {}
        if contract_validation.get("error"):
            return f"error: {contract_validation['error']}; no click was delivered"
        if self._action_abort_requested():
            return "error: action deadline expired before visual observation; no click was delivered"
        initial = self.observe(window, language)
        matches, source, before_elements = self._locate_details(
            initial, text, language, role=role, anchor=anchor,
        )
        if not matches:
            return f"error: no visual text matched {text!r} in {initial.title!r}"
        if source == "local-vlm" and not any((expect_text, expect_absent_text, expect_window, state_contract)):
            return (
                "error: Florence semantic targets require an explicit expected text, absent text, "
                "resulting window, or structured expected state before clicking; no click was delivered"
            )
        target, selection_error = self._select_target(matches, occurrence)
        if target is None:
            return f"error: {selection_error}"
        initial_target = dict(target)
        repair_method = "initial ranked selector"

        # Re-capture and re-locate immediately before input. This closes the
        # same-window stale-content gap that geometry checks alone cannot see.
        try:
            before = self.observe("", language, focus=False)
        except (RuntimeError, ValueError) as exc:
            return f"error: target could not be revalidated immediately before click: {exc}"
        if before.handle != initial.handle:
            return "error: foreground window changed during visual target revalidation; no click was delivered"
        matches, source, before_elements = self._locate_details(
            before, text, language, role=role, anchor=anchor,
        )
        target, repair_method = self.selector_engine.revalidate(
            initial_target, matches, occurrence,
        )
        if target is None:
            return f"error: target changed during visual revalidation ({repair_method}); no click was delivered"
        if self._target_moved(initial_target, target):
            # One relocation may be layout settling or an animated target. A
            # third immediate observation must show a stable target before any
            # input is trusted.
            second_target = dict(target)
            try:
                stable = self.observe("", language, focus=False)
            except (RuntimeError, ValueError) as exc:
                return f"error: moving target could not be stabilized before click: {exc}; no click was delivered"
            if stable.handle != initial.handle:
                return "error: foreground window changed while stabilizing moving target; no click was delivered"
            stable_matches, source, before_elements = self._locate_details(
                stable, text, language, role=role, anchor=anchor,
            )
            stable_target, stabilization_method = self.selector_engine.revalidate(
                second_target, stable_matches, occurrence,
            )
            if stable_target is None:
                return f"error: moving target disappeared during stabilization ({stabilization_method}); no click was delivered"
            if self._target_moved(second_target, stable_target):
                return "error: visual target is still moving; wait for it to settle before retrying; no click was delivered"
            before, target = stable, stable_target
            repair_method = stabilization_method

        semantic_before: tuple[tuple[Any, ...], ...] = ()
        if (expect_text or expect_absent_text or state_contract) and not before_elements:
            try:
                before_elements = self.control.ui_elements(before.handle)
            except (AttributeError, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if before_elements:
            semantic_before = self._semantic_signature(before_elements)
        if expect_absent_text and not self._text_present(before, expect_absent_text, before_elements):
            return (
                f"error: cannot verify disappearance of {expect_absent_text!r} because it was not "
                "present before the click; no click was delivered"
            )
        expected_text_was_present = bool(
            expect_text and self._text_present(before, expect_text, before_elements)
        )
        expected_window_was_current = bool(
            expect_window and self._window_matches(before, expect_window)
        )
        before_contract_result = (
            self.state_graph.evaluate_contract(before, before_elements, state_contract)
            if state_contract else {}
        )
        state_was_satisfied = bool(before_contract_result.get("satisfied"))
        relative_x = int(target["screen_x"]) - before.origin_x
        relative_y = int(target["screen_y"]) - before.origin_y
        if not (0 <= relative_x < before.width and 0 <= relative_y < before.height):
            return "error: visual target coordinates fall outside the observed window; no click was delivered"
        current = next(
            (item for item in self.control.windows() if item["handle"] == before.handle),
            None,
        )
        if current is None:
            return "error: target window closed after observation; no click was delivered"
        current_rect = [int(value) for value in current["rect"]]
        expected_rect = [
            before.origin_x, before.origin_y,
            before.origin_x + before.width, before.origin_y + before.height,
        ]
        if current_rect != expected_rect:
            return "error: target window moved or resized after observation; no stale-coordinate click was delivered"
        if int(self.control.user32.GetForegroundWindow() or 0) != before.handle:
            return "error: foreground window changed after observation; no click was delivered"
        if self._action_abort_requested():
            return "error: action deadline expired after revalidation; no click was delivered"
        point = wintypes.POINT(int(target["screen_x"]), int(target["screen_y"]))
        hit = int(self.control.user32.WindowFromPoint(point) or 0)
        hit_root = int(self.control.user32.GetAncestor(hit, 2) or 0) if hit else 0  # GA_ROOT
        if hit_root != before.handle:
            return "error: another window covers the visual target; no click was delivered"
        delivered = ""
        guarded_invoke = getattr(type(self.control), "guarded_invoke_at_point", None)
        if button == "left" and callable(guarded_invoke):
            invoked = guarded_invoke(
                self.control, before.handle, int(target["screen_x"]), int(target["screen_y"]),
                expected_rect,
            )
            if not invoked.lower().startswith("unsupported:"):
                delivered = invoked
        if not delivered:
            guarded_click = getattr(type(self.control), "guarded_click", None)
            if callable(guarded_click):
                delivered = guarded_click(
                    self.control, before.handle, int(target["screen_x"]), int(target["screen_y"]),
                    expected_rect, button,
                )
            else:
                delivered = self.control.mouse_action(
                    "click", int(target["screen_x"]), int(target["screen_y"]), button
                )
        if delivered.lower().startswith(("error:", "denied")):
            return delivered
        timeout = min(max(requested_timeout, 0.2), 5.0)
        deadline = time.monotonic() + timeout
        absence_streak = 0
        last_after: VisionObservation | None = None
        last_elements: list[dict[str, Any]] = []
        last_evidence = "no result captured"
        last_contract_result: dict[str, Any] = {}
        attempts = 0
        time.sleep(min(0.15, timeout))
        while True:
            if self._action_abort_requested():
                return "error: action deadline expired during postcondition verification; late success discarded"
            attempts += 1
            rows = self.control.windows()
            original = next((item for item in rows if item["handle"] == before.handle), None)
            if original is None:
                close_satisfies = bool(expect_absent_text) and not expect_text and not expect_window
                legacy_requested = any((expect_text, expect_absent_text, expect_window))
                last_contract_result = (
                    self.state_graph.evaluate_contract(None, None, state_contract)
                    if state_contract else {}
                )
                verified = (not legacy_requested or close_satisfies) and (
                    not state_contract or bool(last_contract_result.get("satisfied"))
                )
                transition = self._record_transition(
                    before, None, text=text, source=source, verified=verified,
                    outcome="target-window-closed", before_elements=before_elements,
                    contract=state_contract, contract_result=last_contract_result,
                )
                if verified:
                    self._record_selector_result(target, True)
                    return (
                        f"Verified visual action: target window closed after click on {target['text']!r}; "
                        f"selector={target.get('selector_strategy', source)}; repair={repair_method}; "
                        f"attempts={attempts}; input={delivered}; state_transition={transition}"
                    )
                self._record_selector_result(target, False)
                return (
                    f"error: target window closed before requested postconditions could be verified; "
                    f"state_transition={transition}"
                )
            foreground = next((item for item in rows if item.get("foreground")), None)
            if foreground is None:
                last_evidence = "no foreground window"
            else:
                same_process = (
                    before.pid > 0 and int(foreground.get("pid") or 0) == before.pid
                ) or (
                    bool(before.process)
                    and str(foreground.get("process") or "").casefold() == before.process.casefold()
                )
                wanted_window = _norm(expect_window)
                foreground_expected = bool(
                    wanted_window and (
                        wanted_window in _norm(str(foreground.get("title") or ""))
                        or wanted_window in _norm(str(foreground.get("process") or ""))
                    )
                )
                if int(foreground["handle"]) != before.handle and not same_process and not foreground_expected:
                    transition = self._record_transition(
                        before, None, text=text, source=source, verified=False,
                        outcome="focus-left-application", before_elements=before_elements,
                        contract=state_contract,
                    )
                    self._record_selector_result(target, False)
                    return (
                        f"error: click was delivered to {target['text']!r}, but focus moved to another "
                        f"application {foreground['title']!r}; inspect it before claiming success; "
                        f"state_transition={transition}"
                    )
                try:
                    after = self.observe("", language, focus=False)
                except ValueError as exc:
                    transition = self._record_transition(
                        before, None, text=text, source=source, verified=False,
                        outcome="protected-result", before_elements=before_elements,
                        contract=state_contract,
                    )
                    self._record_selector_result(target, False)
                    return (
                        f"error: click opened a protected or unsupported surface: {exc}; "
                        f"state_transition={transition}"
                    )
                except RuntimeError:
                    last_evidence = "resulting foreground could not be captured"
                else:
                    last_after = after
                    ratio, target_ratio = self._change_metrics(before, after, target)
                    text_changed = before.text != after.text
                    after_elements: list[dict[str, Any]] = []
                    semantic_changed = False
                    if semantic_before or expect_text or expect_absent_text or state_contract:
                        try:
                            after_elements = self.control.ui_elements(after.handle)
                            semantic_changed = bool(
                                semantic_before
                                and semantic_before != self._semantic_signature(after_elements)
                            )
                        except (AttributeError, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
                            pass
                    last_elements = after_elements
                    window_changed = before.handle != after.handle
                    observable_change = (
                        window_changed or text_changed or semantic_changed
                        or target_ratio >= 0.08 or ratio >= 0.02
                    )
                    checks: list[bool] = []
                    labels: list[str] = []
                    if expect_text:
                        present = self._text_present(after, expect_text, after_elements)
                        checks.append(present and (observable_change or not expected_text_was_present))
                        labels.append(f"expect_text={present}")
                    if expect_absent_text:
                        absent = not self._text_present(after, expect_absent_text, after_elements)
                        absence_streak = absence_streak + 1 if absent else 0
                        checks.append(absence_streak >= 2)
                        labels.append(f"expect_absent={absent},stable_samples={absence_streak}")
                    if expect_window:
                        matched = self._window_matches(after, expect_window)
                        checks.append(matched and (observable_change or not expected_window_was_current))
                        labels.append(f"expect_window={matched}")
                    if state_contract:
                        last_contract_result = self.state_graph.evaluate_contract(
                            after, after_elements, state_contract,
                        )
                        state_ok = bool(last_contract_result.get("satisfied"))
                        checks.append(state_ok and (observable_change or not state_was_satisfied))
                        passed = sum(bool(item.get("passed")) for item in last_contract_result.get("checks", []))
                        labels.append(
                            f"expected_state={state_ok},checks={passed}/{len(last_contract_result.get('checks', []))}"
                        )
                    verified = all(checks) if checks else observable_change
                    last_evidence = (
                        f"global_change={ratio:.3%}, target_change={target_ratio:.3%}, "
                        f"ocr_changed={text_changed}, semantic_changed={semantic_changed}, "
                        f"window_changed={window_changed}"
                        + (", " + ", ".join(labels) if labels else "")
                    )
                    if verified:
                        transition = self._record_transition(
                            before, after, text=text, source=source, verified=True,
                            outcome="postconditions-verified" if checks else "observed-response",
                            before_elements=before_elements, after_elements=after_elements,
                            contract=state_contract, contract_result=last_contract_result,
                        )
                        self._record_selector_result(target, True)
                        return (
                            f"Verified visual action on {target['text']!r} via {source}; {last_evidence}; "
                            f"selector={target.get('selector_strategy', source)}; repair={repair_method}; "
                            f"attempts={attempts}; resulting_window={after.title!r}; "
                            f"before={before.image_hash}; after={after.image_hash}; "
                            f"state_transition={transition}"
                        )
            if time.monotonic() >= deadline:
                transition = self._record_transition(
                    before, last_after, text=text, source=source, verified=False,
                    outcome="postcondition-timeout" if any((expect_text, expect_absent_text, expect_window, state_contract))
                    else "no-observed-response",
                    before_elements=before_elements, after_elements=last_elements,
                    contract=state_contract, contract_result=last_contract_result,
                )
                self._record_selector_result(target, False)
                return (
                    f"error: click was delivered to {target['text']!r}, but verification timed out; "
                    f"{last_evidence}; attempts={attempts}; input={delivered}; state_transition={transition}"
                )
            time.sleep(min(0.25, max(0.02, deadline - time.monotonic())))

    def save_diagnostic_snapshot(self, window: str, language: str = "en") -> str:
        observation = self.observe(window, language)
        folder = self.state_dir / "vision"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "latest-redacted.png"
        observation.image.save(path, "PNG")
        return (
            f"Saved redacted diagnostic snapshot: {path} | hash={observation.image_hash} | "
            f"password_fields_redacted={observation.redacted_password_fields}"
        )
