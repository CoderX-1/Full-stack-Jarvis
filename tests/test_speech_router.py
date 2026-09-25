import base64
import importlib.util
import io
import os
import struct
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np


def load_router(cfg=None, env=None):
    cfg = cfg or {"speech": {}}
    backtalk = types.ModuleType("backtalk")
    backtalk.__path__ = []
    config = types.ModuleType("backtalk.config")
    config.CFG = cfg
    vlog = types.ModuleType("backtalk.vlog")
    vlog.log = Mock()
    path = Path(__file__).parents[1] / "provider_bridge" / "speech_router.py"
    name = "speech_router_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
            "backtalk": backtalk, "backtalk.config": config,
            "backtalk.vlog": vlog, name: module}):
        with patch.dict(os.environ, env or {}, clear=True):
            spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


class StreamResponse(Response):
    def __init__(self, lines, status=200):
        super().__init__({}, status)
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        return iter(self.lines)


class FishResponse(Response):
    def __init__(self, chunks, status=200):
        super().__init__({}, status)
        self.chunks = chunks

    def iter_bytes(self, chunk_size=4096):
        return iter(self.chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakePipe:
    def __init__(self, reads=None):
        self.reads = list(reads or [])
        self.writes = []

    def read(self, _size):
        return self.reads.pop(0) if self.reads else b""

    def write(self, data):
        self.writes.append(data)

    def close(self):
        pass


class FakeProcess:
    def __init__(self, pcm=b""):
        self.stdin = FakePipe()
        self.stdout = FakePipe([pcm] if pcm else [])
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.request = None

    def stream(self, method, url, **kwargs):
        self.request = (method, url, kwargs)
        return self.response


class SpeechRouterTests(unittest.TestCase):
    def test_stt_and_tts_circuits_use_independent_cooldowns(self):
        router = load_router({"speech": {
            "failure_cooldown_s": 90,
            "stt_failure_cooldown_s": 120,
            "tts_failure_cooldown_s": 3600,
        }})
        with patch.object(router.time, "monotonic", return_value=1000.0):
            router._stt_circuit.trip()
            router._tts_circuit.trip()
        self.assertEqual(router._stt_circuit.retry_at, 1120.0)
        self.assertEqual(router._tts_circuit.retry_at, 4600.0)

    def test_cloud_tts_route_is_ordered_validated_and_deduplicated(self):
        router = load_router({"speech": {
            "tts_provider": "fish",
            "tts_fallback_providers": ["gemini", "fish", "unknown"],
        }})
        self.assertEqual(router.tts_provider_order(), ("fish", "gemini"))

    def test_fish_tts_uses_free_model_header_and_never_puts_key_in_body(self):
        cfg = {"speech": {
            "tts_provider": "fish",
            "tts_fallback_providers": ["gemini"],
            "fish_model": "s2.1-pro-free",
            "fish_reference_id": "voice-reference",
            "fish_temperature": 0.2,
            "fish_top_p": 0.5,
            "fish_latency": "balanced",
            "fish_chunk_length": 200,
            "fish_speed": 1.0,
        }}
        router = load_router(cfg, {"FISH_API_KEY": "fish-secret"})
        pcm = struct.pack("<hhh", -20, 0, 20)
        process = FakeProcess(pcm)
        client = FakeClient(FishResponse([b"fake-mp3"]))
        with patch.object(router, "_fish_key", return_value="fish-secret"), \
                patch.object(router.shutil, "which", return_value="ffmpeg"), \
                patch.object(router.subprocess, "Popen", return_value=process), \
                patch.object(router, "_fish_http_client", return_value=client):
            chunks = list(router.stream_fish_tts("Hello Ayaan"))
        self.assertEqual(chunks[0][0], 24000)
        np.testing.assert_array_equal(
            chunks[0][1], np.array([-20, 0, 20], dtype=np.int16))
        method, url, request = client.request
        self.assertEqual((method, url), ("POST", router._FISH_URL))
        self.assertEqual(request["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(request["headers"]["Authorization"],
                         "Bearer fish-secret")
        self.assertEqual(request["json"], {
            "text": "Hello Ayaan", "format": "mp3",
            "reference_id": "voice-reference",
            "temperature": 0.2,
            "top_p": 0.5,
            "latency": "balanced",
            "chunk_length": 200,
            "normalize": True,
            "condition_on_previous_chunks": True,
            "prosody": {
                "speed": 1.0,
                "volume": 0,
                "normalize_loudness": True,
            },
        })
        self.assertNotIn("fish-secret", str(request["json"]))
        router.log.assert_called_once()
        self.assertNotIn("fish-secret", str(router.log.call_args))

    def test_fish_tts_failure_opens_only_fish_circuit(self):
        router = load_router(
            {"speech": {"tts_provider": "fish",
                         "tts_fallback_providers": ["gemini"],
                         "fish_reference_id": "voice-reference"}},
            {"FISH_API_KEY": "fish-secret", "GEMINI_API_KEY": "g-key"},
        )
        process = FakeProcess()
        client = FakeClient(FishResponse([], status=429))
        with patch.object(router, "_fish_key", return_value="fish-secret"), \
                patch.object(router.shutil, "which", return_value="ffmpeg"), \
                patch.object(router.subprocess, "Popen", return_value=process), \
                patch.object(router, "_fish_http_client", return_value=client):
            with self.assertRaises(RuntimeError):
                list(router.stream_fish_tts("Hello"))
        self.assertFalse(router._fish_tts_circuit.available())
        self.assertTrue(router._tts_circuit.available())

    def test_fish_requires_a_fixed_reference_voice(self):
        router = load_router(
            {"speech": {"tts_provider": "fish", "fish_reference_id": ""}},
            {"FISH_API_KEY": "fish-secret"},
        )
        with patch.object(router, "_fish_key", return_value="fish-secret"):
            self.assertFalse(router.fish_tts_enabled())
            self.assertEqual(
                router.fish_voice_status(),
                (False, "reference-voice-missing"),
            )

    def test_fish_sampling_and_latency_settings_are_bounded(self):
        cfg = {"speech": {
            "tts_provider": "fish",
            "fish_reference_id": "fixed-voice",
            "fish_temperature": 9,
            "fish_top_p": -2,
            "fish_latency": "unsupported",
            "fish_chunk_length": 999,
            "fish_speed": 10,
        }}
        router = load_router(cfg, {"FISH_API_KEY": "fish-secret"})
        process = FakeProcess(struct.pack("<h", 1))
        client = FakeClient(FishResponse([b"fake-mp3"]))
        with patch.object(router, "_fish_key", return_value="fish-secret"), \
                patch.object(router.shutil, "which", return_value="ffmpeg"), \
                patch.object(router.subprocess, "Popen", return_value=process), \
                patch.object(router, "_fish_http_client", return_value=client):
            list(router.stream_fish_tts("Consistent voice"))
        payload = client.request[2]["json"]
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 0.0)
        self.assertEqual(payload["latency"], "balanced")
        self.assertEqual(payload["chunk_length"], 300)
        self.assertEqual(payload["prosody"]["speed"], 2.0)

    def test_wav_encoding_is_mono_16_bit_at_requested_rate(self):
        router = load_router()
        encoded = router._wav_bytes(np.array([-2, 0, 2], dtype=np.int16), 16000)
        with wave.open(io.BytesIO(encoded), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.readframes(3), struct.pack("<hhh", -2, 0, 2))

    def test_transcription_uses_auto_language_and_custom_vocabulary(self):
        cfg = {"speech": {"custom_vocabulary": ["Jarvis", "Ayaan"]}}
        router = load_router(cfg, {"GEMINI_API_KEY": "test-key"})
        body = {"steps": [{"content": [
            {"type": "text", "text": "Open WhatsApp, Jarvis."}]}]}
        with patch.object(router, "_key", return_value="test-key"), \
                patch.object(router.httpx, "post", return_value=Response(body)) as post:
            text = router.try_gemini_transcribe(np.zeros(1600, np.int16))
        self.assertEqual(text, "Open WhatsApp, Jarvis.")
        request = post.call_args.kwargs
        self.assertNotIn("test-key", str(request["json"]))
        config = request["json"]["generation_config"]["transcription_config"]
        self.assertEqual(config["language_codes"], [])
        self.assertEqual(config["custom_vocabulary"], ["Jarvis", "Ayaan"])
        audio = request["json"]["input"][0]
        self.assertTrue(base64.b64decode(audio["data"]).startswith(b"RIFF"))

    def test_transcription_failure_opens_circuit_and_uses_fallback(self):
        router = load_router(env={"GEMINI_API_KEY": "test-key"})
        with patch.object(router, "_key", return_value="test-key"), \
                patch.object(router.httpx, "post",
                             side_effect=RuntimeError("offline")) as post:
            self.assertIsNone(router.try_gemini_transcribe(
                np.zeros(1600, np.int16)))
            self.assertIsNone(router.try_gemini_transcribe(
                np.zeros(1600, np.int16)))
        self.assertEqual(post.call_count, 1)

    def test_tts_stream_decodes_audio_delta(self):
        router = load_router(env={"GEMINI_API_KEY": "test-key"})
        pcm = struct.pack("<hhh", -10, 0, 10)
        event = {"event_type": "step.delta", "delta": {
            "type": "audio", "sample_rate": 24000,
            "data": base64.b64encode(pcm).decode()}}
        stream = StreamResponse(["event: step.delta",
                                 "data: " + __import__("json").dumps(event),
                                 "data: [DONE]"])
        with patch.object(router, "_key", return_value="test-key"), \
                patch.object(router.httpx, "stream", return_value=stream):
            chunks = list(router.stream_gemini_tts("Hello"))
        self.assertEqual(chunks[0][0], 24000)
        np.testing.assert_array_equal(
            chunks[0][1], np.array([-10, 0, 10], dtype=np.int16))

    def test_tts_stream_surfaces_in_band_quota_error_and_opens_circuit(self):
        router = load_router(env={"GEMINI_API_KEY": "test-key"})
        event = {"event_type": "error", "error": {
            "code": "quota_exceeded", "message": "retry later"}}
        stream = StreamResponse(["data: " + __import__("json").dumps(event)])
        with patch.object(router, "_key", return_value="test-key"), \
                patch.object(router.httpx, "stream", return_value=stream):
            with self.assertRaisesRegex(RuntimeError, "retry later"):
                list(router.stream_gemini_tts("Hello"))
        self.assertFalse(router._tts_circuit.available())
        router.log.assert_called_once()

    def test_urdu_script_detection_does_not_restrict_other_languages(self):
        router = load_router()
        self.assertTrue(router.contains_urdu_script("میں تیار ہوں"))
        self.assertFalse(router.contains_urdu_script("main tayyar hoon"))
        self.assertTrue(router.contains_urdu_script("مرحبا"))


if __name__ == "__main__":
    unittest.main()
