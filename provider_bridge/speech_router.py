"""Free-first multilingual speech with automatic local fallbacks."""

from __future__ import annotations

import base64
import io
import json
import os
import threading
import time
import wave
from dataclasses import dataclass

import httpx
import numpy as np

from backtalk.config import CFG
from backtalk.vlog import log

_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_REVISION = "2026-05-20"


@dataclass
class _Circuit:
    kind: str
    retry_at: float = 0.0

    def available(self) -> bool:
        return time.monotonic() >= self.retry_at

    def trip(self, quota: bool = False) -> None:
        try:
            speech = _speech()
            delay = max(10.0, float(speech.get(
                f"{self.kind}_failure_cooldown_s",
                speech.get("failure_cooldown_s", 90),
            )))
            if quota:
                delay = max(delay, float(speech.get(f"{self.kind}_quota_cooldown_s", 900)))
        except (TypeError, ValueError):
            delay = 90.0
        self.retry_at = time.monotonic() + delay

    def reset(self) -> None:
        self.retry_at = 0.0


_stt_circuit = _Circuit("stt")
_tts_circuit = _Circuit("tts")
_urdu_lock = threading.Lock()
_urdu_tokenizer = None
_urdu_model = None


def _speech() -> dict:
    return CFG.get("speech") or {}


def _key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or
            os.environ.get("GOOGLE_API_KEY") or "").strip()


def _provider(kind: str) -> str:
    return str(_speech().get(f"{kind}_provider") or "gemini").lower()


def gemini_stt_enabled() -> bool:
    return bool(_key() and _provider("stt") in {"auto", "gemini"})


def gemini_tts_enabled() -> bool:
    return bool(_key() and _provider("tts") in {"auto", "gemini"})


def _headers(stream: bool = False) -> dict[str, str]:
    headers = {"x-goog-api-key": _key(), "Content-Type": "application/json",
               "Api-Revision": _REVISION}
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def _timeout(kind: str, default: float) -> float:
    try:
        return max(5.0, float(_speech().get(f"{kind}_timeout_s", default)))
    except (TypeError, ValueError):
        return default


def _wav_bytes(pcm: np.ndarray, rate: int) -> bytes:
    data = np.asarray(pcm, dtype=np.int16).reshape(-1)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data.astype("<i2", copy=False).tobytes())
    return out.getvalue()


