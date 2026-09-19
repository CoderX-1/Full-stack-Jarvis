"""Privacy-safe selector ranking and self-healing memory for JARVIS.

The engine never delivers input.  It combines live locator evidence, prefers
stable identities, and remembers only hashed app/target identities plus
bounded success statistics.  Raw labels, OCR, titles, paths, and coordinates
are deliberately excluded from persistence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _digest(*parts: Any) -> str:
    payload = "\x1f".join(_normalized(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:24]


class SelectorEngine:
    """Rank live selectors and learn verified strategies without raw UI data."""

    SCHEMA = 1
    MAX_RECORDS = 512
    HALF_LIFE_SECONDS = 30 * 24 * 60 * 60
    SOURCE_PRIORS = {
        "uia-stable-id": 0.99,
        "access-key-identity": 0.97,
        "uia-exact": 0.96,
        "ocr-exact": 0.94,
        "local-template": 0.92,
        "uia-context": 0.89,
        "ocr-fuzzy": 0.86,
        "ocr-enhanced": 0.82,
        "local-pixel-grounding": 0.76,
        "local-vlm": 0.69,
        "unknown": 0.55,
    }

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._load_error = ""
        self._data: dict[str, Any] = {"schema": self.SCHEMA, "records": {}}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict) or parsed.get("schema") != self.SCHEMA:
                raise ValueError("unsupported selector-memory schema")
            records = parsed.get("records")
            if not isinstance(records, dict):
                raise ValueError("selector-memory records are invalid")
            clean: dict[str, dict[str, Any]] = {}
            for key, row in records.items():
                if not isinstance(key, str) or not isinstance(row, dict):
                    continue
                strategy = str(row.get("strategy") or "")
                if strategy not in self.SOURCE_PRIORS:
                    continue
                clean[key[:96]] = {
                    "strategy": strategy,
                    "successes": max(0.0, float(row.get("successes") or 0.0)),
                    "failures": max(0.0, float(row.get("failures") or 0.0)),
                    "updated": max(0.0, float(row.get("updated") or 0.0)),
                }
            self._data = {"schema": self.SCHEMA, "records": clean}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._load_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            # Preserve corrupt evidence for diagnosis instead of silently
            # destroying it.  Failure to preserve is non-fatal and status() is
            # still honest about the load error.
            try:
                stamp = int(self.clock())
                preserved = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
                os.replace(self.path, preserved)
            except OSError:
                pass
            self._data = {"schema": self.SCHEMA, "records": {}}

    @staticmethod
    def app_key(process: str, version_fingerprint: str = "") -> str:
        return _digest("app", process or "unknown", version_fingerprint or "unversioned")

    @staticmethod
    def target_key(query: str, role: str = "", anchor: str = "") -> str:
        return _digest("target", query, role, anchor)

    @staticmethod
    def _identity_key(candidate: dict[str, Any]) -> str:
        stable = (
            candidate.get("automation_id"), candidate.get("runtime_id"),
            candidate.get("parent_runtime_id"), candidate.get("role"),
        )
        if any(stable[:3]):
            return _digest("stable", *stable)
        # This is returned live only. Geometry is never written to memory.
        return _digest(
            "ephemeral", candidate.get("source"), candidate.get("role"),
            candidate.get("text"), candidate.get("x"), candidate.get("y"),
            candidate.get("width"), candidate.get("height"),
        )

    @classmethod
    def _strategy(cls, candidate: dict[str, Any], query: str) -> str:
        source = str(candidate.get("source") or "")
        score = float(candidate.get("score") or 0.0)
        if source == "local-template":
            return source
        if source == "local-vlm":
            return source
        if source == "local-pixel-grounding":
            return source
        if source == "ocr-enhanced":
            return source
        if source == "ocr":
            return "ocr-exact" if score >= 0.995 else "ocr-fuzzy"
        if source in {"uia-fallback", "semantic-grounding"}:
            access_key = _normalized(candidate.get("access_key"))
            wanted = _normalized(query)
            if access_key and wanted and (wanted == access_key or wanted in access_key):
                return "access-key-identity"
            if candidate.get("automation_id") or candidate.get("runtime_id"):
                return "uia-stable-id" if score >= 0.995 else "uia-context"
            return "uia-exact" if score >= 0.995 else "uia-context"
        return "unknown"

    @staticmethod
    def _iou(first: dict[str, Any], second: dict[str, Any]) -> float:
        ax1, ay1 = int(first.get("x") or 0), int(first.get("y") or 0)
        ax2 = ax1 + max(0, int(first.get("width") or 0))
        ay2 = ay1 + max(0, int(first.get("height") or 0))
        bx1, by1 = int(second.get("x") or 0), int(second.get("y") or 0)
        bx2 = bx1 + max(0, int(second.get("width") or 0))
        by2 = by1 + max(0, int(second.get("height") or 0))
        intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
        union = max(1, (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - intersection)
        return intersection / union

    @classmethod
    def _same_visible_target(cls, first: dict[str, Any], second: dict[str, Any]) -> bool:
        if cls._iou(first, second) >= 0.45:
            return True
        distance = math.hypot(
            float(first.get("screen_x") or 0) - float(second.get("screen_x") or 0),
            float(first.get("screen_y") or 0) - float(second.get("screen_y") or 0),
        )
        size = max(8.0, min(
            float(first.get("width") or 1), float(first.get("height") or 1),
            float(second.get("width") or 1), float(second.get("height") or 1),
        ))
        return distance <= size * 0.35

    def _memory_reliability(self, key: str, now: float) -> float:
        row = self._data["records"].get(key)
        if not row:
            return 0.5
        age = max(0.0, now - float(row.get("updated") or 0.0))
        decay = 0.5 ** (age / self.HALF_LIFE_SECONDS)
        successes = float(row.get("successes") or 0.0) * decay
        failures = float(row.get("failures") or 0.0) * decay
        # Beta prior prevents one lucky result from dominating live evidence.
        return (successes + 2.0) / (successes + failures + 4.0)

    @staticmethod
    def _anchor_rows(elements: list[dict[str, Any]], anchor: str) -> list[dict[str, Any]]:
        wanted = _normalized(anchor)
        if not wanted:
            return []
        rows: list[dict[str, Any]] = []
        for item in elements:
            labels = [_normalized(item.get(field)) for field in ("name", "id", "help")]
            if any(wanted == label or wanted in label for label in labels if label):
                rows.append(item)
        return rows

    @classmethod
    def _context_bonus(
        cls, candidate: dict[str, Any], elements: list[dict[str, Any]], anchor: str,
    ) -> float:
        anchors = cls._anchor_rows(elements, anchor)
        if not anchors:
            return 0.0
        parent = str(candidate.get("parent_runtime_id") or "")
        if parent:
            return 0.08 if any(
                parent == str(row.get("runtime_id") or "") for row in anchors
            ) else 0.0
        cx, cy = float(candidate.get("screen_x") or 0), float(candidate.get("screen_y") or 0)
        nearest = min((
            math.hypot(
                cx - (float(row.get("x") or 0) + float(row.get("width") or 0) / 2),
                cy - (float(row.get("y") or 0) + float(row.get("height") or 0) / 2),
            )
            for row in anchors
        ), default=float("inf"))
        return max(0.0, 0.06 - min(0.06, nearest / 8000.0))

    def rank(
        self, candidates: list[dict[str, Any]], *, process: str,
        version_fingerprint: str, query: str, role: str = "", anchor: str = "",
        elements: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return fused, deterministically ranked live candidates."""
        now = float(self.clock())
        app_key = self.app_key(process, version_fingerprint)
        target_key = self.target_key(query, role, anchor)
        wanted_role = _normalized(role).replace("controltype.", "")
        anchor_rows = self._anchor_rows(elements or [], anchor)
        anchor_ids = {str(row.get("runtime_id") or "") for row in anchor_rows if row.get("runtime_id")}
        has_structural_anchor = bool(anchor_ids) and any(
            str(item.get("parent_runtime_id") or "") in anchor_ids for item in candidates
        )
        scored: list[dict[str, Any]] = []
        for original in candidates:
            candidate = dict(original)
            strategy = self._strategy(candidate, query)
            candidate_role = _normalized(candidate.get("role")).replace("controltype.", "")
            if wanted_role and candidate_role and wanted_role not in candidate_role:
                continue
            if has_structural_anchor and str(candidate.get("parent_runtime_id") or "") not in anchor_ids:
                continue
            memory_key = f"{app_key}:{target_key}:{strategy}"
            reliability = self._memory_reliability(memory_key, now)
            identity_bonus = 0.035 if candidate.get("automation_id") or candidate.get("runtime_id") else 0.0
            role_bonus = 0.035 if wanted_role and wanted_role in candidate_role else 0.0
            context_bonus = self._context_bonus(candidate, elements or [], anchor)
            live_score = min(1.0, max(0.0, float(candidate.get("score") or 0.0)))
            ranked_score = (
                0.52 * self.SOURCE_PRIORS[strategy]
                + 0.36 * live_score
                + 0.12 * reliability
                + identity_bonus + role_bonus + context_bonus
            )
            strategies = [strategy]
            access_key = str(candidate.get("access_key") or "").strip()
            if access_key and len(access_key) <= 32 and any(
                modifier in access_key.casefold() for modifier in ("alt+", "ctrl+", "shift+", "win+")
            ) and "access-key-identity" not in strategies:
                # The access key is live alternative identity evidence.  It is
                # never persisted or pressed automatically by this engine.
                strategies.append("access-key-identity")
            candidate.update({
                "selector_strategy": strategy,
                "selector_strategies": strategies,
                "selector_score": round(min(1.0, ranked_score), 4),
                "selector_id": self._identity_key(candidate),
                "_selector_app_key": app_key,
                "_selector_target_key": target_key,
            })
            scored.append(candidate)

        fused: list[dict[str, Any]] = []
        for candidate in sorted(
            scored,
            key=lambda item: (-float(item["selector_score"]), -float(item.get("score") or 0.0), int(item.get("y") or 0), int(item.get("x") or 0)),
        ):
            existing = next((item for item in fused if self._same_visible_target(item, candidate)), None)
            if existing is None:
                fused.append(candidate)
                continue
            strategies = list(existing.get("selector_strategies") or [])
            if candidate["selector_strategy"] not in strategies:
                strategies.append(candidate["selector_strategy"])
            existing["selector_strategies"] = strategies
            existing["selector_score"] = round(min(1.0, float(existing["selector_score"]) + 0.025), 4)
        return sorted(
            fused,
            key=lambda item: (-float(item["selector_score"]), int(item.get("y") or 0), int(item.get("x") or 0)),
        )[:20]

    @staticmethod
    def revalidate(
        previous: dict[str, Any], candidates: list[dict[str, Any]], occurrence: int = 0,
    ) -> tuple[dict[str, Any] | None, str]:
        """Repair a selector against a fresh observation without using stale coordinates."""
        if not candidates:
            return None, "selector disappeared in the fresh observation"
        previous_id = str(previous.get("selector_id") or "")
        stable = [item for item in candidates if previous_id and str(item.get("selector_id") or "") == previous_id]
        if len(stable) == 1:
            return stable[0], "stable identity"
        if occurrence > 0:
            index = occurrence - 1
            if index < len(candidates):
                return candidates[index], "explicit occurrence"
            return None, f"occurrence {occurrence} exceeds {len(candidates)} fresh match(es)"
        previous_strategies = set(previous.get("selector_strategies") or [previous.get("selector_strategy")])
        compatible = [
            item for item in candidates
            if previous_strategies & set(item.get("selector_strategies") or [item.get("selector_strategy")])
            and _normalized(item.get("role")) == _normalized(previous.get("role"))
        ]
        if len(compatible) == 1:
            return compatible[0], "role and strategy repair"
        if len(candidates) == 1:
            return candidates[0], "unique fresh alternative"
        margin = float(candidates[0].get("selector_score") or 0.0) - float(candidates[1].get("selector_score") or 0.0)
        if margin >= 0.08:
            return candidates[0], "clear confidence margin"
        return None, f"fresh selector remains ambiguous ({len(candidates)} matches)"

    def record_result(self, candidate: dict[str, Any], success: bool) -> None:
        app_key = str(candidate.get("_selector_app_key") or "")
        target_key = str(candidate.get("_selector_target_key") or "")
        strategy = str(candidate.get("selector_strategy") or "unknown")
        if not app_key or not target_key or strategy not in self.SOURCE_PRIORS:
            return
        now = float(self.clock())
        key = f"{app_key}:{target_key}:{strategy}"
        existing = self._data["records"].get(key, {})
        age = max(0.0, now - float(existing.get("updated") or now))
        decay = 0.5 ** (age / self.HALF_LIFE_SECONDS)
        row = {
            "strategy": strategy,
            "successes": float(existing.get("successes") or 0.0) * decay + (1.0 if success else 0.0),
            "failures": float(existing.get("failures") or 0.0) * decay + (0.0 if success else 1.0),
            "updated": now,
        }
        self._data["records"][key] = row
        ordered = sorted(
            self._data["records"].items(),
            key=lambda pair: float(pair[1].get("updated") or 0.0), reverse=True,
        )[: self.MAX_RECORDS]
        self._data["records"] = dict(ordered)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        payload = json.dumps(self._data, separators=(",", ":"), sort_keys=True)
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)

    def status(self) -> str:
        records = list(self._data["records"].values())
        successes = sum(float(row.get("successes") or 0.0) for row in records)
        failures = sum(float(row.get("failures") or 0.0) for row in records)
        warning = f" | load_warning={self._load_error}" if self._load_error else ""
        return (
            "Self-Healing Selector Engine ready | ranking=stable-id+access-key+role+context+OCR+"
            "template+guarded-geometry+local-VLM | fresh_revalidation=required | blind_retries=disabled | "
            f"memory_records={len(records)}/{self.MAX_RECORDS} | verified_successes={successes:.1f} | "
            f"verified_failures={failures:.1f} | decay_half_life_days=30 | "
            "privacy=hashed-app-version-and-target-strategy-only,no-labels,no-OCR,no-titles,no-paths,no-coordinates"
            f"{warning}"
        )
