import tempfile
import unittest
from pathlib import Path

from provider_bridge.patch_backtalk import patch


class BacktalkPatchTests(unittest.TestCase):
    def test_patch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "backtalk"
            package.mkdir()
            (package / "brain.py").write_text("old brain\n", encoding="utf-8")
            (package / "main.py").write_text(
                "    from claude_agent_sdk import (PermissionResultAllow,\n"
                "                                  PermissionResultDeny)\n"
                "couldn't reach my brain, the Claude Code session.\n"
                "Claude Code isn't signed in, or the plan is out of usage.\n",
                encoding="utf-8",
            )
            (package / "config.py").write_text(
                'DEFAULTS = {\n    "name": "Assistant",\n'
                '    "model": "claude-sonnet-5",\n'
                '    "deep_model": "claude-opus-5",\n}\n',
                encoding="utf-8",
            )
            (root / "pyproject.toml").write_text(
                'dependencies = [\n    "claude-agent-sdk>=0.2.100",\n]\n',
                encoding="utf-8",
            )

            patch(root)
            patch(root)

            project = (root / "pyproject.toml").read_text(encoding="utf-8")
            main = (package / "main.py").read_text(encoding="utf-8")
            config = (package / "config.py").read_text(encoding="utf-8")
            brain = (package / "brain.py").read_text(encoding="utf-8")
            self.assertNotIn("claude-agent-sdk", project)
            self.assertIn("from backtalk.brain import", main)
            self.assertIn("configured AI provider", main)
            self.assertIn("API key is missing", main)
            self.assertIn("account is out of API credit", main)
            self.assertEqual(config.count('"provider": "openai"'), 1)
            self.assertIn('"fullstack-agent-main" / "agent.py"', brain)
            self.assertIn("sys.path.insert(0, runner_dir)", brain)
            self.assertIn("unavailable at startup", brain)
            self.assertIn("ProviderConfig.from_env(provider=fallback)", brain)
            self.assertTrue((root / "PROVIDER_COMPATIBILITY.md").is_file())


if __name__ == "__main__":
    unittest.main()
