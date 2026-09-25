"""Bounded action leases and stuck-operation containment for JARVIS Mark II."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable


class WatchdogBusyError(RuntimeError):
    """Raised when a second mutation is attempted while one is still active."""


@dataclass
class ActionLease:
    action_id: str
    tool: str
    deadline_seconds: float
    started_monotonic: float
    started_at: str
    timer: threading.Timer | None = None
    timed_out: bool = False
    containment_ok: bool | None = None
    finished: bool = False


DEFAULT_DEADLINES: dict[str, float] = {
    "register_project": 15.0,
    "open_project": 20.0,
    "start_project": 70.0,
    "stop_project": 25.0,
    "run_project_build": 1805.0,
    "position_window": 15.0,
    "learn_visual_target": 45.0,
    "click_visual_text": 60.0,
    "click_visual_target": 90.0,
    "save_diagnostic_snapshot": 30.0,
    "launch_app": 25.0,
    "open_item": 20.0,
    "control_window": 20.0,
    "send_keys": 30.0,
    "type_text": 180.0,
    "mouse_action": 10.0,
    "media_control": 10.0,
    "interact_ui": 30.0,
    "browser_navigate": 50.0,
    "browser_interact": 50.0,
    "play_youtube": 75.0,
}


class ActionWatchdog:
    """Own one mutating action lease and contain it when its deadline expires.

    Python cannot safely kill an arbitrary worker thread. The watchdog instead
    sets the shared abort signal, releases agent-held input, quarantines new
    mutations, persists the stuck state, and discards any late success claim.
    Cooperative loops then stop at their next bounded checkpoint.
    """

    def __init__(
        self,
        state_dir: Path,
        contain: Callable[[], Any],
        deadlines: dict[str, float] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "watchdog.json"
        self._contain = contain
        self._deadlines = dict(DEFAULT_DEADLINES)
        if deadlines:
            self._deadlines.update({str(k): float(v) for k, v in deadlines.items()})
        self._lock = threading.RLock()
        self._active: ActionLease | None = None
        self._last_record: dict[str, Any] | None = None
        self._state = self._load_state()
        self._recover_interrupted_action()

    def _load_state(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "current": None, "recent": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "current": None, "recent": []}
        if not isinstance(value, dict):
            return {"schema_version": 1, "current": None, "recent": []}
        value.setdefault("schema_version", 1)
        value.setdefault("current", None)
        value.setdefault("recent", [])
        return value

    def _persist_locked(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state["recent"] = list(self._state.get("recent") or [])[-32:]
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _recover_interrupted_action(self) -> None:
        with self._lock:
            stale = self._state.get("current")
            if not isinstance(stale, dict):
                return
            record = {
                "action_id": str(stale.get("action_id") or "unknown"),
                "tool": str(stale.get("tool") or "unknown"),
                "status": "recovered-interrupted",
                "started_at": str(stale.get("started_at") or "unknown"),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "deadline_seconds": float(stale.get("deadline_seconds") or 0),
                "elapsed_ms": None,
                "containment_ok": None,
            }
            self._state.setdefault("recent", []).append(record)
            self._state["current"] = None
            self._persist_locked()

    def deadline_for(self, tool: str, args: dict[str, Any]) -> float:
        if tool not in self._deadlines:
            raise ValueError(f"No watchdog deadline registered for mutating tool {tool}")
        deadline = float(self._deadlines[tool])
        if tool == "start_project":
            deadline = min(70.0, max(6.0, float(args.get("verify_timeout") or 20) + 5.0))
        elif tool == "run_project_build":
            deadline = min(1805.0, max(6.0, float(args.get("timeout") or 600) + 5.0))
        elif tool == "launch_app":
            deadline = min(25.0, max(6.0, float(args.get("wait_seconds") or 8) + 5.0))
        elif tool in {"click_visual_text", "click_visual_target"}:
            verify = min(5.0, max(0.2, float(args.get("timeout_seconds") or 2.0)))
            scrolls = min(12, max(0, int(args.get("max_scrolls") or 0)))
            deadline = min(deadline, max(15.0, verify + 8.0 + scrolls * 4.0))
        elif tool == "send_keys":
            chords = min(20, max(1, len([p for p in str(args.get("keys") or "").split(",") if p.strip()])))
            interval = min(1000, max(0, int(args.get("interval_ms") or 40))) / 1000.0
            deadline = min(30.0, max(8.0, 5.0 + chords * (interval + 0.3)))
        elif tool == "type_text":
            length = min(10_000, len(str(args.get("text") or "")))
            interval = min(250, max(0, int(args.get("interval_ms") or 5))) / 1000.0
            deadline = min(180.0, max(15.0, 12.0 + length * interval))
        return round(max(0.05, deadline), 3)

    def begin(self, tool: str, args: dict[str, Any]) -> ActionLease:
        deadline = self.deadline_for(tool, args)
        with self._lock:
            if self._active and not self._active.finished:
                raise WatchdogBusyError(
                    f"action {self._active.tool} is still active; new mutation refused"
                )
            lease = ActionLease(
                action_id=uuid.uuid4().hex[:16],
                tool=tool,
                deadline_seconds=deadline,
                started_monotonic=time.monotonic(),
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            self._active = lease
            self._last_record = None
            self._state["current"] = {
                "action_id": lease.action_id,
                "tool": tool,
                "status": "active",
                "started_at": lease.started_at,
                "deadline_seconds": deadline,
            }
            self._persist_locked()
            lease.timer = threading.Timer(deadline, self._trip, args=(lease,))
            lease.timer.daemon = True
            lease.timer.start()
            return lease

    def _trip(self, lease: ActionLease) -> None:
        with self._lock:
            if self._active is not lease or lease.finished:
                return
            lease.timed_out = True
            current = dict(self._state.get("current") or {})
            current["status"] = "stuck-contained"
            current["contained_at"] = datetime.now(timezone.utc).isoformat()
            self._state["current"] = current
            self._persist_locked()
        try:
            self._contain()
        except Exception:
            lease.containment_ok = False
        else:
            lease.containment_ok = True
        with self._lock:
            current = dict(self._state.get("current") or {})
            if current.get("action_id") == lease.action_id:
                current["containment_ok"] = lease.containment_ok
                self._state["current"] = current
                self._persist_locked()

    def finish(self, lease: ActionLease, outcome: str) -> dict[str, Any]:
        if lease.timer:
            lease.timer.cancel()
        with self._lock:
            if lease.finished:
                return dict(self._last_record or {})
            lease.finished = True
            elapsed_ms = max(0, round((time.monotonic() - lease.started_monotonic) * 1000))
            status = "timed-out-contained" if lease.timed_out else str(outcome)
            record = {
                "action_id": lease.action_id,
                "tool": lease.tool,
                "status": status,
                "started_at": lease.started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "deadline_seconds": lease.deadline_seconds,
                "elapsed_ms": elapsed_ms,
                "containment_ok": lease.containment_ok,
            }
            self._state.setdefault("recent", []).append(record)
            self._state["current"] = None
            self._persist_locked()
            if self._active is lease:
                self._active = None
            self._last_record = record
            return dict(record)

    def clear_last(self) -> None:
        with self._lock:
            self._last_record = None

    def last_record(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_record) if isinstance(self._last_record, dict) else None

    def take_last(self, tool: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            value = dict(self._last_record) if isinstance(self._last_record, dict) else None
            if value is not None and tool is not None and value.get("tool") != tool:
                return None
            self._last_record = None
            return value

    def status(self) -> str:
        with self._lock:
            recent = list(self._state.get("recent") or [])
            timeouts = sum(item.get("status") == "timed-out-contained" for item in recent)
            recovered = sum(item.get("status") == "recovered-interrupted" for item in recent)
            active = self._active.tool if self._active and not self._active.finished else "none"
            return (
                "Action Watchdog component 2 ready | "
                f"deadline_policies={len(self._deadlines)} | active={active} | "
                f"recent={len(recent)}/32 | timeouts_contained={timeouts} | "
                f"interrupted_recovered={recovered} | overlapping_mutations=refused | "
                "late_success=discarded | persistence=atomic-bounded-no-arguments"
            )
