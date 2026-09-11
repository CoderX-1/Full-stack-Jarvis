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
            (package / "ears.py").write_text(
                "import numpy as np\n\n"
                "def warm():\n    return 'warm'\n\n"
                "def transcribe(pcm: np.ndarray) -> str:\n"
                "    model = warm()\n"
                "    return str(model)\n\n"
                "class Ears:\n    pass\n",
                encoding="utf-8",
            )
            (package / "mouth.py").write_text(
                '''def _elevenlabs_ready():\n    return False\n\n'''
                '''def synth_stream(text: str, timeout: float = 30.0):
    """One sentence -> yields (sample_rate, pcm_chunk) as the TTS
    renders. ElevenLabs when configured, Kokoro otherwise — and Kokoro
    as the fallback on ANY ElevenLabs failure. Degrade, never mute."""
    if _elevenlabs_ready():
        try:
            for pcm in _stream_elevenlabs(text, timeout):
                yield EL_RATE, pcm
            return
        except Exception as e:
            log(f"[mouth] elevenlabs failed ({str(e)[:60]}) — "
                f"falling back to {CFG['voice']}")
    for pcm in _stream_kokoro(text):
        yield KOKORO_RATE, pcm
''',
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
            ears = (package / "ears.py").read_text(encoding="utf-8")
            mouth = (package / "mouth.py").read_text(encoding="utf-8")
            self.assertNotIn("claude-agent-sdk", project)
            self.assertEqual(project.count('"transformers>=4.45.0"'), 1)
            self.assertIn("from backtalk.brain import", main)
            self.assertIn("configured AI provider", main)
            self.assertIn("API key is missing", main)
            self.assertIn("account is out of API credit", main)
            self.assertEqual(config.count('"provider": "openai"'), 1)
            self.assertIn('"fullstack-agent-main" / "agent.py"', brain)
            self.assertIn("sys.path.insert(0, runner_dir)", brain)
            self.assertIn("unavailable at startup", brain)
            self.assertIn("ProviderConfig.from_env(provider=fallback)", brain)
            self.assertIn("Gemini multilingual STT first", ears)
            self.assertIn("try_gemini_transcribe", ears)
            self.assertEqual(ears.count("def warm():\n"), 1)
            self.assertEqual(
                ears.count("def transcribe(pcm: np.ndarray) -> str:\n"), 1)
            self.assertEqual(ears.count("def _warm_local():\n"), 1)
            self.assertEqual(
                ears.count("def _transcribe_local(pcm: np.ndarray) -> str:\n"),
                1)
            self.assertIn("Gemini streaming TTS first", mouth)
            self.assertIn("stream_local_urdu", mouth)
            self.assertTrue((package / "speech_router.py").is_file())
            self.assertTrue((root / "PROVIDER_COMPATIBILITY.md").is_file())


if __name__ == "__main__":
    unittest.main()
