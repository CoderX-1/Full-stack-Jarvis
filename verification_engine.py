"""Truthful, privacy-safe action outcome classification for JARVIS Mark II.

The existing adapters already collect strong evidence, but historically their
human-readable strings were reduced to a binary "does it start with error".
That made a successfully delivered key or mouse event look like verified goal
completion.  This module gives every mutating Mark II tool an explicit contract
and preserves the crucial distinction between delivery, observable response,
and verified completion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


@dataclass(frozen=True)
class ActionContract:
    contract_id: str
    verified_markers: tuple[str, ...] = ()
    observed_markers: tuple[str, ...] = ()
    delivered_markers: tuple[str, ...] = ()
    conditional_postcondition: bool = False


@dataclass(frozen=True)
class VerificationDecision:
    status: str
    contract: str
    input_delivered: bool
    observable_response: bool
    goal_verified: bool
    evidence: tuple[str, ...]

    def audit_record(self) -> dict[str, Any]:
        """Return bounded evidence codes only; never copy UI text or arguments."""
        value = asdict(self)
        value["evidence"] = list(self.evidence[:8])
        return value


# Every mutating tool must appear here. Tests compare this set with the public
# tool registry so a newly added action cannot silently bypass verification.
ACTION_CONTRACTS: dict[str, ActionContract] = {
    "register_project": ActionContract(
        "registry-readback", verified_markers=("registered project",),
    ),
    "open_project": ActionContract(
        "application-handoff", delivered_markers=("opened project",),
    ),
    "start_project": ActionContract(
        "process-or-health-ready",
        verified_markers=("started and verified project", "already running with pid"),
    ),
    "stop_project": ActionContract(
        "process-exit", verified_markers=("stopped project", "verified pid"),
    ),
    "run_project_build": ActionContract(
        "exit-code-zero", verified_markers=("build verified successfully with exit code 0",),
    ),
    "position_window": ActionContract(
        "observed-window-geometry", verified_markers=("verified window position",),
    ),
    "learn_visual_target": ActionContract(
        "template-artifact-written", verified_markers=("learned local visual target",),
    ),
    "click_visual_text": ActionContract(
        "explicit-ui-postcondition",
        verified_markers=("verified visual action",),
        observed_markers=("verified visual action",),
        conditional_postcondition=True,
    ),
    "click_visual_target": ActionContract(
        "explicit-ui-postcondition",
        verified_markers=("verified visual action",),
        observed_markers=("verified visual action",),
        conditional_postcondition=True,
    ),
    "save_diagnostic_snapshot": ActionContract(
        "redacted-artifact-written", verified_markers=("saved redacted diagnostic snapshot",),
    ),
    "launch_app": ActionContract(
        "new-or-focused-app-window",
        verified_markers=("opened and verified",),
        observed_markers=("launch sent", "foreground verification="),
    ),
    "open_item": ActionContract(
        "registered-application-handoff", delivered_markers=("opened ",),
    ),
    "control_window": ActionContract(
        "observed-window-state",
        verified_markers=("verified window action", "verified window position"),
    ),
    "send_keys": ActionContract(
        "target-retained-focus", delivered_markers=("delivered ", "target verified"),
    ),
    "type_text": ActionContract(
        "focused-control-content",
        verified_markers=("typed and verified",),
        observed_markers=("password-protected control", "target focus verified"),
    ),
    "mouse_action": ActionContract(
        "cursor-and-foreground", delivered_markers=("delivered mouse", "cursor verified"),
    ),
    "media_control": ActionContract(
        "media-key-delivery", delivered_markers=("delivered windows media action",),
    ),
    "interact_ui": ActionContract(
        "uia-observed-state", verified_markers=("verified ui action",),
    ),
    "browser_navigate": ActionContract(
        "browser-url-title", verified_markers=("verified browser navigation",),
    ),
    "browser_interact": ActionContract(
        "browser-explicit-postcondition", verified_markers=("verified browser interaction",),
    ),
    "play_youtube": ActionContract(
        "media-time-progression", verified_markers=("verified youtube playback",),
    ),
}


_FAILED = re.compile(r"(?:^|;\s*)error:", re.IGNORECASE)
_DENIED = re.compile(r"(?:^|;\s*)denied(?::|\b)", re.IGNORECASE)
_NO_DELIVERY = re.compile(r"no (?:click|input|keys?|text|scroll) (?:was|were) delivered", re.IGNORECASE)


class VerificationEngine:
    """Evaluate and enforce the contract for one completed tool call."""

    @staticmethod
    def _has_explicit_postcondition(args: dict[str, Any]) -> bool:
        return bool(
            str(args.get("expect_text") or "").strip()
            or str(args.get("expect_absent_text") or "").strip()
            or str(args.get("expect_window") or "").strip()
            or dict(args.get("expected_state") or {})
        )

    def evaluate(
        self,
        tool: str,
        args: dict[str, Any],
        result: str,
        *,
        read_only: bool = False,
    ) -> VerificationDecision:
        text = str(result or "").strip()
        lowered = text.casefold()
        delivered = "delivered" in lowered and not _NO_DELIVERY.search(lowered)

        if lowered.startswith("unknown:"):
            return VerificationDecision(
                "unknown", ACTION_CONTRACTS.get(tool, ActionContract("none")).contract_id,
                delivered, False, False, ("contract-not-satisfied", "blind-repeat-blocked"),
            )
        if _DENIED.search(lowered):
            return VerificationDecision(
                "denied", "safety-gate", delivered, False, False,
                ("policy-denied",),
            )
        if _FAILED.search(lowered):
            return VerificationDecision(
                "failed", ACTION_CONTRACTS.get(tool, ActionContract("none")).contract_id,
                delivered, False, False,
                (("input-may-have-been-delivered",) if delivered else ("failed-before-verification",)),
            )
        if read_only:
            return VerificationDecision(
                "observed", "read-only-observation", False, bool(text), bool(text),
                ("read-only-result",),
            )

        contract = ACTION_CONTRACTS.get(tool)
        if contract is None:
            return VerificationDecision(
                "unknown", "missing-contract", delivered, False, False,
                ("contract-missing",),
            )

        verified_marker = next(
            (marker for marker in contract.verified_markers if marker in lowered), None
        )
        observed_marker = next(
            (marker for marker in contract.observed_markers if marker in lowered), None
        )
        delivery_marker = next(
            (marker for marker in contract.delivered_markers if marker in lowered), None
        )

        if contract.conditional_postcondition and verified_marker:
            if self._has_explicit_postcondition(args):
                return VerificationDecision(
                    "verified", contract.contract_id, True, True, True,
                    ("explicit-postcondition", "fresh-after-state"),
                )
            return VerificationDecision(
                "observed", contract.contract_id, True, True, False,
                ("observable-response", "goal-postcondition-absent"),
            )
        if verified_marker:
            return VerificationDecision(
                "verified", contract.contract_id, delivered, True, True,
                ("contract-marker",),
            )
        if observed_marker:
            return VerificationDecision(
                "observed", contract.contract_id, delivered, True, False,
                ("observable-response", "goal-not-proven"),
            )
        if delivery_marker:
            return VerificationDecision(
                "delivered", contract.contract_id, True, False, False,
                ("input-delivery", "goal-not-proven"),
            )
        return VerificationDecision(
            "unknown", contract.contract_id, delivered, False, False,
            ("contract-not-satisfied",),
        )

    def enforce(
        self,
        tool: str,
        args: dict[str, Any],
        result: str,
        *,
        read_only: bool = False,
    ) -> str:
        """Keep verified/error results stable; label every non-goal outcome."""
        decision = self.evaluate(tool, args, result, read_only=read_only)
        if read_only or decision.status in {"verified", "failed", "denied"}:
            return result
        if str(result).casefold().startswith(("unverified:", "unknown:")):
            return result
        if decision.status == "observed":
            return (
                "unverified: an observable response occurred, but the requested goal "
                f"was not proven; contract={decision.contract}; {result}"
            )
        if decision.status == "delivered":
            return (
                "unverified: input/handoff was delivered, but goal completion was not "
                f"proven; do not repeat blindly; contract={decision.contract}; {result}"
            )
        return (
            "unknown: the action returned without satisfying its verification contract; "
            f"do not repeat blindly; contract={decision.contract}; {result}"
        )

    @staticmethod
    def status() -> str:
        conditional = sum(c.conditional_postcondition for c in ACTION_CONTRACTS.values())
        delivery_only = sum(
            bool(c.delivered_markers) and not c.verified_markers and not c.observed_markers
            for c in ACTION_CONTRACTS.values()
        )
        return (
            "Verification Engine 2.0 component 1 ready | "
            f"action_contracts={len(ACTION_CONTRACTS)} | conditional_postconditions={conditional} | "
            f"delivery_only={delivery_only} | outcomes=verified,observed,delivered,failed,denied,unknown | "
            "fail_closed=true | blind_repeat=false | audit_evidence=bounded-codes-only"
        )
