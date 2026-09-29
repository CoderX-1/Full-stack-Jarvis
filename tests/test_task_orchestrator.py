import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import agent
from task_orchestrator import TaskOrchestrator


def steps():
    return [
        {"description": "Inspect available apps", "kind": "observe", "expected_tool": "list_installed_apps"},
        {"description": "Open Calculator", "kind": "act", "expected_tool": "launch_app"},
    ]


class TaskOrchestratorTests(unittest.TestCase):
    def test_durable_plan_requires_fresh_expected_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tasks = TaskOrchestrator(home)
            first = json.loads(tasks.create_plan(steps()))
            self.assertEqual(first["status"], "active")
            self.assertEqual(json.loads(tasks.create_plan(steps()))["id"], first["id"])
            tasks.record_tool("list_installed_apps", "observed")
            self.assertIn("observed", tasks.complete_step(1, "list_installed_apps"))
            tasks.record_tool("launch_app", "delivered")
            self.assertIn("not goal-verified", tasks.complete_step(2, "launch_app"))
            tasks.record_tool("open_item", "verified")
            self.assertIn("does not match", tasks.complete_step(2, "open_item"))
            tasks.record_tool("launch_app", "verified")
            self.assertEqual(json.loads(tasks.complete_step(2, "launch_app"))["status"], "completed")
            reloaded = TaskOrchestrator(home)
            self.assertEqual(json.loads(reloaded.status())["status"], "completed")

    def test_plan_validation_and_no_cross_turn_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = TaskOrchestrator(Path(tmp))
            invalid = [{"description": "Launch Calculator", "kind": "act"},
                       {"description": "Observe", "kind": "observe"}]
            self.assertIn("expected_tool", tasks.create_plan(invalid))
            self.assertIn("refusing", tasks.create_plan([
                {"description": "api_key=abc", "kind": "observe"},
                {"description": "Open app", "kind": "act", "expected_tool": "launch_app"},
            ]))
            tasks.create_plan(steps())
            tasks.record_tool("list_installed_apps", "observed")
            tasks.begin_turn()
            self.assertIn("no fresh", tasks.complete_step(1, "list_installed_apps"))

    def test_cancel_does_not_undo_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = TaskOrchestrator(Path(tmp))
            tasks.create_plan(steps())
            self.assertEqual(json.loads(tasks.cancel())["status"], "cancelled")
            self.assertIn("no active", tasks.complete_step(1, "list_installed_apps"))


class AgentPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_action_uses_readback_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            result = await runner._run_tool("write_file", {"path": "example.txt", "content": "hello"})
            self.assertTrue(result.startswith("verified:"))
            self.assertEqual((Path(tmp) / "example.txt").read_text(encoding="utf-8"), "hello")

    async def test_premature_final_is_not_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            replies = iter([
                {"content": None, "tool_calls": [{"id": "plan-1", "function": {
                    "name": "create_task_plan", "arguments": json.dumps({"steps": steps()})}}]},
                {"content": "Done."},
                {"content": "I need to inspect apps first."},
                {"content": "Blocked."},
            ])
            calls = []

            async def reply():
                calls.append(1)
                return next(replies)

            runner._request = reply
            answer = await runner.ask("Open Calculator app and verify it")
            self.assertEqual(len(calls), 4)
            self.assertIn("incomplete", answer)
            self.assertEqual(json.loads(runner.tasks.status())["status"], "active")
            self.assertFalse(any("task plan is still active" in str(m.get("content"))
                                 for m in runner.messages))

    async def test_cancellation_requires_user_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            runner.tasks.create_plan(steps())
            self.assertIn("explicit user request", await runner._run_tool("cancel_task_plan", {}))
            runner._cancel_requested = True
            self.assertEqual(json.loads(await runner._run_tool("cancel_task_plan", {}))["status"], "cancelled")

    async def test_roman_urdu_cancel_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            runner.tasks.create_plan(steps())
            replies = iter([
                {"content": None, "tool_calls": [{"id": "cancel-1", "function": {
                    "name": "cancel_task_plan", "arguments": "{}"}}]},
                {"content": "Plan cancelled."},
            ])

            async def reply():
                return next(replies)

            runner._request = reply
            self.assertEqual(await runner.ask("task cancel karo"), "Plan cancelled.")
            self.assertEqual(json.loads(runner.tasks.status())["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
