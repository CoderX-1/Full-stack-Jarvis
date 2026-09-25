"""Modal-blocker and human-input arbitration for JARVIS desktop actions."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Any


DESKTOP_MUTATIONS = {
    "open_project", "position_window", "click_visual_text", "click_visual_target",
    "launch_app", "open_item", "control_window", "send_keys", "type_text",
    "mouse_action", "media_control", "interact_ui",
}

MODAL_AWARE_MUTATIONS = {
    "position_window", "click_visual_text", "click_visual_target", "control_window",
    "send_keys", "type_text", "mouse_action", "interact_ui",
}


class InteractionGuard:
    """Refuse desktop input when the user is active or a modal blocks it."""

    def __init__(self, state_dir: Path, control: Any, *, quiet_ms: int = 450) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "interaction-guard.json"
        self.control = control
        self.quiet_ms = max(100, min(int(quiet_ms), 2000))
        self._lock = threading.RLock()
        self._last: dict[str, Any] | None = None
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "recent": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "recent": []}
        recent = value.get("recent", []) if isinstance(value, dict) else []
        return {"schema_version": 1, "recent": recent if isinstance(recent, list) else []}

    def _record(self, tool: str, status: str, *, sources: list[str] | None = None) -> None:
        entry = {
            "tool": str(tool)[:64],
            "status": str(status)[:48],
            "sources": [str(item)[:32] for item in (sources or [])[:4]],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            recent = list(self._state.get("recent") or [])
            recent.append(entry)
            self._state["recent"] = recent[-64:]
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
            self._last = dict(entry)

    def preflight(self, tool: str, args: dict[str, Any]) -> tuple[bool, str]:
        if tool not in DESKTOP_MUTATIONS:
            return True, "not-desktop-input"
        if bool(self.control.recent_physical_input(self.quiet_ms)):
            self._record(tool, "blocked-user-active", sources=["last-input"])
            return False, "user physical input is active; desktop mutation paused"
        if tool in MODAL_AWARE_MUTATIONS:
            selector = str(args.get("window") or "")
            try:
                snapshot = self.control.modal_blocker_snapshot(selector)
            except (RuntimeError, ValueError):
                # Target resolution and capability errors remain the action
                # adapter's responsibility; never invent a modal from failure.
                snapshot = {"blocked": False, "sources": []}
            if snapshot.get("blocked"):
                sources = [str(item) for item in snapshot.get("sources") or []]
                self._record(tool, "blocked-modal", sources=sources)
                selector = str(snapshot.get("dialog_selector") or "")
                route = f"; address the dialog using exact selector {selector}" if selector else ""
                return False, (
                    "target is blocked by a modal window; inspect and address the dialog first"
                    f"{route}"
                )
        self._last = {
            "tool": str(tool), "status": "allowed", "sources": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return True, "allowed"

    def finish(self, tool: str) -> None:
        if tool not in DESKTOP_MUTATIONS:
            return
        interrupted = bool(self.control.take_user_input_interruption())
        self._record(
            tool,
            "stopped-user-input" if interrupted else "completed-with-arbitration",
            sources=["mid-action-input"] if interrupted else [],
        )

    def take_last(self, tool: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            value = dict(self._last) if isinstance(self._last, dict) else None
            if value is not None and tool is not None and value.get("tool") != tool:
                return None
            self._last = None
            return value

    def status(self) -> str:
        with self._lock:
            recent = list(self._state.get("recent") or [])
        modal = sum(item.get("status") == "blocked-modal" for item in recent)
        user = sum(item.get("status") in {"blocked-user-active", "stopped-user-input"} for item in recent)
        return (
            "Interaction Guard component 4 ready | modal_preflight=fresh-native+uia | "
            f"physical_quiet_ms={self.quiet_ms} | mid_action_user_input=cooperative-stop | "
            "modal_dialog_target=allowed | parent_behind_modal=blocked | "
            f"recent={len(recent)}/64 | modal_blocks={modal} | user_arbitrations={user} | "
            "arguments_persisted=false"
        )
