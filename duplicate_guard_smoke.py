"""Safe deployed-copy smoke test for Duplicate Action Guard Component 3."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    core = args.core.resolve()
    sys.path.insert(0, str(core))

    from duplicate_guard import DuplicateActionGuard
    from jarvis_mark2 import Mark2Runtime

    first_route = DuplicateActionGuard.fingerprint(
        "click_visual_text",
        {"window": "Calculator", "text": "Seven", "expect_text": "7", "timeout_seconds": 2},
    )
    alternate_route = DuplicateActionGuard.fingerprint(
        "interact_ui",
        {"window": "calculator", "control": "seven", "action": "invoke"},
    )
    if first_route != alternate_route:
        raise RuntimeError("selector-route semantic canonicalization failed")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        runtime = Mark2Runtime(root)
        delivery = Mock(return_value="Delivered Windows media action mute")
        runtime.windows_control.media_control = delivery
        action = {"action": "mute", "private_note": "must-not-persist"}

        first = runtime.execute("media_control", action)
        runtime.audit("media_control", action, first)
        duplicate = runtime.execute("media_control", action)
        runtime.audit("media_control", action, duplicate)
        if not duplicate.startswith("denied: duplicate action suppressed"):
            raise RuntimeError("same-request duplicate was not suppressed")
        if delivery.call_count != 1:
            raise RuntimeError("suppressed mutation reached the adapter")

        runtime.begin_request()
        repeated = runtime.execute("media_control", action)
        if repeated.startswith("denied:") or delivery.call_count != 2:
            raise RuntimeError("deliberate new-request repeat was not allowed")

        persisted_text = (root / ".jarvis" / "duplicate-actions.json").read_text(encoding="utf-8")
        if "must-not-persist" in persisted_text or "private_note" in persisted_text:
            raise RuntimeError("raw action arguments were persisted")
        persisted = json.loads(persisted_text)
        if len(persisted.get("recent", [])) > 64:
            raise RuntimeError("duplicate history exceeded its bound")

        status = runtime.execute("duplicate_guard_status", {})
        if "component 3 ready" not in status or "blind_replay=false" not in status:
            raise RuntimeError(f"unexpected duplicate guard status: {status}")

    print(f"PASS core={core}")
    print(status)
    print("same_request_duplicate=blocked-before-adapter")
    print("selector_route_bypass=blocked")
    print("new_request_repeat=allowed")
    print("persistence=bounded-hashes-no-arguments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
