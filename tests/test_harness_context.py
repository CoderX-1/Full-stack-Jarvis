import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import agent
from harness_context import HarnessMemory, route_lanes, select_tools


class HarnessRoutingTests(unittest.TestCase):
    def test_routes_multidomain_request_and_reduces_catalog(self):
        lanes = route_lanes("Research a website and fix my app bug")
        self.assertEqual(lanes, ("desktop", "research", "project"))
        names = {tool["function"]["name"] for tool in select_tools(agent.TOOLS, ("desktop",))}
        self.assertIn("launch_app", names)
        self.assertNotIn("run_command", names)
        self.assertIn("recall_memory", names)

    def test_ambiguous_request_keeps_every_capability(self):
        self.assertEqual(route_lanes("do that again"), ("general",))
        self.assertEqual(select_tools(agent.TOOLS, ("general",)), agent.TOOLS)


class HarnessMemoryTests(unittest.TestCase):
    def test_scoped_retrieval_and_secret_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = HarnessMemory(Path(tmp))
            self.assertIn("Remembered", memory.remember("Calculator is useful for arithmetic", "desktop"))
            self.assertIn("Remembered", memory.remember("Project uses pytest", "project"))
            self.assertEqual(memory.recall("Calculator arithmetic", ("desktop",)),
                             ["Calculator is useful for arithmetic"])
            self.assertEqual(memory.recall("Project pytest", ("desktop",)), [])
            self.assertTrue(memory.remember("api_key=abc", "general").startswith("error:"))
            self.assertEqual(len(memory.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_corrupt_line_cannot_break_retrieval(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = HarnessMemory(Path(tmp))
            memory.remember("launch Calculator", "desktop")
            with memory.path.open("a", encoding="utf-8") as handle:
                handle.write("not-json\n")
            self.assertEqual(memory.recall("launch", ("desktop",)), ["launch Calculator"])


class HarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_is_temporary_and_scoped_tool_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            captured = []

            async def reply():
                captured.append((runner.messages[-1]["content"],
                                 {tool["function"]["name"] for tool in runner.active_tools}))
                return {"content": "Done"}

            runner._request = reply
            original = runner.messages[0]["content"]
            self.assertEqual(await runner.ask("open Calculator app"), "Done")
            self.assertIn('"desktop"', captured[0][0])
            self.assertNotIn("run_command", captured[0][1])
            self.assertEqual(runner.messages[0]["content"], original)
            self.assertEqual(runner.messages[-1]["content"], "Done")

    async def test_memory_requires_explicit_request_even_with_auto_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(cfg, Path(tmp), "instructions", auto_approve=True)
            denied = await runner._run_tool("remember_fact", {"fact": "user likes tea", "lane": "general"})
            self.assertTrue(denied.startswith("error:"))
            runner._remember_requested = True
            saved = await runner._run_tool("remember_fact", {"fact": "user likes tea", "lane": "general"})
            self.assertIn("Remembered", saved)
            recalled = await runner._run_tool("recall_memory", {"query": "tea"})
            self.assertEqual(json.loads(recalled)["matches"], ["user likes tea"])


if __name__ == "__main__":
    unittest.main()
