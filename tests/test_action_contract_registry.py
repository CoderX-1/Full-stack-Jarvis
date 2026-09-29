import tempfile
import unittest
from pathlib import Path

from action_watchdog import DEFAULT_DEADLINES
from jarvis_mark2 import MARK2_TOOL_NAMES, READ_ONLY_MARK2_TOOLS, Mark2Runtime
from verification_engine import ACTION_CONTRACTS, VerificationEngine


class ActionContractRegistryTests(unittest.TestCase):
    def test_every_mutating_tool_has_contract_and_watchdog_deadline(self) -> None:
        mutating = MARK2_TOOL_NAMES - READ_ONLY_MARK2_TOOLS
        self.assertEqual(mutating, set(ACTION_CONTRACTS))
        self.assertEqual(mutating, set(DEFAULT_DEADLINES))
        self.assertTrue(all(value > 0 for value in DEFAULT_DEADLINES.values()))

    def test_app_foundry_contracts_distinguish_success_from_error(self) -> None:
        engine = VerificationEngine()
        for tool, success in (
            ("create_verified_app", "Verified app created: Focus Timer; not launched"),
            ("launch_generated_app", "Verified app launched: Focus Timer; pid=123; integrity=verified"),
            ("launch_generated_app", "Verified app Focus Timer is already running; pid=123"),
        ):
            with self.subTest(tool=tool, success=success):
                self.assertEqual(engine.evaluate(tool, {}, success).status, "verified")
                self.assertEqual(engine.evaluate(tool, {}, "error: refused").status, "failed")

    def test_create_app_routes_through_live_watchdog_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Mark2Runtime(Path(temporary))
            result = runtime.execute("create_verified_app", {
                "name": "Contract Fixture",
                "description": "Offline test fixture",
                "entrypoint": "main.py",
                "files": [{"path": "main.py", "content": "value = 1\n"}],
            })
            self.assertIn("Verified app created:", result)
            self.assertNotIn("unknown:", result.casefold())
            self.assertEqual(runtime.watchdog.deadline_for("create_verified_app", {}), 30)
            self.assertEqual(runtime.watchdog.deadline_for("launch_generated_app", {}), 20)

    def test_launch_app_routes_through_live_watchdog_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Mark2Runtime(Path(temporary))
            runtime.execute("create_verified_app", {
                "name": "Launch Fixture",
                "description": "Offline test fixture",
                "entrypoint": "main.py",
                "files": [{"path": "main.py", "content": "import time\ntime.sleep(5)\n"}],
            })
            try:
                result = runtime.execute("launch_generated_app", {"name": "Launch Fixture"})
                self.assertIn("Verified app launched:", result)
                self.assertIn("integrity=verified", result)
            finally:
                for process in runtime.app_foundry._live.values():
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
