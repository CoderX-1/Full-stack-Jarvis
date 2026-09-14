"""Project-control foundation for JARVIS Mark II."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from windows_control import WindowsControl
from windows_vision import WindowsVision


SKIP_DIRS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
}

READ_ONLY_MARK2_TOOLS = {
    "list_projects",
    "discover_projects",
    "project_status",
    "git_status",
    "search_project_files",
    "check_local_url",
    "recent_audit",
    "list_installed_apps",
    "list_windows",
    "inspect_ui",
    "read_window_text",
    "list_known_folders",
    "find_files",
    "vision_status",
    "ui_state_graph_status",
    "recent_ui_transitions",
    "inspect_ui_state",
    "verify_ui_state",
    "observe_screen",
    "find_visual_text",
    "find_visual_target",
    "wait_for_visual_text",
}

UI_STATE_CONTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "window": {"type": "string"},
        "operation": {"type": "string", "enum": ["idle", "busy", "blocked", "error"]},
        "modified": {"type": "boolean"},
        "modal": {"oneOf": [{"type": "boolean"}, {"type": "string", "enum": ["present", "absent"]}]},
        "active_tab": {"type": "string"},
        "focused": {"oneOf": [{"type": "string"}, {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "role": {"type": "string"}}}]},
        "control": {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "present": {"type": "boolean"}, "enabled": {"type": "boolean"}, "focused": {"type": "boolean"}, "selected": {"type": "boolean"}, "toggle_state": {"type": "string"}, "expand_state": {"type": "string"}}},
        "progress_at_least": {"type": "number", "minimum": 0, "maximum": 100},
    },
    "minProperties": 1,
}

MARK2_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "List projects registered with JARVIS.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_projects",
            "description": "Find likely project folders directly inside the agent home without registering or changing them.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_project",
            "description": "Register an exact project path and its approved commands. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "start_command": {"type": "string"},
                    "build_command": {"type": "string"},
                    "url": {"type": "string"},
                    "description": {"type": "string"},
                    "open_command": {"type": "string"},
                },
                "required": ["name", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_status",
            "description": "Report registered metadata, current-session process state, localhost health, and git status.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Read git branch and working-tree status for a registered project.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_project_files",
            "description": "Safely search filenames and UTF-8 text inside a registered project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["name", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_local_url",
            "description": "Check an HTTP endpoint restricted to localhost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_project",
            "description": "Open a registered project in its configured app or the system file browser. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_project",
            "description": "Start only the registered dev command and verify its process or localhost URL. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verify_timeout": {"type": "integer", "minimum": 1, "maximum": 60},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_project",
            "description": "Stop only a project process started by this JARVIS session. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_project_build",
            "description": "Run only the registered build command and verify its exit code. Requires permission.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 1800},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_audit",
            "description": "Read recent JARVIS Mark II tool audit records.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
]
MARK2_TOOLS.extend([
    {
        "type": "function",
        "function": {
            "name": "position_window",
            "description": "Place any window in a region of its own monitor work area, automatically detecting screen size. Coordinates are fractions 0..1: top-right quarter is x=.5,y=0,width=.5,height=.5. Use exact selector from list_windows for duplicate titles. Verifies resulting geometry.",
            "parameters": {"type": "object", "properties": {
                "window": {"type": "string"},
                "x": {"type": "number", "minimum": 0, "maximum": 1},
                "y": {"type": "number", "minimum": 0, "maximum": 1},
                "width": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "height": {"type": "number", "exclusiveMinimum": 0, "maximum": 1}
            }, "required": ["window", "x", "y", "width", "height"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vision_status",
            "description": "Report whether the local, offline Vision-Control Engine is ready and which privacy protections are active.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ui_state_graph_status",
            "description": "Report privacy-safe UI State Graph health, bounded state/transition counts, and persistence status. The graph is observational evidence and never grants permission or blindly replays actions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_ui_transitions",
            "description": "Read recent privacy-safe UI transitions. Labels, OCR text, screenshots, and window titles are not stored; use this only as historical evidence and re-observe before acting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_ui_state",
            "description": "Build a fresh structured world-state for one window: control hierarchy, focus, active tabs, selection/toggle/expand state, modal, modified, progress, error, and operation state. Read-only; labels are live and are not persisted in the graph or audit log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "description": "Unique title or process; empty means foreground"},
                    "language": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_ui_state",
            "description": "Read-only verification of a machine-checkable expected UI state against a fresh observation. Does not click or type. Use after an action when completion depends on focus, selected tab/control state, modal state, operation state, modified state, progress, or resulting window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "language": {"type": "string"},
                    "expected": UI_STATE_CONTRACT_SCHEMA,
                },
                "required": ["window", "expected"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe_screen",
            "description": "Capture one exact visible app window, redact password fields, run offline Windows OCR, and return recognized text with window-relative boxes. Use when UI Automation cannot describe the screen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "description": "Unique title or process; empty means the foreground window"},
                    "language": {"type": "string", "description": "OCR language such as en"},
                    "max_words": {"type": "integer", "minimum": 1, "maximum": 300},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_visual_text",
            "description": "Find visible text in an app screenshot with fuzzy OCR matching. If OCR misses a named control, use its current UI Automation bounds as a one-shot fallback. Return precise boxes without clicking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "text": {"type": "string"},
                    "language": {"type": "string"},
                },
                "required": ["window", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_visual_text",
            "description": "Wait without input until visible text becomes present or absent. Uses repeated local OCR/UIA observations and requires two observations before claiming absence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "text": {"type": "string"},
                    "condition": {"type": "string", "enum": ["present", "absent"]},
                    "timeout_seconds": {"type": "number", "minimum": 0.2, "maximum": 10},
                    "language": {"type": "string"},
                },
                "required": ["window", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_visual_target",
            "description": "Find a visible target using confidence-fused OCR, enriched accessibility metadata, role/position/color/shape descriptions, an explicit learned template, or the offline local Florence semantic fallback. Returns boxes without input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "target": {"type": "string", "description": "Visible label, natural semantic description such as settings gear, role/position/color/shape description, or template:name"},
                    "language": {"type": "string"},
                },
                "required": ["window", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_visual_target",
            "description": "Explicitly learn a small password-redacted local image template from a currently identifiable visible target. Use when an icon must later be recognized by appearance. This writes only the selected redacted crop under JARVIS state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "name": {"type": "string"},
                    "source": {"type": "string", "description": "Current visible text or semantic target description used to select the crop"},
                    "occurrence": {"type": "integer", "minimum": 0, "maximum": 50},
                    "language": {"type": "string"},
                },
                "required": ["window", "name", "source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_visual_target",
            "description": "Find and safely click text, a semantic visual target, or template:name. Uses offline Florence only after deterministic methods miss; Florence targets require an explicit expect_* or expected_state postcondition. Performs bounded guarded scrolling, stabilizes moving targets, refuses ambiguity, and verifies results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "target": {"type": "string"},
                    "occurrence": {"type": "integer", "minimum": 0, "maximum": 50},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "language": {"type": "string"},
                    "max_scrolls": {"type": "integer", "minimum": 0, "maximum": 12},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "expect_text": {"type": "string"},
                    "expect_absent_text": {"type": "string"},
                    "expect_window": {"type": "string"},
                    "expected_state": UI_STATE_CONTRACT_SCHEMA,
                    "timeout_seconds": {"type": "number", "minimum": 0.2, "maximum": 5},
                },
                "required": ["window", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_visual_text",
            "description": "Visually locate text, re-observe and re-locate it immediately before clicking, then poll and verify the result with screenshots, OCR, structured UI state, window identity, and optional explicit postconditions. Refuses ambiguous, changed, stale, or occluded targets. Supply expect_* for resulting text/window or expected_state for focus, tab, control, modal, modified, progress, or operation outcomes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "text": {"type": "string"},
                    "occurrence": {"type": "integer", "minimum": 0, "maximum": 50},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "language": {"type": "string"},
                    "expect_text": {"type": "string", "description": "Text that must be visible after the action"},
                    "expect_absent_text": {"type": "string", "description": "Text present before the action that must disappear and remain absent across two observations"},
                    "expect_window": {"type": "string", "description": "Expected foreground window title or process after the action"},
                    "expected_state": UI_STATE_CONTRACT_SCHEMA,
                    "timeout_seconds": {"type": "number", "minimum": 0.2, "maximum": 5},
                },
                "required": ["window", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_diagnostic_snapshot",
            "description": "Explicitly save the latest password-redacted window screenshot under .jarvis/vision for debugging. Normal observations remain memory-only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "language": {"type": "string"},
                },
                "required": ["window"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_known_folders",
            "description": "Resolve the current Windows user's real Home, Desktop, Documents, Downloads, Pictures, Music, and Videos folders without guessing the username.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files or folders by partial name under a Windows known folder or an exact directory. Use this instead of guessing paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "location": {"type": "string", "description": "home, desktop, documents, downloads, pictures, music, videos, or an exact path"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_apps",
            "description": "Discover Windows Start-menu apps dynamically. Use this before launch_app when the spoken app name may be uncertain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_windows",
            "description": "List visible Windows application windows, including foreground, process, PID, and minimized state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_app",
            "description": "Open any uniquely matched installed Windows app and verify that an application window appeared. If uncertain, call list_installed_apps first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_item",
            "description": "Open an existing file/folder, web URL, mail link, or Windows Settings page in its registered app.",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "control_window",
            "description": "Reliably focus, minimize, maximize, restore, close, or snap a visible app window. Close is graceful and never force-kills unsaved work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string", "description": "Unique title/process or exact hwnd:<handle>:pid:<pid> selector from list_windows"},
                    "action": {"type": "string", "enum": ["focus", "minimize", "maximize", "restore", "close", "snap_left", "snap_right"]},
                },
                "required": ["window", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_keys",
            "description": "Send only keyboard shortcuts or named keys to a verified target window, such as ctrl+l, ctrl+s, or enter. Never pass literal words or sentences; use type_text for literal text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                    "window": {"type": "string"},
                    "interval_ms": {"type": "integer", "minimum": 0, "maximum": 1000},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type supplied English/Roman-Urdu literal text into an exact target window, then verify foreground process and focused-control content without exposing the text in the audit log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "window": {"type": "string"},
                    "interval_ms": {"type": "integer", "minimum": 0, "maximum": 250},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_action",
            "description": "Move, click, double-click, or scroll the mouse anywhere on the Windows virtual desktop and verify cursor/foreground state. Prefer semantic interact_ui when possible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["move", "click", "double_click", "scroll"]},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "amount": {"type": "integer", "minimum": -100, "maximum": 100},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": "Control Windows volume and media playback globally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["volume_up", "volume_down", "mute", "play_pause", "next", "previous", "stop"]},
                    "steps": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_ui",
            "description": "Inspect named buttons, fields, menus, tabs, and other controls inside an open Windows app using native UI Automation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": ["window"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_window_text",
            "description": "Read visible document/value text from an open Windows app through UI Automation. Password fields are always skipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 50000},
                },
                "required": ["window"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "interact_ui",
            "description": "Operate a named Windows UI control semantically (button, text field, checkbox, list item, or expandable control) and verify the invoked control.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window": {"type": "string"},
                    "control": {"type": "string", "description": "Visible control name or AutomationId returned by inspect_ui"},
                    "action": {"type": "string", "enum": ["click", "focus", "set_text", "toggle", "select", "expand", "collapse"]},
                    "value": {"type": "string"},
                },
                "required": ["window", "control", "action"],
            },
        },
    },
])
MARK2_TOOL_NAMES = {item["function"]["name"] for item in MARK2_TOOLS}


class Mark2Runtime:
    """Persistent project registry, verified actions, and append-only audit."""

    def __init__(self, agent_home: Path) -> None:
        self.agent_home = agent_home.resolve()
        self.state_dir = self.agent_home / ".jarvis"
        self.registry_path = self.state_dir / "projects.json"
        self.process_path = self.state_dir / "processes.json"
        self.audit_path = self.state_dir / "audit.jsonl"
        self._live_processes: dict[str, subprocess.Popen[str]] = {}
        self._process_uses_shell: dict[str, bool] = {}
        self.windows_control = WindowsControl(self.agent_home)
        self.windows_vision = WindowsVision(self.windows_control, self.state_dir)

    def _ensure_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    def _save_json(self, path: Path, value: Any) -> None:
        self._ensure_state()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _safe_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.agent_home / path
        path = path.resolve()
        try:
            path.relative_to(self.agent_home)
        except ValueError as exc:
            raise ValueError("Project paths must stay inside the agent home folder") from exc
        return path

    def _projects(self) -> dict[str, dict[str, Any]]:
        data = self._load_json(self.registry_path, {"projects": {}})
        projects = data.get("projects") if isinstance(data, dict) else {}
        return projects if isinstance(projects, dict) else {}

    def audit(self, tool: str, args: dict[str, Any], result: str) -> None:
        self._ensure_state()
        safe_args = self._sanitize_for_audit(args)
        if tool == "verify_ui_state" and isinstance(safe_args, dict) and "expected" in safe_args:
            safe_args["expected"] = "[REDACTED: state contract]"
        if tool in {"click_visual_text", "click_visual_target"} and isinstance(safe_args, dict) and "expected_state" in safe_args:
            safe_args["expected_state"] = "[REDACTED: state contract]"
        if tool in {"find_visual_target", "learn_visual_target", "click_visual_target"} and isinstance(safe_args, dict):
            for key in ("target", "source", "name"):
                if key in safe_args:
                    safe_args[key] = "[REDACTED]"
        if tool == "send_keys" and isinstance(safe_args, dict) and "keys" in safe_args:
            # Rejected legacy/model calls may put literal user text in this
            # shortcut field. Never retain that text in the audit trail.
            safe_args["keys"] = "[REDACTED]"
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": safe_args,
            "success": not result.lower().startswith(("error:", "denied")),
            "result": (
                "[REDACTED: visible window text]"
                if tool in {"read_window_text", "observe_screen", "find_visual_text", "find_visual_target", "learn_visual_target", "wait_for_visual_text", "click_visual_text", "click_visual_target", "interact_ui", "inspect_ui_state", "verify_ui_state"}
                else result[:1000]
            ),
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    @classmethod
    def _sanitize_for_audit(cls, value: Any) -> Any:
        if isinstance(value, dict):
            safe = {}
            for key, item in value.items():
                normalized = str(key).casefold()
                if normalized in {"content", "new", "old", "text", "value"} or normalized.endswith("_text") or any(
                    marker in normalized
                    for marker in ("api_key", "apikey", "password", "secret", "token")
                ):
                    safe[str(key)] = "[REDACTED]"
                else:
                    safe[str(key)] = cls._sanitize_for_audit(item)
            return safe
        if isinstance(value, list):
            return [cls._sanitize_for_audit(item) for item in value]
        return value

    def list_projects(self) -> str:
        projects = self._projects()
        if not projects:
            return "No projects registered yet. Use discover_projects, then register_project."
        rows = []
        for name, item in sorted(projects.items()):
            rows.append(
                f"{name}: {item['path']}"
                + (f" | {item.get('description')}" if item.get("description") else "")
            )
        return "\n".join(rows)

    def _get_project(self, name: str) -> dict[str, Any]:
        projects = self._projects()
        exact = projects.get(name)
        if exact:
            return exact
        matches = [value for key, value in projects.items() if key.lower() == name.lower()]
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Unknown project: {name}. Use list_projects first.")

    def discover_projects(self) -> str:
        candidates = []
        for folder in sorted(self.agent_home.iterdir()):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            markers = [
                marker
                for marker in ("package.json", "pyproject.toml", "requirements.txt", ".git")
                if (folder / marker).exists()
            ]
            if markers:
                candidates.append(f"{folder.name}: {folder} | markers={','.join(markers)}")
        return "\n".join(candidates) or "No project candidates found."

    def register_project(
        self,
        name: str,
        path: str,
        start_command: str = "",
        build_command: str = "",
        url: str = "",
        description: str = "",
        open_command: str = "",
    ) -> str:
        clean_name = name.strip()
        if not clean_name or len(clean_name) > 80:
            raise ValueError("Project name must be between 1 and 80 characters")
        project_path = self._safe_path(path)
        if not project_path.is_dir():
            raise ValueError(f"Project directory does not exist: {project_path}")
        if url:
            self._validate_local_url(url)
        projects = self._projects()
        projects[clean_name] = {
            "name": clean_name,
            "path": str(project_path).replace("\\", "/"),
            "start_command": start_command.strip(),
            "build_command": build_command.strip(),
            "url": url.strip(),
            "description": description.strip(),
            "open_command": open_command.strip(),
        }
        self._save_json(self.registry_path, {"version": 1, "projects": projects})
        return f"Registered project {clean_name} at {project_path}"

    def git_status(self, name: str) -> str:
        project = self._get_project(name)
        path = self._safe_path(project["path"])
        completed = subprocess.run(
            ["git", "-C", str(path), "status", "--short", "--branch"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            encoding="utf-8",
            errors="replace",
        )
        output = (completed.stdout or "").strip()
        if completed.returncode:
            return f"error: git status failed ({completed.returncode}): {output}"
        return output or "Working tree clean."

    @staticmethod
    def _validate_local_url(url: str) -> urllib.parse.ParseResult:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http and https URLs are supported")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Health checks are restricted to localhost")
        return parsed

    def check_local_url(self, url: str, timeout: int = 5) -> str:
        self._validate_local_url(url)
        try:
            with urllib.request.urlopen(url, timeout=max(1, min(timeout, 30))) as response:
                status = int(response.status)
                return f"HTTP {status} from {url}"
        except urllib.error.HTTPError as exc:
            return f"error: HTTP {exc.code} from {url}"
        except (urllib.error.URLError, TimeoutError) as exc:
            return f"error: could not reach {url}: {getattr(exc, 'reason', exc)}"

    @staticmethod
    def _child_env() -> dict[str, str]:
        env = dict(os.environ)
        for name in (
            "AI_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "ELEVENLABS_API_KEY",
        ):
            env.pop(name, None)
        return env

    @staticmethod
    def _launch_args(command: str) -> tuple[str | list[str], bool]:
        if os.name != "nt":
            return command, True
        parts = shlex.split(command, posix=False)
        parts = [
            part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
            for part in parts
        ]
        executable = shutil.which(parts[0]) if parts else None
        if executable and Path(executable).suffix.casefold() not in {".bat", ".cmd"}:
            parts[0] = executable
            return parts, False
        return command, True

    def open_project(self, name: str) -> str:
        project = self._get_project(name)
        path = self._safe_path(project["path"])
        command = project.get("open_command") or ""
        if command:
            subprocess.Popen(
                command,
                cwd=path,
                shell=True,
                env=self._child_env(),
            )
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return f"Opened project {name} at {path}"

    def start_project(self, name: str, verify_timeout: int = 20) -> str:
        project = self._get_project(name)
        command = project.get("start_command") or ""
        if not command:
            return f"error: project {name} has no registered start command"
        existing = self._live_processes.get(project["name"])
        if existing and existing.poll() is None:
            return f"Project {name} is already running with PID {existing.pid}"
        path = self._safe_path(project["path"])
        self._ensure_state()
        log_path = self.state_dir / f"{self._slug(project['name'])}-server.log"
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        launch_args, uses_shell = self._launch_args(command)
        with log_path.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(
                launch_args,
                cwd=path,
                shell=uses_shell,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                env=self._child_env(),
                creationflags=creationflags,
            )
        self._live_processes[project["name"]] = process
        self._process_uses_shell[project["name"]] = uses_shell
        process_state = self._load_json(self.process_path, {"processes": {}})
        records = process_state.setdefault("processes", {})
        records[project["name"]] = {
            "pid": process.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "log": str(log_path).replace("\\", "/"),
        }
        self._save_json(
            self.process_path,
            process_state,
        )
        url = project.get("url") or ""
        deadline = time.monotonic() + max(1, min(verify_timeout, 60))
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return f"error: project {name} exited with code {process.returncode}; log={log_path}"
            health = self.check_local_url(url) if url else ""
            if not url or not health.startswith("error:"):
                return f"Started and verified project {name}; PID {process.pid}" + (
                    f"; {health}" if url else ""
                )
            time.sleep(0.5)
        return f"error: project {name} started with PID {process.pid}, but {url} did not become ready"

    def stop_project(self, name: str) -> str:
        project = self._get_project(name)
        process = self._live_processes.get(project["name"])
        if not process or process.poll() is not None:
            return "error: no process started by this JARVIS session is running for that project"
        uses_shell = self._process_uses_shell.get(project["name"], False)
        if os.name == "nt" and uses_shell:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            if completed.returncode:
                return f"error: could not stop PID {process.pid}: {(completed.stdout or '').strip()}"
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return f"error: stop command ran, but PID {process.pid} is still active"
        self._live_processes.pop(project["name"], None)
        self._process_uses_shell.pop(project["name"], None)
        process_state = self._load_json(self.process_path, {"processes": {}})
        process_state.get("processes", {}).pop(project["name"], None)
        self._save_json(self.process_path, process_state)
        return f"Stopped project {name}; verified PID {process.pid} is no longer active"

    def project_status(self, name: str) -> str:
        project = self._get_project(name)
        process = self._live_processes.get(project["name"])
        running = bool(process and process.poll() is None)
        details = [
            f"name={project['name']}",
            f"path={project['path']}",
            f"started_by_this_session={running}",
        ]
        if running and process:
            details.append(f"pid={process.pid}")
        url = project.get("url") or ""
        if url:
            details.append(f"health={self.check_local_url(url)}")
        details.append(f"git={self.git_status(project['name'])}")
        return "\n".join(details)

    def search_project_files(
        self,
        name: str,
        query: str,
        max_results: int = 50,
    ) -> str:
        project = self._get_project(name)
        root = self._safe_path(project["path"])
        needle = query.casefold().strip()
        if not needle:
            raise ValueError("Search query cannot be empty")
        limit = max(1, min(max_results, 200))
        matches: list[str] = []
        for path in root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts) or not path.is_file():
                continue
            if path.is_symlink():
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError:
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            if needle in relative.casefold():
                matches.append(relative)
            elif path.stat().st_size <= 2_000_000:
                try:
                    for line_number, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(),
                        start=1,
                    ):
                        if needle in line.casefold():
                            matches.append(f"{relative}:{line_number}: {line.strip()[:240]}")
                            break
                except (OSError, UnicodeDecodeError):
                    pass
            if len(matches) >= limit:
                break
        return "\n".join(matches) if matches else f"No matches for {query!r} in {name}."

    def run_project_build(self, name: str, timeout: int = 600) -> str:
        project = self._get_project(name)
        command = project.get("build_command") or ""
        if not command:
            return f"error: project {name} has no registered build command"
        path = self._safe_path(project["path"])
        completed = subprocess.run(
            command,
            cwd=path,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(1, min(timeout, 1800)),
            encoding="utf-8",
            errors="replace",
            env=self._child_env(),
        )
        output = (completed.stdout or "")[-12_000:]
        if completed.returncode:
            return f"error: build failed with exit code {completed.returncode}\n{output}"
        return f"Build verified successfully with exit code 0\n{output}"

    def recent_audit(self, limit: int = 20) -> str:
        if not self.audit_path.is_file():
            return "No audited actions yet."
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[-max(1, min(limit, 100)):])

    def execute(self, name: str, args: dict[str, Any]) -> str:
        routes = {
            "list_projects": lambda: self.list_projects(),
            "discover_projects": lambda: self.discover_projects(),
            "register_project": lambda: self.register_project(
                str(args["name"]),
                str(args["path"]),
                str(args.get("start_command") or ""),
                str(args.get("build_command") or ""),
                str(args.get("url") or ""),
                str(args.get("description") or ""),
                str(args.get("open_command") or ""),
            ),
            "project_status": lambda: self.project_status(str(args["name"])),
            "git_status": lambda: self.git_status(str(args["name"])),
            "search_project_files": lambda: self.search_project_files(
                str(args["name"]),
                str(args["query"]),
                int(args.get("max_results") or 50),
            ),
            "check_local_url": lambda: self.check_local_url(
                str(args["url"]),
                int(args.get("timeout") or 5),
            ),
            "open_project": lambda: self.open_project(str(args["name"])),
            "start_project": lambda: self.start_project(
                str(args["name"]),
                int(args.get("verify_timeout") or 20),
            ),
            "stop_project": lambda: self.stop_project(str(args["name"])),
            "run_project_build": lambda: self.run_project_build(
                str(args["name"]),
                int(args.get("timeout") or 600),
            ),
            "recent_audit": lambda: self.recent_audit(int(args.get("limit") or 20)),
            "list_installed_apps": lambda: self.windows_control.list_installed_apps(
                str(args.get("search") or ""), int(args.get("limit") or 50)
            ),
            "list_windows": lambda: self.windows_control.list_windows(),
            "list_known_folders": lambda: self.windows_control.list_known_folders(),
            "find_files": lambda: self.windows_control.find_files(
                str(args["query"]), str(args.get("location") or "home"),
                int(args.get("limit") or 50),
            ),
            "vision_status": lambda: self.windows_vision.status(),
            "ui_state_graph_status": lambda: self.windows_vision.state_graph.status(),
            "recent_ui_transitions": lambda: self.windows_vision.state_graph.recent(
                int(args.get("limit") or 10)
            ),
            "inspect_ui_state": lambda: self.windows_vision.inspect_ui_state(
                str(args.get("window") or ""), str(args.get("language") or "en")
            ),
            "verify_ui_state": lambda: self.windows_vision.verify_ui_state(
                str(args["window"]), dict(args.get("expected") or {}),
                str(args.get("language") or "en"),
            ),
            "observe_screen": lambda: self.windows_vision.observe_screen(
                str(args.get("window") or ""), str(args.get("language") or "en"),
                int(args.get("max_words") or 120),
            ),
            "find_visual_text": lambda: self.windows_vision.find_visual_text(
                str(args["window"]), str(args["text"]), str(args.get("language") or "en")
            ),
            "find_visual_target": lambda: self.windows_vision.find_visual_target(
                str(args["window"]), str(args["target"]), str(args.get("language") or "en")
            ),
            "learn_visual_target": lambda: self.windows_vision.learn_visual_target(
                str(args["window"]), str(args["name"]), str(args["source"]),
                int(args.get("occurrence") or 0), str(args.get("language") or "en"),
            ),
            "wait_for_visual_text": lambda: self.windows_vision.wait_for_visual_text(
                str(args["window"]), str(args["text"]), str(args.get("condition") or "present"),
                float(args.get("timeout_seconds") or 5.0), str(args.get("language") or "en"),
            ),
            "click_visual_text": lambda: self.windows_vision.click_visual_text(
                str(args["window"]), str(args["text"]), int(args.get("occurrence") or 0),
                str(args.get("button") or "left"), str(args.get("language") or "en"),
                expect_text=str(args.get("expect_text") or ""),
                expect_absent_text=str(args.get("expect_absent_text") or ""),
                expect_window=str(args.get("expect_window") or ""),
                timeout_seconds=float(args.get("timeout_seconds") or 2.0),
                expected_state=dict(args.get("expected_state") or {}),
            ),
            "click_visual_target": lambda: self.windows_vision.click_visual_target(
                str(args["window"]), str(args["target"]), int(args.get("occurrence") or 0),
                str(args.get("button") or "left"), str(args.get("language") or "en"),
                max_scrolls=int(args.get("max_scrolls") or 0),
                direction=str(args.get("direction") or "down"),
                expect_text=str(args.get("expect_text") or ""),
                expect_absent_text=str(args.get("expect_absent_text") or ""),
                expect_window=str(args.get("expect_window") or ""),
                timeout_seconds=float(args.get("timeout_seconds") or 2.0),
                expected_state=dict(args.get("expected_state") or {}),
            ),
            "save_diagnostic_snapshot": lambda: self.windows_vision.save_diagnostic_snapshot(
                str(args["window"]), str(args.get("language") or "en")
            ),
            "launch_app": lambda: self.windows_control.launch_app(
                str(args["name"]), int(args.get("wait_seconds") or 8)
            ),
            "open_item": lambda: self.windows_control.open_item(str(args["target"])),
            "control_window": lambda: self.windows_control.control_window(
                str(args["window"]), str(args["action"])
            ),
            "position_window": lambda: self.windows_control.position_window(
                str(args['window']), float(args['x']), float(args['y']),
                float(args['width']), float(args['height']),
            ),
            "send_keys": lambda: self.windows_control.send_keys(
                str(args["keys"]), str(args.get("window") or ""),
                int(args.get("interval_ms") if args.get("interval_ms") is not None else 40),
            ),
            "type_text": lambda: self.windows_control.type_text(
                str(args["text"]), str(args.get("window") or ""),
                int(args.get("interval_ms") if args.get("interval_ms") is not None else 5),
            ),
            "mouse_action": lambda: self.windows_control.mouse_action(
                str(args["action"]),
                int(args["x"]) if args.get("x") is not None else None,
                int(args["y"]) if args.get("y") is not None else None,
                str(args.get("button") or "left"), int(args.get("amount") or 3),
            ),
            "media_control": lambda: self.windows_control.media_control(
                str(args["action"]), int(args.get("steps") or 1)
            ),
            "inspect_ui": lambda: self.windows_control.inspect_ui(
                str(args["window"]), int(args.get("limit") or 80)
            ),
            "read_window_text": lambda: self.windows_control.read_window_text(
                str(args["window"]), int(args.get("max_chars") or 20_000)
            ),
            "interact_ui": lambda: self.windows_control.interact_ui(
                str(args["window"]), str(args["control"]), str(args["action"]),
                str(args.get("value") or ""),
            ),
        }
        if name not in routes:
            return f"error: unknown Mark II tool {name}"
        return routes[name]()

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "project"
