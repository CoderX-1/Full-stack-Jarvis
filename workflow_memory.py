"""Privacy-minimal learning from completed, evidence-gated task plans.

Patterns contain only lanes and tool/kind sequences. They never include user
requests, arguments, document content, UI text, or secrets. A learned pattern
is a hint for a future plan, never authority to execute or skip verification.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
MAX_PATTERNS = 80
MAX_SUGGESTIONS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowMemory:
    def __init__(self, agent_home: Path) -> None:
        self.path = agent_home / ".jarvis" / "workflow-patterns.json"
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": SCHEMA_VERSION, "patterns": {}, "seen_plan_ids": []}
        try:
            if self.path.stat().st_size > 1_000_000:
                return {"schema_version": SCHEMA_VERSION, "patterns": {}, "seen_plan_ids": []}
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": SCHEMA_VERSION, "patterns": {}, "seen_plan_ids": []}
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return {"schema_version": SCHEMA_VERSION, "patterns": {}, "seen_plan_ids": []}
        if not isinstance(data.get("patterns"), dict) or not isinstance(data.get("seen_plan_ids"), list):
            return {"schema_version": SCHEMA_VERSION, "patterns": {}, "seen_plan_ids": []}
        patterns: dict[str, dict[str, Any]] = {}
        for pattern_id, row in list(data["patterns"].items())[:MAX_PATTERNS]:
            if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{16}", str(pattern_id)):
                continue
            lanes = row.get("lanes")
            steps = row.get("steps")
            if (not isinstance(lanes, list) or not lanes or len(lanes) > 4 or
                any(lane not in {"desktop", "research", "project", "general"} for lane in lanes) or
                not isinstance(steps, list) or not 2 <= len(steps) <= 8):
                continue
            safe_steps = []
            for step in steps:
                if (not isinstance(step, dict) or step.get("kind") not in {"observe", "act"} or
                    not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(step.get("tool") or ""))):
                    break
                safe_steps.append({"kind": step["kind"], "tool": step["tool"]})
            if len(safe_steps) != len(steps):
                continue
            try:
                version = int(row.get("version"))
                successes = int(row.get("successes"))
            except (TypeError, ValueError):
                continue
            if not 1 <= version <= 1_000 or not 0 <= successes <= 1_000_000:
                continue
            status = row.get("status")
            if status not in {"candidate", "active", "disabled"}:
                continue
            patterns[pattern_id] = {
                "id": pattern_id, "version": version, "lanes": lanes,
                "steps": safe_steps, "successes": successes, "status": status,
                "last_verified_at": str(row.get("last_verified_at") or "")[:48],
            }
        seen = [str(value) for value in data["seen_plan_ids"][-500:]
                if re.fullmatch(r"[0-9a-f]{32}", str(value))]
        return {"schema_version": SCHEMA_VERSION, "patterns": patterns, "seen_plan_ids": seen}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def observe_completed(self, task: dict[str, Any] | None, lanes: tuple[str, ...]) -> str:
        if not isinstance(task, dict) or task.get("status") != "completed":
            return "ignored: task is not completed"
        plan_id = str(task.get("id") or "")
        if not plan_id or plan_id in self.data["seen_plan_ids"]:
            return "ignored: plan already counted or missing ID"
        steps = task.get("steps")
        if not isinstance(steps, list) or not 2 <= len(steps) <= 8:
            return "ignored: invalid plan shape"
        pattern_steps: list[dict[str, str]] = []
        for step in steps:
            if not isinstance(step, dict):
                return "ignored: invalid step"
            kind = step.get("kind")
            tool = str(step.get("expected_tool") or "")
            status = step.get("status")
            if kind not in {"observe", "act"} or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool):
                return "ignored: step has no bounded tool identity"
            if step.get("evidence_tool") != tool:
                return "ignored: completed step evidence does not match its planned tool"
            if (kind == "act" and status != "verified") or (kind == "observe" and status not in {"observed", "verified"}):
                return "ignored: plan lacks goal-verified evidence"
            pattern_steps.append({"kind": kind, "tool": tool})
        safe_lanes = sorted({lane for lane in lanes if lane in {"desktop", "research", "project", "general"}})
        if not safe_lanes:
            safe_lanes = ["general"]
        shape = {"lanes": safe_lanes, "steps": pattern_steps}
        pattern_id = hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        patterns = self.data["patterns"]
        existing = patterns.get(pattern_id)
        if existing is None:
            if len(patterns) >= MAX_PATTERNS:
                return "ignored: workflow pattern limit reached"
            existing = {"id": pattern_id, "version": 1, **shape, "successes": 0,
                        "status": "candidate", "last_verified_at": ""}
            patterns[pattern_id] = existing
        if existing.get("status") == "disabled":
            return "ignored: workflow pattern was disabled"
        existing["successes"] = min(1_000_000, int(existing.get("successes") or 0) + 1)
        existing["last_verified_at"] = _now()
        if existing["successes"] >= 2 and existing["status"] == "candidate":
            existing["status"] = "active"
            existing["version"] = min(1_000, int(existing["version"]) + 1)
        self.data["seen_plan_ids"] = (self.data["seen_plan_ids"] + [plan_id])[-500:]
        self._save()
        return f"{existing['status']}: pattern {pattern_id}; verified completions={existing['successes']}"

    def suggestions(self, lanes: tuple[str, ...]) -> list[dict[str, Any]]:
        wanted = set(lanes)
        active = [row for row in self.data["patterns"].values()
                  if isinstance(row, dict) and row.get("status") == "active"
                  and wanted.intersection(row.get("lanes") or [])]
        active.sort(key=lambda row: (int(row.get("successes") or 0), row.get("last_verified_at") or ""), reverse=True)
        return [{"id": row["id"], "version": row["version"], "successes": row["successes"],
                 "steps": row["steps"]} for row in active[:MAX_SUGGESTIONS]]

    def status(self) -> str:
        rows = [row for row in self.data["patterns"].values() if isinstance(row, dict)]
        return json.dumps({
            "schema_version": SCHEMA_VERSION,
            "candidates": sum(row.get("status") == "candidate" for row in rows),
            "active": sum(row.get("status") == "active" for row in rows),
            "disabled": sum(row.get("status") == "disabled" for row in rows),
            "note": "Patterns are untrusted hints, never replay commands or permission grants.",
        })

    def disable(self, pattern_id: str) -> str:
        row = self.data["patterns"].get(str(pattern_id))
        if not isinstance(row, dict):
            return "error: learned workflow pattern not found"
        if row.get("status") == "disabled":
            return "already disabled"
        row["status"] = "disabled"
        row["version"] = int(row.get("version") or 1) + 1
        self._save()
        return f"disabled workflow pattern {pattern_id}; version={row['version']}"


WORKFLOW_TOOLS = [
    {"type": "function", "function": {"name": "workflow_status", "description": "Read counts of privacy-minimal learned workflow patterns. A pattern is a hint, never an executable macro.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "recall_workflows", "description": "Show at most three previously goal-verified tool-sequence hints for this task lane. Reobserve and plan afresh; never blindly replay.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "disable_workflow", "description": "Disable one learned workflow pattern only when the user explicitly requests forgetting or disabling that pattern. Does not undo past actions.", "parameters": {"type": "object", "properties": {"pattern_id": {"type": "string"}}, "required": ["pattern_id"]}}},
]
