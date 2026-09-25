"""Turn-scoped, privacy-safe duplicate mutation suppression for JARVIS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from typing import Any


_OPERATIONAL_KEYS = {
    "interval_ms", "timeout", "timeout_seconds", "verify_timeout",
    "wait_seconds", "language", "max_scrolls", "direction",
    # Postconditions describe how success is proven, not which side effect is
    # delivered. Changing one must not bypass replay suppression.
    "expect_text", "expect_absent_text", "expect_window", "expected_state",
}
_IDENTITY_KEYS = {
    "action", "anchor", "button", "condition", "control", "direction",
    "name", "role", "source", "subject", "target", "window",
}


@dataclass(frozen=True)
class DuplicateDecision:
    blocked: bool
    code: str
    fingerprint: str
    prior_status: str = ""
    prior_age_ms: int | None = None


class DuplicateActionGuard:
    """Block blind semantic replays while permitting a new user request.

    Only SHA-256 fingerprints and bounded outcome codes are persisted. Raw
    arguments, labels, window titles, typed text, and user utterances are not.
    """

    def __init__(self, state_dir: Path, *, max_records: int = 64) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "duplicate-actions.json"
        self.max_records = max(8, min(int(max_records), 256))
        self._lock = threading.RLock()
        self._scope_id = uuid.uuid4().hex[:16]
        self._last_decision: dict[str, Any] | None = None
        self._state = self._load()
        self._prune_locked()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "recent": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "recent": []}
        if not isinstance(value, dict) or not isinstance(value.get("recent", []), list):
            return {"schema_version": 1, "recent": []}
        return {"schema_version": 1, "recent": value.get("recent", [])}

    def _persist_locked(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state["recent"] = list(self._state.get("recent") or [])[-self.max_records:]
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _prune_locked(self) -> None:
        cutoff = time.time() - 3600.0
        clean = []
        for item in list(self._state.get("recent") or []):
            if not isinstance(item, dict):
                continue
            try:
                recorded_epoch = float(item.get("recorded_epoch") or 0)
            except (TypeError, ValueError):
                continue
            if recorded_epoch >= cutoff:
                clean.append({
                    "fingerprint": str(item.get("fingerprint") or "")[:64],
                    "tool": str(item.get("tool") or "unknown")[:64],
                    "scope": str(item.get("scope") or "")[:32],
                    "status": str(item.get("status") or "unknown")[:32],
                    "may_have_delivered": bool(item.get("may_have_delivered")),
                    "recorded_at": str(item.get("recorded_at") or "unknown")[:48],
                    "recorded_epoch": recorded_epoch,
                })
        self._state["recent"] = clean[-self.max_records:]

    @classmethod
    def _normalize(cls, value: Any, *, tool: str, key: str = "") -> Any:
        if isinstance(value, dict):
            return {
                str(item_key): cls._normalize(item, tool=tool, key=str(item_key))
                for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if str(item_key) not in _OPERATIONAL_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalize(item, tool=tool, key=key) for item in value]
        if isinstance(value, str):
            compact = " ".join(value.strip().split())
            # Literal typing is case-sensitive. Visual target text and stable
            # identifiers are not, so casing variations should still dedupe.
            if key in _IDENTITY_KEYS or (key == "text" and tool != "type_text"):
                return compact.casefold()
            return compact
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)

    @classmethod
    def fingerprint(cls, tool: str, args: dict[str, Any]) -> str:
        tool_name = str(tool)
        semantic_args: dict[str, Any] = dict(args)
        # Text- and descriptor-grounded clicks are alternate selector routes to
        # the same mutation. Canonicalize them so switching modality cannot
        # turn an uncertain click into a second blind click.
        if tool_name in {"click_visual_text", "click_visual_target"}:
            tool_name = "visual_click"
            semantic_args = {
                "window": args.get("window"),
                "subject": args.get("text") if args.get("text") is not None else args.get("target"),
                "occurrence": args.get("occurrence", 0),
                "button": args.get("button", "left"),
            }
        elif tool_name == "interact_ui" and str(args.get("action") or "").casefold() in {"click", "invoke"}:
            tool_name = "visual_click"
            semantic_args = {
                "window": args.get("window"),
                "subject": args.get("control"),
                "occurrence": 0,
                "button": "left",
            }
        canonical = {
            "tool": tool_name,
            "args": cls._normalize(semantic_args, tool=tool_name),
        }
        encoded = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def begin_scope(self) -> str:
        """Start one user-request scope; deliberate later requests may repeat."""
        with self._lock:
            self._scope_id = uuid.uuid4().hex[:16]
            self._last_decision = None
            return self._scope_id

    def preflight(self, tool: str, args: dict[str, Any]) -> DuplicateDecision:
        fingerprint = self.fingerprint(tool, args)
        now = time.time()
        with self._lock:
            self._prune_locked()
            prior = next(
                (
                    item for item in reversed(self._state.get("recent") or [])
                    if item.get("fingerprint") == fingerprint
                    and (
                        item.get("may_have_delivered")
                        or item.get("status") in {
                            "unknown", "timed-out-contained", "recovered-interrupted",
                        }
                    )
                ),
                None,
            )
            blocked = False
            code = "unique-in-request"
            prior_status = ""
            prior_age_ms: int | None = None
            if prior:
                prior_status = str(prior.get("status") or "unknown")
                prior_age_ms = max(0, round((now - float(prior.get("recorded_epoch") or now)) * 1000))
                if prior.get("scope") == self._scope_id and prior.get("may_have_delivered"):
                    blocked = True
                    code = "same-request-replay-blocked"
                elif prior_status in {"unknown", "timed-out-contained", "recovered-interrupted"} and prior_age_ms < 60_000:
                    blocked = True
                    code = "recent-uncertain-replay-blocked"
                else:
                    code = "new-request-repeat-allowed"
            decision = {
                "tool": str(tool),
                "blocked": blocked,
                "code": code,
                "fingerprint_prefix": fingerprint[:12],
                "prior_status": prior_status,
                "prior_age_ms": prior_age_ms,
            }
            self._last_decision = decision
            if blocked:
                self._state.setdefault("recent", []).append({
                    "fingerprint": fingerprint,
                    "tool": str(tool)[:64],
                    "scope": self._scope_id,
                    "status": "duplicate-blocked",
                    "may_have_delivered": False,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "recorded_epoch": now,
                })
                self._prune_locked()
                self._persist_locked()
            return DuplicateDecision(blocked, code, fingerprint, prior_status, prior_age_ms)

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        status: str,
        may_have_delivered: bool,
    ) -> None:
        now = time.time()
        fingerprint = self.fingerprint(tool, args)
        with self._lock:
            record = {
                "fingerprint": fingerprint,
                "tool": str(tool)[:64],
                "scope": self._scope_id,
                "status": str(status)[:32],
                "may_have_delivered": bool(may_have_delivered),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "recorded_epoch": now,
            }
            self._state.setdefault("recent", []).append(record)
            self._prune_locked()
            self._persist_locked()
            self._last_decision = {
                "tool": str(tool),
                "blocked": False,
                "code": "action-recorded",
                "fingerprint_prefix": fingerprint[:12],
                "status": str(status)[:32],
                "may_have_delivered": bool(may_have_delivered),
            }

    def take_last(self, tool: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            value = dict(self._last_decision) if isinstance(self._last_decision, dict) else None
            if value is not None and tool is not None and value.get("tool") != tool:
                return None
            self._last_decision = None
            return value

    def status(self) -> str:
        with self._lock:
            self._prune_locked()
            recent = list(self._state.get("recent") or [])
            blocked = sum(item.get("status") == "duplicate-blocked" for item in recent)
            return (
                "Duplicate Action Guard component 3 ready | scope=user-request | "
                "fingerprints=sha256-canonical | same_request_replay=blocked | "
                "new_request_repeat=allowed | uncertain_restart_grace=60s | "
                f"recent={len(recent)}/{self.max_records} | blocked_records={blocked} | "
                "arguments_persisted=false | blind_replay=false"
            )
