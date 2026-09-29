"""Durable, evidence-gated plans for the existing JARVIS tool loop.

Plans guide *user-requested* multi-step work. They do not schedule background
actions, grant new permissions, or treat a delivered click as goal completion.
Only bounded plan metadata is persisted locally; tool arguments, outputs,
secrets, and the user's raw request are never written to the task file.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SECRET = re.compile(
    r"(?:\b(?:api[_ -]?key|password|secret|token)\b\s*[:=]|"
    r"\bsk-[A-Za-z0-9_-]{12,}\b|-----BEGIN [A-Z ]+PRIVATE KEY-----)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskOrchestrator:
    def __init__(self, agent_home: Path) -> None:
        self.directory = agent_home / ".jarvis" / "tasks"
        self.task: dict[str, Any] | None = None
        self._last_evidence: tuple[str, str] | None = None
        self._load_latest()

    def _load_latest(self) -> None:
        if not self.directory.is_dir():
            return
        files = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[:10]:
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(task, dict) and task.get("id") == path.stem and isinstance(task.get("steps"), list):
                self.task = task
                return

    def _save(self) -> None:
        if self.task is None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{self.task['id']}.json"
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(self.task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def begin_turn(self) -> None:
        """A previous turn's observation cannot verify a new action."""
        self._last_evidence = None

    def is_active(self) -> bool:
        return self.task is not None and self.task.get("status") == "active"

    def create_plan(self, steps: list[dict[str, str]], lanes: tuple[str, ...] = ("general",)) -> str:
        if not isinstance(steps, list) or not 2 <= len(steps) <= 8:
            return "error: plan requires 2 to 8 steps"
        clean_steps: list[dict[str, Any]] = []
        for number, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                return "error: each step must be an object"
            description = " ".join(str(step.get("description") or "").split())
            kind = step.get("kind")
            expected_tool = str(step.get("expected_tool") or "").strip()
            if kind not in {"observe", "act"} or not description or len(description) > 160:
                return "error: each step needs a short description and kind observe or act"
            if kind == "act" and not expected_tool:
                return "error: action steps require expected_tool for evidence binding"
            if expected_tool and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", expected_tool):
                return "error: invalid expected_tool name"
            if _SECRET.search(description):
                return "error: refusing a plan containing a possible secret"
            clean_steps.append({"id": number, "description": description, "kind": kind,
                                "expected_tool": expected_tool, "status": "pending"})
        if self.is_active() and all(
            {key: old.get(key) for key in ("description", "kind", "expected_tool")}
            == {key: new.get(key) for key in ("description", "kind", "expected_tool")}
            for old, new in zip(self.task["steps"], clean_steps)
        ) and len(self.task["steps"]) == len(clean_steps):
            return self.status()
        if self.task is not None and self.task.get("status") == "active":
            self.task["status"] = "paused"
            self.task["updated_at"] = _now()
            self._save()
        self.task = {
            "id": uuid.uuid4().hex,
            "created_at": _now(),
            "updated_at": _now(),
            "status": "active",
            "lanes": sorted({lane for lane in lanes if lane in {"desktop", "research", "project", "general"}}) or ["general"],
            "steps": clean_steps,
            "events": [],
        }
        self._last_evidence = None
        self._save()
        return self.status()

    def record_tool(self, tool: str, status: str) -> None:
        if self.task is None or self.task.get("status") != "active":
            return
        if status not in {"verified", "observed", "delivered", "failed", "denied", "unknown"}:
            status = "unknown"
        self._last_evidence = (tool, status)
        events = self.task.setdefault("events", [])
        events.append({"at": _now(), "tool": tool, "status": status})
        self.task["events"] = events[-80:]
        self.task["updated_at"] = _now()
        self._save()

    def complete_step(self, step_id: int, evidence_tool: str) -> str:
        if self.task is None or self.task.get("status") != "active":
            return "error: no active task plan"
        pending = next((s for s in self.task["steps"] if s["status"] == "pending"), None)
        if pending is None or pending["id"] != step_id:
            return "error: complete the next pending step in order"
        if self._last_evidence is None or self._last_evidence[0] != evidence_tool:
            return "error: no fresh matching tool evidence for this step"
        if pending.get("expected_tool") and pending["expected_tool"] != evidence_tool:
            return "error: evidence tool does not match the planned step"
        status = self._last_evidence[1]
        if pending["kind"] == "act" and status != "verified":
            return f"error: action is {status}, not goal-verified; inspect and verify before completing"
        if pending["kind"] == "observe" and status not in {"observed", "verified"}:
            return f"error: observation is {status}; no reliable evidence yet"
        pending["status"] = "verified" if pending["kind"] == "act" else "observed"
        pending["evidence_tool"] = evidence_tool
        self._last_evidence = None  # One tool result cannot complete multiple steps.
        self.task["updated_at"] = _now()
        if all(step["status"] != "pending" for step in self.task["steps"]):
            self.task["status"] = "completed"
        self._save()
        return self.status()

    def status(self) -> str:
        if self.task is None:
            return "No task plan yet."
        public = {
            "id": self.task["id"],
            "status": self.task["status"],
            "steps": self.task["steps"],
        }
        return json.dumps(public, ensure_ascii=False)

    def cancel(self) -> str:
        if not self.is_active():
            return "error: no active task plan"
        self.task["status"] = "cancelled"
        self.task["updated_at"] = _now()
        self._last_evidence = None
        self._save()
        return self.status()


TASK_TOOLS = [
    {"type": "function", "function": {"name": "create_task_plan", "description": "For a user-requested multi-step task, create 2-8 ordered observe/act steps. Action steps require expected_tool, naming the tool whose verified outcome will prove them. This stores a local plan but performs no actions.", "parameters": {"type": "object", "properties": {"steps": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "object", "properties": {"description": {"type": "string"}, "kind": {"type": "string", "enum": ["observe", "act"]}, "expected_tool": {"type": "string"}}, "required": ["description", "kind"]}}}, "required": ["steps"]}}},
    {"type": "function", "function": {"name": "complete_task_step", "description": "Mark only the next plan step complete using fresh matching tool evidence; actions require verified goal outcome.", "parameters": {"type": "object", "properties": {"step_id": {"type": "integer"}, "evidence_tool": {"type": "string"}}, "required": ["step_id", "evidence_tool"]}}},
    {"type": "function", "function": {"name": "task_status", "description": "Read the most recent durable task plan and step states.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "cancel_task_plan", "description": "Cancel the active plan only when the user explicitly asks to cancel or stop that plan. Does not undo completed actions.", "parameters": {"type": "object", "properties": {}}}},
]
