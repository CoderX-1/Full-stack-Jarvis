"""Safe smoke test for deployed Action Watchdog Component 2."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    core = args.core.resolve()
    sys.path.insert(0, str(core))

    from action_watchdog import ActionWatchdog, DEFAULT_DEADLINES, WatchdogBusyError
    from jarvis_mark2 import MARK2_TOOL_NAMES, READ_ONLY_MARK2_TOOLS, Mark2Runtime

    mutations = MARK2_TOOL_NAMES - READ_ONLY_MARK2_TOOLS
    if mutations != set(DEFAULT_DEADLINES):
        raise RuntimeError("mutating tool and watchdog deadline coverage differ")

    contained: list[bool] = []
    with tempfile.TemporaryDirectory() as directory:
        state_dir = Path(directory)
        watchdog = ActionWatchdog(
            state_dir, lambda: contained.append(True), {"media_control": 0.05},
        )
        lease = watchdog.begin("media_control", {"action": "mute"})
        try:
            watchdog.begin("media_control", {"action": "volume_up"})
        except WatchdogBusyError:
            overlap_refused = True
        else:
            overlap_refused = False
        time.sleep(0.09)
        record = watchdog.finish(lease, "completed")
        persisted = json.loads((state_dir / "watchdog.json").read_text(encoding="utf-8"))
        if not overlap_refused:
            raise RuntimeError("overlapping mutation was not refused")
        if contained != [True] or record.get("status") != "timed-out-contained":
            raise RuntimeError("deadline did not trigger containment and late-success rejection")
        if persisted.get("current") is not None or persisted.get("recent", [])[-1] != record:
            raise RuntimeError("watchdog state was not atomically finalized")
        if any("args" in item or "arguments" in item for item in persisted.get("recent", [])):
            raise RuntimeError("watchdog persisted action arguments")

        runtime = Mark2Runtime(state_dir / "runtime")
        status = runtime.execute("watchdog_status", {})
        if "component 2 ready" not in status or "deadline_policies=18" not in status:
            raise RuntimeError(f"unexpected watchdog status: {status}")

    print(f"PASS core={core}")
    print(status)
    print("coverage=18/18")
    print("overlap=refused")
    print("timeout=contained")
    print("late_success=discarded")
    print("persistence=atomic-bounded-no-arguments")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
