"""Structured, privacy-preserving UI state and transition memory for JARVIS.

Live inspection may return labels already visible to the selected brain. Durable
storage contains only hashes and coarse state: never OCR, titles, labels, values,
or screenshots. Historical state is evidence, never permission to replay.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
CONTRACT_KEYS = {"window", "operation", "modified", "modal", "active_tab", "focused", "control", "progress_at_least"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any, length: int = 20) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8", errors="replace")).hexdigest()[:length]


def _role(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("role") or "unknown").replace("ControlType.", "").casefold()[:40]


def _words(value: Any) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold(), flags=re.UNICODE))


def _matches(actual: Any, expected: Any) -> bool:
    wanted, found = _words(expected), _words(actual)
    return bool(wanted) and (wanted == found or wanted in found)


class UIStateGraph:
    """Bounded, atomic world-state memory plus live contract evaluation."""

    def __init__(self, path: Path, *, max_nodes: int = 500, max_edges: int = 1500) -> None:
        self.path = path
        self.max_nodes = max(20, int(max_nodes))
        self.max_edges = max(50, int(max_edges))
        self._lock = threading.RLock()
        self._load_error = ""
        self._save_error = ""
        self._migrated_from = 0
        self._data = self._load()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "nodes": {}, "edges": {}, "history": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            version = loaded.get("schema_version") if isinstance(loaded, dict) else None
            if version not in {1, SCHEMA_VERSION}:
                raise ValueError("unsupported schema")
            if not all(isinstance(loaded.get(key), expected) for key, expected in (("nodes", dict), ("edges", dict), ("history", list))):
                raise ValueError("invalid graph structure")
            if version == 1:
                self._migrated_from = 1
                loaded["schema_version"] = SCHEMA_VERSION
                for node in loaded["nodes"].values():
                    if isinstance(node, dict):
                        node.setdefault("state", {})
            return loaded
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self._load_error = f"{type(exc).__name__}: {exc}"
            return self._empty()

    def _save(self) -> None:
        if self._load_error:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(self._data, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
            temporary.replace(self.path)
            self._save_error = ""
        except OSError as exc:
            self._save_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _visual_fingerprint(observation: Any) -> str:
        image = getattr(observation, "image", None)
        if image is None:
            return str(getattr(observation, "image_hash", ""))[:20]
        try:
            grayscale = image.convert("L").resize((17, 16))
            pixels = list(grayscale.get_flattened_data() if hasattr(grayscale, "get_flattened_data") else grayscale.getdata())
            value = 0
            for row in range(16):
                for column in range(16):
                    value = (value << 1) | int(pixels[row * 17 + column] > pixels[row * 17 + column + 1])
            rgb = image.convert("RGB").resize((8, 8))
            rgb_pixels = list(rgb.get_flattened_data() if hasattr(rgb, "get_flattened_data") else rgb.getdata())
            means = tuple(sum(pixel[channel] for pixel in rgb_pixels) // max(1, len(rgb_pixels)) for channel in range(3))
            return "".join(f"{channel // 8:02x}" for channel in means) + f"{value:064x}"
        except Exception:
            return str(getattr(observation, "image_hash", ""))[:20]

    @staticmethod
    def _element_key(item: dict[str, Any]) -> str:
        runtime = item.get("runtime_id")
        if runtime:
            return _digest(["runtime", str(runtime)], 16)
        return _digest({
            "name": _words(item.get("name")), "id": _words(item.get("id")), "role": _role(item),
            "box": [int(item.get(key) or 0) for key in ("x", "y", "width", "height")],
        }, 16)

    @classmethod
    def _semantic_fingerprint(cls, elements: list[dict[str, Any]] | None) -> str:
        safe = [{
            "key": cls._element_key(item), "role": _role(item), "enabled": bool(item.get("enabled", True)),
            "focused": bool(item.get("focused")), "selected": item.get("selected"),
            "toggle": item.get("toggle_state"), "expand": item.get("expand_state"),
            "box": [round(int(item.get(key) or 0) / 8) for key in ("x", "y", "width", "height")],
        } for item in (elements or [])]
        return _digest(sorted(safe, key=lambda item: json.dumps(item, sort_keys=True)))

    @classmethod
    def live_state(cls, observation: Any, elements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Build a structured current state. The returned labels are not persisted."""
        controls: list[dict[str, Any]] = []
        for raw in (elements or []):
            if not isinstance(raw, dict):
                continue
            parent_runtime = raw.get("parent_runtime_id")
            parent_key = _digest(["runtime", str(parent_runtime)], 16) if parent_runtime else str(raw.get("parent_key") or "")
            controls.append({
                "key": cls._element_key(raw), "parent_key": parent_key,
                "name": str(raw.get("name") or "")[:300], "automation_id": str(raw.get("id") or "")[:160],
                "role": _role(raw), "enabled": bool(raw.get("enabled", True)),
                "focusable": bool(raw.get("focusable")), "focused": bool(raw.get("focused")),
                "actionable": bool(raw.get("actionable")),
                "selected": raw.get("selected") if isinstance(raw.get("selected"), bool) else None,
                "toggle_state": str(raw.get("toggle_state") or "") or None,
                "expand_state": str(raw.get("expand_state") or "") or None,
                "value_present": bool(raw.get("value_present")),
                "range_value": raw.get("range_value") if isinstance(raw.get("range_value"), (int, float)) else None,
                "range_min": raw.get("range_min") if isinstance(raw.get("range_min"), (int, float)) else None,
                "range_max": raw.get("range_max") if isinstance(raw.get("range_max"), (int, float)) else None,
                "is_modal": bool(raw.get("is_modal")), "interaction_state": str(raw.get("interaction_state") or "") or None,
                "box": [int(raw.get(key) or 0) for key in ("x", "y", "width", "height")], "children": [],
            })
        by_key = {item["key"]: item for item in controls}
        parent_keys: dict[str, str] = {}
        roots: list[str] = []
        for item in controls:
            parent = item.pop("parent_key")
            parent_keys[item["key"]] = parent
            if parent and parent in by_key and parent != item["key"]:
                by_key[parent]["children"].append(item["key"])
            else:
                roots.append(item["key"])

        focused = next((item for item in controls if item["focused"]), None)
        selected = [item for item in controls if item["selected"] is True]
        tabs = [item for item in selected if item["role"] == "tabitem"]
        modals = [item for item in controls if item["is_modal"]]
        progress_controls = [item for item in controls if item["role"] == "progressbar"]
        busy_words = {"running", "notresponding", "blockedbymodalwindow", "closing"}
        busy = [item for item in controls if _words(item["interaction_state"]).replace(" ", "") in busy_words]
        # Treat status-shaped labels as errors, not arbitrary content that
        # merely discusses an error (commit history, webpages, chat, files).
        # Broad keyword matching made an otherwise-idle VS Code window report
        # operation=error when commit messages contained "error handling".
        def has_error_context(item: dict[str, Any]) -> bool:
            current = item
            for _depth in range(12):
                if current["role"] in {"alert", "dialog", "status", "statusbar"}:
                    return True
                parent = parent_keys.get(current["key"], "")
                if not parent or parent not in by_key:
                    return False
                current = by_key[parent]
            return False

        errors = []
        for item in controls:
            name = _words(item["name"])
            automation_id = _words(item["automation_id"])
            failure_word = any(
                word in name.split() for word in ("error", "errors", "failed", "failure")
            )
            id_marks_error = any(
                word in automation_id.split()
                for word in ("error", "errors", "failed", "failure")
            )
            if (failure_word and has_error_context(item)) or id_marks_error:
                errors.append(item)
        title = str(getattr(observation, "title", ""))
        modified = title.rstrip().endswith("*") or any(marker in _words(title).split() for marker in ("unsaved", "modified"))
        progress: list[float] = []
        for item in progress_controls:
            value, minimum, maximum = item["range_value"], item["range_min"], item["range_max"]
            if all(isinstance(number, (int, float)) for number in (value, minimum, maximum)) and maximum > minimum:
                progress.append(round((float(value) - float(minimum)) * 100 / (float(maximum) - float(minimum)), 2))
        if errors:
            operation = "error"
        elif modals and any(_words(item["interaction_state"]).replace(" ", "") == "blockedbymodalwindow" for item in controls):
            operation = "blocked"
        elif busy or any(value < 100 for value in progress):
            operation = "busy"
        else:
            operation = "idle"
        return {
            "window": {"handle": int(getattr(observation, "handle", 0)), "pid": int(getattr(observation, "pid", 0)),
                       "process": str(getattr(observation, "process", "")), "title": title,
                       "size": [int(getattr(observation, "width", 0)), int(getattr(observation, "height", 0))]},
            "operation": operation, "modified": bool(modified), "focused": focused,
            "active_tabs": tabs, "selected": selected, "modals": modals, "progress_percent": progress,
            "errors": errors, "roots": roots, "controls": controls[:500],
            "summary": {"controls": len(controls), "actionable": sum(bool(item["actionable"]) for item in controls),
                        "disabled": sum(not bool(item["enabled"]) for item in controls), "selected": len(selected),
                        "modals": len(modals), "errors": len(errors)},
        }

    @classmethod
    def _safe_state(cls, live: dict[str, Any]) -> dict[str, Any]:
        focused = live.get("focused") or {}
        controls = live.get("controls") or []
        return {
            "operation": live.get("operation", "unknown"), "modified": bool(live.get("modified")),
            "control_count": len(controls), "actionable_count": int(live["summary"].get("actionable") or 0),
            "disabled_count": int(live["summary"].get("disabled") or 0), "focused_key": focused.get("key", ""),
            "focused_role": focused.get("role", ""), "active_tab_keys": [item.get("key", "") for item in live.get("active_tabs", [])[:20]],
            "modal_count": len(live.get("modals") or []), "error_count": len(live.get("errors") or []),
            "progress_buckets": [min(10, max(0, round(float(value) / 10))) for value in live.get("progress_percent", [])[:20]],
            "structure_hash": _digest([{"key": item.get("key"), "role": item.get("role"), "children": item.get("children", []),
                                       "enabled": item.get("enabled"), "selected": item.get("selected")} for item in controls]),
        }

    def fingerprint(self, observation: Any, elements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        semantic_hash = self._semantic_fingerprint(elements)
        identity = {
            "process": str(getattr(observation, "process", "")).casefold()[:120],
            "size": [int(getattr(observation, "width", 0)), int(getattr(observation, "height", 0))],
            "title_hash": _digest(str(getattr(observation, "title", "")).casefold()),
            "ocr_hash": _digest(" ".join(str(getattr(observation, "text", "")).casefold().split())),
            "semantic_hash": semantic_hash, "visual_hash": self._visual_fingerprint(observation),
            "state": self._safe_state(self.live_state(observation, elements)),
        }
        return {"state_id": _digest(identity), **identity}

    def observe(self, observation: Any, elements: list[dict[str, Any]] | None = None) -> str:
        fingerprint = self.fingerprint(observation, elements)
        state_id = fingerprint.pop("state_id")
        when = _now()
        with self._lock:
            node = self._data["nodes"].get(state_id)
            if node is None:
                self._data["nodes"][state_id] = {**fingerprint, "first_seen": when, "last_seen": when, "visits": 1}
            else:
                node["last_seen"] = when
                node["visits"] = int(node.get("visits") or 0) + 1
            self._trim()
            self._save()
        return state_id

    @classmethod
    def evaluate_contract(cls, observation: Any | None, elements: list[dict[str, Any]] | None, contract: dict[str, Any] | None) -> dict[str, Any]:
        expected = contract or {}
        if not isinstance(expected, dict):
            return {"satisfied": False, "error": "state contract must be an object", "checks": []}
        unknown = sorted(set(expected) - CONTRACT_KEYS)
        if unknown:
            return {"satisfied": False, "error": f"unsupported state contract keys: {', '.join(unknown)}", "checks": []}
        if not expected:
            return {"satisfied": False, "error": "state contract must contain at least one expectation", "checks": []}
        if "operation" in expected and expected["operation"] not in {"idle", "busy", "blocked", "error"}:
            return {"satisfied": False, "error": "operation must be idle, busy, blocked, or error", "checks": []}
        if "modified" in expected and not isinstance(expected["modified"], bool):
            return {"satisfied": False, "error": "modified must be a boolean", "checks": []}
        if "modal" in expected and not (
            isinstance(expected["modal"], bool) or expected["modal"] in {"present", "absent"}
        ):
            return {"satisfied": False, "error": "modal must be a boolean, present, or absent", "checks": []}
        if "focused" in expected and isinstance(expected["focused"], dict):
            focused_unknown = sorted(set(expected["focused"]) - {"name", "role"})
            if focused_unknown or not expected["focused"]:
                detail = ", ".join(focused_unknown) if focused_unknown else "empty object"
                return {"satisfied": False, "error": f"invalid focused contract: {detail}", "checks": []}
        if "focused" in expected and not isinstance(expected["focused"], (str, dict)):
            return {"satisfied": False, "error": "focused must be a name/role string or object", "checks": []}
        if "control" in expected:
            control = expected["control"]
            allowed = {"name", "role", "present", "enabled", "focused", "selected", "toggle_state", "expand_state"}
            if not isinstance(control, dict):
                return {"satisfied": False, "error": "control contract must be an object", "checks": []}
            control_unknown = sorted(set(control) - allowed)
            if control_unknown:
                return {"satisfied": False, "error": f"unsupported control contract keys: {', '.join(control_unknown)}", "checks": []}
            if not any(key in control for key in ("name", "role")):
                return {"satisfied": False, "error": "control contract requires a name or role", "checks": []}
            for field in ("present", "enabled", "focused", "selected"):
                if field in control and not isinstance(control[field], bool):
                    return {"satisfied": False, "error": f"control.{field} must be a boolean", "checks": []}
        if "progress_at_least" in expected:
            value = expected["progress_at_least"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 100:
                return {"satisfied": False, "error": "progress_at_least must be a number from 0 to 100", "checks": []}
        if observation is None:
            wants_closed = _words(expected.get("window")) in {"closed", "absent"}
            return {"satisfied": wants_closed and len(expected) == 1,
                    "checks": [{"field": "window", "expected": expected.get("window"), "actual": "closed", "passed": wants_closed}]}
        live = cls.live_state(observation, elements)
        checks: list[dict[str, Any]] = []

        def add(field: str, wanted: Any, actual: Any, passed: bool) -> None:
            checks.append({"field": field, "expected": wanted, "actual": actual, "passed": bool(passed)})

        if "window" in expected:
            wanted, window = expected["window"], live["window"]
            add("window", wanted, {"title": window["title"], "process": window["process"]},
                _matches(window["title"], wanted) or _matches(window["process"], wanted))
        if "operation" in expected:
            wanted = str(expected["operation"]).casefold()
            add("operation", wanted, live["operation"], live["operation"] == wanted)
        if "modified" in expected:
            wanted = bool(expected["modified"])
            add("modified", wanted, live["modified"], live["modified"] is wanted)
        if "modal" in expected:
            raw = expected["modal"]
            wanted = raw if isinstance(raw, bool) else str(raw).casefold() == "present"
            actual = bool(live["modals"])
            add("modal", wanted, actual, actual is wanted)
        if "active_tab" in expected:
            wanted = expected["active_tab"]
            actual = [item.get("name") or item.get("automation_id") or item.get("key") for item in live["active_tabs"]]
            add("active_tab", wanted, actual, any(_matches(value, wanted) for value in actual))
        if "focused" in expected:
            wanted, focused = expected["focused"], live.get("focused") or {}
            if isinstance(wanted, str):
                passed = any(_matches(focused.get(key), wanted) for key in ("name", "automation_id", "role"))
            elif isinstance(wanted, dict):
                passed = all(
                    any(_matches(focused.get(key), value) for key in (("name", "automation_id") if field == "name" else ("role",)))
                    for field, value in wanted.items() if field in {"name", "role"}
                ) and bool(wanted)
            else:
                passed = False
            add("focused", wanted, {key: focused.get(key) for key in ("name", "automation_id", "role")}, passed)
        if "control" in expected:
            wanted = expected["control"]
            if not isinstance(wanted, dict):
                add("control", wanted, None, False)
            else:
                candidates = live["controls"]
                if "name" in wanted:
                    candidates = [item for item in candidates if _matches(item.get("name"), wanted["name"]) or _matches(item.get("automation_id"), wanted["name"])]
                if "role" in wanted:
                    candidates = [item for item in candidates if _matches(item.get("role"), wanted["role"])]
                present, desired = bool(candidates), bool(wanted.get("present", True))
                passed = present is desired
                if present and desired:
                    for field in ("enabled", "focused", "selected"):
                        if field in wanted:
                            passed = passed and any(item.get(field) is bool(wanted[field]) for item in candidates)
                    for field in ("toggle_state", "expand_state"):
                        if field in wanted:
                            passed = passed and any(_matches(item.get(field), wanted[field]) for item in candidates)
                actual = [{key: item.get(key) for key in ("name", "automation_id", "role", "enabled", "focused", "selected", "toggle_state", "expand_state")} for item in candidates[:10]]
                add("control", wanted, actual, passed)
        if "progress_at_least" in expected:
            try:
                wanted = float(expected["progress_at_least"])
            except (TypeError, ValueError):
                add("progress_at_least", expected["progress_at_least"], live["progress_percent"], False)
            else:
                values = live["progress_percent"]
                add("progress_at_least", wanted, values, bool(values) and max(values) >= wanted)
        return {"satisfied": bool(checks) and all(item["passed"] for item in checks), "checks": checks, "state": live}

    @classmethod
    def format_live(cls, observation: Any, elements: list[dict[str, Any]] | None = None) -> str:
        live = cls.live_state(observation, elements)
        focused = live.get("focused") or {}
        tabs = [item.get("name") or item.get("automation_id") or item.get("key") for item in live["active_tabs"]]
        rows = [
            f"UI state: {live['window']['title']!r} ({live['window']['process'] or 'unknown process'})",
            f"operation={live['operation']} | modified={live['modified']} | controls={live['summary']['controls']} | actionable={live['summary']['actionable']} | disabled={live['summary']['disabled']}",
            f"focused={focused.get('role') or 'none'}:{focused.get('name') or focused.get('automation_id') or 'none'} | active_tabs={tabs or 'none'} | modals={len(live['modals'])} | errors={len(live['errors'])} | progress={live['progress_percent'] or 'none'}",
            "Control hierarchy (each row includes child keys):",
        ]
        rows.extend(
            f"{item['key']} {item['role']} {item['name'] or item['automation_id'] or '(unnamed)'} | enabled={item['enabled']} focused={item['focused']} selected={item['selected']} toggle={item['toggle_state']} expand={item['expand_state']} | children={item['children']}"
            for item in live["controls"][:200]
        )
        return "\n".join(rows)

    def transition(self, before: Any, after: Any | None, *, action: str, target: str, source: str,
                   verified: bool, outcome: str, before_elements: list[dict[str, Any]] | None = None,
                   after_elements: list[dict[str, Any]] | None = None, contract: dict[str, Any] | None = None,
                   contract_result: dict[str, Any] | None = None) -> str:
        before_id = self.observe(before, before_elements)
        after_id = self.observe(after, after_elements) if after is not None else "closed"
        key_data = {"from": before_id, "to": after_id, "action": action, "source": source, "outcome": outcome}
        edge_id, when = _digest(key_data), _now()
        safe_contract: dict[str, Any] = {}
        if contract:
            checks = (contract_result or {}).get("checks") or []
            safe_contract = {"contract_hash": _digest(contract), "checks": len(checks),
                             "passed": sum(bool(item.get("passed")) for item in checks if isinstance(item, dict)),
                             "satisfied": bool((contract_result or {}).get("satisfied"))}
        with self._lock:
            edge = self._data["edges"].get(edge_id)
            if edge is None:
                edge = {**key_data, "first_seen": when, "last_seen": when, "attempts": 1, "verified_count": int(bool(verified))}
                self._data["edges"][edge_id] = edge
            else:
                edge["last_seen"] = when
                edge["attempts"] = int(edge.get("attempts") or 0) + 1
                edge["verified_count"] = int(edge.get("verified_count") or 0) + int(bool(verified))
            if safe_contract:
                edge["last_contract"] = safe_contract
            self._data["history"].append({"edge_id": edge_id, "at": when, "verified": bool(verified), **({"contract": safe_contract} if safe_contract else {})})
            self._data["history"] = self._data["history"][-200:]
            self._trim()
            self._save()
        return f"{before_id}->{after_id}"

    def _trim(self) -> None:
        edges = self._data["edges"]
        if len(edges) > self.max_edges:
            for key in sorted(edges, key=lambda item: edges[item].get("last_seen", ""))[:len(edges) - self.max_edges]:
                del edges[key]
        nodes = self._data["nodes"]
        if len(nodes) > self.max_nodes:
            newest = set(sorted(nodes, key=lambda item: nodes[item].get("last_seen", ""), reverse=True)[:self.max_nodes])
            for key in list(nodes):
                if key not in newest:
                    del nodes[key]
            for key, edge in list(edges.items()):
                if edge.get("from") not in nodes or (edge.get("to") != "closed" and edge.get("to") not in nodes):
                    del edges[key]
        self._data["history"] = [event for event in self._data["history"] if event.get("edge_id") in edges][-200:]

    def status(self) -> str:
        with self._lock:
            if self._load_error:
                persistence = f"disabled to preserve unreadable file ({self._load_error})"
            elif self._save_error:
                persistence = f"memory-only after write failure ({self._save_error})"
            else:
                persistence = f"atomic JSON at {self.path}"
            verified = sum(int(edge.get("verified_count") or 0) for edge in self._data["edges"].values())
            attempts = sum(int(edge.get("attempts") or 0) for edge in self._data["edges"].values())
            migration = f" | migrated_from=v{self._migrated_from}" if self._migrated_from else ""
            return (f"UI State Graph v{SCHEMA_VERSION} ready | states={len(self._data['nodes'])} | transitions={len(self._data['edges'])} | "
                    f"verified_attempts={verified}/{attempts} | persistence={persistence}{migration} | "
                    "model=hierarchy+focus+selection+tabs+modal+modified+progress+error+operation+contracts | "
                    "privacy=no-screenshots,no-OCR-text,no-window-titles,no-control-labels,no-values | mode=observational-no-autonomous-replay")

    def recent(self, limit: int = 10) -> str:
        cap = max(1, min(int(limit), 50))
        with self._lock:
            rows = []
            for event in reversed(self._data["history"][-cap:]):
                edge = self._data["edges"].get(event.get("edge_id"), {})
                contract = event.get("contract") or {}
                contract_text = f" | contract={contract.get('passed', 0)}/{contract.get('checks', 0)}" if contract else ""
                rows.append(f"{event.get('at')} | {edge.get('action', 'unknown')} via {edge.get('source', 'unknown')} | "
                            f"{edge.get('from', '?')}->{edge.get('to', '?')} | outcome={edge.get('outcome', 'unknown')} | "
                            f"verified={bool(event.get('verified'))}{contract_text}")
            return "\n".join(rows) if rows else "UI State Graph has no recorded transitions yet."
