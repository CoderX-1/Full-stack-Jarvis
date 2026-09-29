import importlib.util
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np


def load_mouth(local_fallback=False):
    cfg = {
        "speech": {"allow_local_tts_fallback": local_fallback},
        "voice": "bm_lewis",
        "speed": 1.0,
        "elevenlabs": {"enabled": False, "voice_id": ""},
    }
    backtalk = types.ModuleType("backtalk")
    backtalk.__path__ = []
    config = types.ModuleType("backtalk.config")
    config.CFG = cfg
    vlog = types.ModuleType("backtalk.vlog")
    vlog.log = Mock()
    path = Path(os.environ.get("JARVIS_MOUTH_TEST_PATH") or (
        Path(__file__).parents[2] / "components" / "backtalk" /
        "backtalk" / "mouth.py"
    ))
    name = "mouth_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
            "backtalk": backtalk, "backtalk.config": config,
            "backtalk.vlog": vlog, name: module}):
        spec.loader.exec_module(module)
    return module


def router_stub(*, fish=True, gemini=True, fish_error=False):
    router = types.ModuleType("backtalk.speech_router")
    router.tts_provider_order = Mock(return_value=("fish", "gemini"))
    router.fish_tts_enabled = Mock(return_value=fish)
    router.fish_voice_status = Mock(return_value=(
        (True, "voice=12345678") if fish
        else (False, "reference-voice-missing")
    ))
    router.gemini_tts_enabled = Mock(return_value=gemini)
    router.contains_urdu_script = Mock(return_value=False)
    router.stream_local_urdu = Mock(side_effect=AssertionError(
        "local Urdu TTS must not run in cloud-only mode"))

    def fish_stream(_text):
        if fish_error:
            raise RuntimeError("fish offline")
        yield 24000, np.array([1, 2], dtype=np.int16)

    def gemini_stream(_text):
        yield 24000, np.array([3, 4], dtype=np.int16)

    router.stream_fish_tts = Mock(side_effect=fish_stream)
    router.stream_gemini_tts = Mock(side_effect=gemini_stream)
    return router


class MouthCloudRoutingTests(unittest.TestCase):
    def test_fish_is_primary_and_stops_route_after_audio(self):
        mouth = load_mouth()
        router = router_stub()
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            chunks = list(mouth.synth_stream("Hello"))
        self.assertEqual(chunks[0][0], 24000)
        np.testing.assert_array_equal(chunks[0][1], [1, 2])
        router.stream_gemini_tts.assert_not_called()

    def test_fish_pre_audio_failure_falls_back_to_gemini(self):
        mouth = load_mouth()
        router = router_stub(fish_error=True)
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            chunks = list(mouth.synth_stream("Hello"))
        np.testing.assert_array_equal(chunks[0][1], [3, 4])
        router.stream_gemini_tts.assert_called_once_with("Hello")

    def test_cloud_only_mode_never_calls_local_voice(self):
        mouth = load_mouth()
        router = router_stub(fish=False, gemini=False)
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            with self.assertRaisesRegex(RuntimeError, "local fallback disabled"):
                list(mouth.synth_stream("Hello"))
        router.stream_local_urdu.assert_not_called()

    def test_cloud_only_warmup_does_not_load_kokoro(self):
        mouth = load_mouth()
        router = router_stub()
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            self.assertIsNone(mouth.warm())
            self.assertTrue(mouth.preload_local_voice_runtime())
        self.assertIsNone(mouth._pipe)

    def test_partial_fish_audio_failure_is_not_silent_success(self):
        mouth = load_mouth()
        router = router_stub()

        def partial(_text):
            yield 24000, np.array([1, 2], dtype=np.int16)
            raise RuntimeError("secret request detail must stay private")

        router.stream_fish_tts = Mock(side_effect=partial)
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            generator = mouth.synth_stream("Hello")
            self.assertEqual(next(generator)[0], 24000)
            with self.assertRaisesRegex(RuntimeError, "spoken reply may be incomplete") as caught:
                next(generator)
        self.assertNotIn("secret request detail", str(caught.exception))
        router.stream_gemini_tts.assert_not_called()

    def test_both_cloud_providers_fail_before_audio_without_local_fallback(self):
        mouth = load_mouth()
        router = router_stub(fish_error=True)
        router.stream_gemini_tts = Mock(side_effect=RuntimeError("Gemini offline"))
        with patch.dict(sys.modules, {"backtalk.speech_router": router}):
            with self.assertRaisesRegex(RuntimeError, "cloud TTS route exhausted"):
                list(mouth.synth_stream("Hello"))
        router.stream_local_urdu.assert_not_called()

    def test_mouth_worker_reports_failure_without_leaking_and_processes_next_item(self):
        mouth = load_mouth()
        signals = types.ModuleType("backtalk.signals")
        signals.reply_done = Mock()
        signals.static_stop = Mock()
        signals.set_state = Mock()
        backtalk = types.ModuleType("backtalk")
        backtalk.signals = signals

        class TwoItems:
            def __init__(self):
                self.items = [("first", None), ("second", None)]

            def get(self):
                if self.items:
                    return self.items.pop(0)
                raise StopIteration

            def empty(self):
                return True

        worker = types.SimpleNamespace(
            _q=TwoItems(), _stop=threading.Event(),
            _speaking=threading.Event(), _audible=threading.Event(),
            ducker=types.SimpleNamespace(speech_end=Mock()),
            _play_stream=Mock(side_effect=[RuntimeError("secret request detail"), None]),
        )
        with patch.dict(sys.modules, {"backtalk": backtalk, "backtalk.signals": signals}):
            with self.assertRaises(StopIteration):
                mouth.Mouth._run(worker)
        self.assertEqual(worker._play_stream.call_count, 2)
        self.assertEqual(signals.reply_done.call_count, 2)
        self.assertFalse(worker._speaking.is_set())
        messages = " ".join(str(call) for call in mouth.log.call_args_list)
        self.assertIn("Speech delivery failed or ended early", messages)
        self.assertNotIn("secret request detail", messages)


if __name__ == "__main__":
    unittest.main()
