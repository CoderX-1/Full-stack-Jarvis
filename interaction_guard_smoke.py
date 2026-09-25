"""Safe deployed-copy smoke test for Interaction Guard Component 4."""

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

    from interaction_guard import InteractionGuard
    from jarvis_mark2 import Mark2Runtime

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        control = Mock()
        control.recent_physical_input.return_value = True
        guard = InteractionGuard(root, control)
        allowed, _ = guard.preflight("type_text", {"text": "must-not-persist"})
        if allowed:
            raise RuntimeError("recent human input was not blocked")

        control.recent_physical_input.return_value = False
        control.modal_blocker_snapshot.return_value = {
            "blocked": True, "sources": ["active-popup", "uia-blocked"],
            "dialog_selector": "hwnd:22:pid:33",
        }
        allowed, reason = guard.preflight("type_text", {"window": "parent"})
        if allowed or "hwnd:22:pid:33" not in reason:
            raise RuntimeError("modal-blocked parent action was not refused")

        control.modal_blocker_snapshot.return_value = {"blocked": False, "sources": []}
        allowed, _ = guard.preflight("interact_ui", {"window": "dialog"})
        if not allowed:
            raise RuntimeError("direct modal dialog target was incorrectly blocked")

        control.take_user_input_interruption.return_value = True
        guard.finish("interact_ui")
        if guard.take_last("interact_ui").get("status") != "stopped-user-input":
            raise RuntimeError("mid-action human input was not recorded")

        raw = (root / "interaction-guard.json").read_text(encoding="utf-8")
        if "must-not-persist" in raw:
            raise RuntimeError("interaction guard persisted action arguments")
        if len(json.loads(raw).get("recent", [])) > 64:
            raise RuntimeError("interaction history exceeded its bound")

        runtime = Mark2Runtime(root / "runtime")
        status = runtime.execute("interaction_guard_status", {})
        if "component 4 ready" not in status or "arguments_persisted=false" not in status:
            raise RuntimeError(f"unexpected interaction guard status: {status}")

    print(f"PASS core={core}")
    print(status)
    print("recent_human_input=blocked-before-adapter")
    print("mid_action_human_input=cooperative-stop")
    print("modal_parent=blocked")
    print("modal_dialog=allowed")
    print("persistence=bounded-no-arguments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
