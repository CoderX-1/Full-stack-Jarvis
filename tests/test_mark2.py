import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import agent
from jarvis_mark2 import Mark2Runtime


class Mark2RegistryTests(unittest.TestCase):
    def test_register_list_search_and_audit_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "demo"
            project.mkdir()
            (project / "app.py").write_text("print('hello mark two')\n", encoding="utf-8")
            runtime = Mark2Runtime(home)

            result = runtime.register_project("Demo", "demo", description="test project")
            self.assertIn("Registered project Demo", result)
            self.assertIn("Demo:", runtime.list_projects())
            self.assertIn("app.py:1", runtime.search_project_files("Demo", "mark two"))

            runtime.audit(
                "register_project",
                {
                    "name": "Demo",
                    "API_KEY": "never-log-this",
                    "content": "private",
                    "nested": {"access_token": "also-never-log-this"},
                },
                result,
            )
            audit = runtime.audit_path.read_text(encoding="utf-8")
            self.assertNotIn("never-log-this", audit)
            self.assertNotIn("also-never-log-this", audit)
            self.assertNotIn("private", audit)
            self.assertIn("[REDACTED]", audit)
            self.assertEqual(json.loads(audit)["tool"], "register_project")

    def test_project_path_cannot_escape_agent_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mark2Runtime(Path(tmp))
            outside = Path(tmp).parent
            with self.assertRaisesRegex(ValueError, "inside the agent home"):
                runtime.register_project("Outside", str(outside))

    def test_health_checks_reject_remote_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mark2Runtime(Path(tmp))
            with self.assertRaisesRegex(ValueError, "localhost"):
                runtime.check_local_url("https://example.com")

    def test_registered_build_command_is_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "demo"
            project.mkdir()
            runtime = Mark2Runtime(home)
            command = f'"{sys.executable}" -c "print(12345)"'
            runtime.register_project("Demo", "demo", build_command=command)
            result = runtime.run_project_build("Demo")
            self.assertIn("exit code 0", result)
            self.assertIn("12345", result)

    def test_child_process_does_not_inherit_jarvis_api_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mark2Runtime(Path(tmp))
            previous = {
                name: os.environ.get(name)
                for name in (
                    "OPENAI_API_KEY",
                    "GEMINI_API_KEY",
                    "ELEVENLABS_API_KEY",
                    "AI_API_KEY",
                )
            }
            try:
                for name in previous:
                    os.environ[name] = "secret"
                child = runtime._child_env()
                for name in previous:
                    self.assertNotIn(name, child)
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_real_server_start_health_status_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "server"
            project.mkdir()
            (project / "index.html").write_text("MARK II ONLINE", encoding="utf-8")
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            runtime = Mark2Runtime(home)
            command = (
                f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'
            )
            runtime.register_project(
                "Server",
                "server",
                start_command=command,
                url=f"http://127.0.0.1:{port}/",
            )
            started = runtime.start_project("Server", verify_timeout=15)
            try:
                self.assertIn("Started and verified", started)
                self.assertIn("HTTP 200", started)
                status = runtime.project_status("Server")
                self.assertIn("started_by_this_session=True", status)
                self.assertIn("HTTP 200", status)
            finally:
                stopped = runtime.stop_project("Server")
            self.assertIn("verified PID", stopped)
            self.assertFalse(runtime._live_processes)


class Mark2PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_is_denied_without_permission_and_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "demo").mkdir()
            config = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(
                config,
                home,
                "instructions",
                fallback_config=None,
            )
            result = await runner._run_tool(
                "register_project",
                {"name": "Demo", "path": "demo"},
            )
            self.assertEqual(result, "denied by user")
            self.assertFalse(runner.mark2.registry_path.exists())
            self.assertTrue(runner.mark2.audit_path.exists())

    async def test_read_only_discovery_needs_no_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "demo"
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")
            config = agent.ProviderConfig("openai", "test", agent.OPENAI_BASE_URL, "test")
            runner = agent.LocalAgent(
                config,
                home,
                "instructions",
                fallback_config=None,
            )
            result = await runner._run_tool("discover_projects", {})
            self.assertIn("demo:", result)


if __name__ == "__main__":
    unittest.main()