def _output_text(response: dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"].strip()
    return "".join(str(part.get("text") or "")
                   for step in response.get("steps") or []
                   for part in step.get("content") or []
                   if part.get("type") == "text").strip()


def try_gemini_transcribe(pcm: np.ndarray, rate: int = 16000) -> str | None:
    """Return an auto-detected multilingual transcript, or None for fallback."""
    if not gemini_stt_enabled() or not _stt_circuit.available():
        return None
    speech = _speech()
    transcription = {
        "mode": str(speech.get("stt_mode") or "verbatim").lower(),
        "language_codes": list(speech.get("language_codes") or []),
    }
    vocabulary = [str(x).strip() for x in speech.get("custom_vocabulary") or []
                  if str(x).strip()]
    if vocabulary:
        transcription["custom_vocabulary"] = vocabulary[:100]
    payload = {
        "model": str(speech.get("gemini_stt_model") or
                     "gemini-3.5-transcribe"),
        "input": [{"type": "audio",
                   "data": base64.b64encode(_wav_bytes(pcm, rate)).decode(),
                   "mime_type": "audio/wav"}],
        "generation_config": {"transcription_config": transcription},
    }
    try:
        response = httpx.post(_URL, headers=_headers(), json=payload,
                              timeout=_timeout("stt", 30.0))
        response.raise_for_status()
        text = _output_text(response.json())
        if not text:
            raise RuntimeError("Gemini returned no transcript")
        _stt_circuit.reset()
        return text
    except Exception as exc:
        response = getattr(exc, 'response', None)
        _stt_circuit.trip(quota=getattr(response, 'status_code', None) == 429)
        log(f"[ears] Gemini transcription unavailable ({str(exc)[:100]}) -- "
            "using local Whisper")
        return None


def _tts_prompt(text: str) -> str:
    style = str(_speech().get("tts_style") or
                "calm, polished, intelligent assistant delivery")
    return ("Speak the following text exactly as written. Detect its language "
            "automatically, pronounce code-switching naturally, and do not "
            f"translate, add, or omit words. Use a {style}.\n\n{text}")


def stream_gemini_tts(text: str):
    """Yield streaming (rate, int16 PCM) chunks from Gemini TTS."""
    if not gemini_tts_enabled() or not _tts_circuit.available():
        return
    speech = _speech()
    payload = {
        "model": str(speech.get("gemini_tts_model") or
                     "gemini-3.1-flash-tts-preview"),
        "input": _tts_prompt(text),
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{
            "voice": str(speech.get("gemini_voice") or "Charon")}]},
        "stream": True,
    }
    got_audio = False
    pending = b""
    try:
        timeout_seconds = _timeout("tts", 45.0)
        deadline = time.monotonic() + timeout_seconds
        with httpx.stream("POST", _URL, headers=_headers(stream=True),
                          json=payload, timeout=timeout_seconds) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                # A streaming server can keep a socket alive indefinitely
                # with non-audio events, continually resetting the library's
                # per-read timeout. Enforce a total request deadline so local
                # speech fallback cannot be starved at startup.
                if time.monotonic() >= deadline:
                    raise TimeoutError("Gemini TTS exceeded its total response deadline")
                if not line.startswith("data: "):
                    continue
                body = line[6:]
                if body == "[DONE]":
                    continue
                event = json.loads(body)
                if event.get("event_type") == "error":
                    error = event.get("error") or {}
                    raise RuntimeError(str(error.get("message") or error))
                delta = event.get("delta") or {}
                if delta.get("type") != "audio" or not delta.get("data"):
                    continue
                raw = pending + base64.b64decode(delta["data"], validate=True)
                aligned = len(raw) - len(raw) % 2
                pending, raw = raw[aligned:], raw[:aligned]
                if raw:
                    got_audio = True
                    _tts_circuit.reset()
                    yield int(delta.get("sample_rate") or 24000), \
                        np.frombuffer(raw, dtype="<i2").copy()
        if pending:
            raise RuntimeError("Gemini returned incomplete PCM audio")
        if not got_audio:
            raise RuntimeError("Gemini returned no speech audio")
    except GeneratorExit:
        raise
    except Exception as exc:
        _tts_circuit.trip()
        log(f"[mouth] Gemini speech unavailable ({str(exc)[:100]}) -- "
            "using a local voice")
        raise


def contains_urdu_script(text: str) -> bool:
    return any("\u0600" <= char <= "\u06ff" or
               "\u0750" <= char <= "\u077f" or
               "\u08a0" <= char <= "\u08ff" for char in text)


def _load_urdu_voice():
    global _urdu_tokenizer, _urdu_model
    with _urdu_lock:
        if _urdu_model is None:
            from transformers import AutoTokenizer, VitsModel
            model_id = str(_speech().get("urdu_fallback_model") or
                           "facebook/mms-tts-urd-script_arabic")
            log(f"[mouth] loading local Urdu fallback ({model_id})...")
            _urdu_tokenizer = AutoTokenizer.from_pretrained(model_id)
            _urdu_model = VitsModel.from_pretrained(model_id)
            _urdu_model.eval()
            log("[mouth] local Urdu fallback ready")
    return _urdu_tokenizer, _urdu_model


def stream_local_urdu(text: str, chunk_samples: int = 4800):
    """Yield local Urdu speech, loading the fallback lazily."""
    import torch
    tokenizer, model = _load_urdu_voice()
    inputs = tokenizer(text, return_tensors="pt")
    with torch.no_grad():
        audio = model(**inputs).waveform[0].detach().cpu().numpy()
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    rate = int(getattr(model.config, "sampling_rate", 16000))
    for start in range(0, len(pcm), chunk_samples):
        yield rate, pcm[start:start + chunk_samples]
