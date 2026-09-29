import tempfile
import unittest
from pathlib import Path

from jarvis_mark2 import MARK2_TOOL_NAMES, READ_ONLY_MARK2_TOOLS, Mark2Runtime
from voice_health import summarize_voice_log


class VoiceHealthTests(unittest.TestCase):
    def test_last_session_only_and_never_returns_transcript_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "backtalk.log"
            log.write_text(
                "2026-09-01 01:00:00 [backtalk] up\n"
                "2026-09-01 01:00:00 [mouth] synth/play error: old failure\n"
                "2026-09-02 01:00:00 [backtalk] up\n"
                "2026-09-02 01:00:00 [you] private medical question API_KEY=never-expose\n"
                "2026-09-02 01:00:01 [ears] Gemini Live STT ok post_release_ms=100 total_ms=1000\n"
                "2026-09-02 01:00:02 [ears] Gemini Live STT ok post_release_ms=200 total_ms=2000\n"
                "2026-09-02 01:00:03 [ears] Gemini Live STT ok post_release_ms=300 total_ms=3000\n"
                "2026-09-02 01:00:04 [mouth] Fish TTS ok first_audio_ms=400 total_ms=4000 voice=secret-voice\n"
                "2026-09-02 01:00:05 [mouth] Gemini TTS ok first_audio_ms=500 total_ms=5000\n"
                "2026-09-02 01:00:06 [mouth] Fish TTS ok first_audio_ms=600 total_ms=6000\n"
                "2026-09-02 01:00:07 [ears] Gemini transcription unavailable; using local Whisper\n"
                "2026-09-02 01:00:08 [you] [mouth] Fish TTS ok first_audio_ms=1 total_ms=1\n",
                encoding="utf-8",
            )
            result = summarize_voice_log(log)
        self.assertIn("stt_success=3", result)
        self.assertIn("stt_fallback_events=1", result)
        self.assertIn("stt_post_release_ms_p50/p95=200/300", result)
        self.assertIn("tts_first_audio_ms_p50/p95=500/600", result)
        self.assertIn("tts_error_events=0", result)
        self.assertNotIn("private medical", result)
        self.assertNotIn("never-expose", result)
        self.assertNotIn("secret-voice", result)

    def test_missing_log_is_truthful_and_read_only_tool_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.assertIn("no readable Backtalk log", summarize_voice_log(home / "missing.log"))
            runtime = Mark2Runtime(home)
            self.assertIn("no readable Backtalk log", runtime.execute("voice_health", {}))
            self.assertFalse((home / "runtime").exists())
        self.assertIn("voice_health", MARK2_TOOL_NAMES)
        self.assertIn("voice_health", READ_ONLY_MARK2_TOOLS)


if __name__ == "__main__":
    unittest.main()
