"""Cloud speech routing for JARVIS.

Fish Audio is the primary TTS path. Gemini remains an independent cloud
fallback, while local speech is an explicit policy choice owned by mouth.py.
No credential or reply text is ever written to the log.
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import wave
from dataclasses import dataclass

import httpx
import numpy as np

from backtalk.config import CFG
from backtalk.vlog import log

_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
_FISH_URL = "https://api.fish.audio/v1/tts"
_REVISION = "2026-05-20"
_FISH_RATE = 24000


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
_stt_live_circuit = _Circuit("stt_live")
_tts_circuit = _Circuit("tts")
_fish_tts_circuit = _Circuit("fish_tts")
_fish_client_lock = threading.Lock()
_fish_client = None
_urdu_lock = threading.Lock()
_urdu_tokenizer = None
_urdu_model = None


def _speech() -> dict:
    return CFG.get("speech") or {}


def _key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or
            os.environ.get("GOOGLE_API_KEY") or "").strip()


def _fish_key() -> str:
    """Read Fish credentials from the environment only, never config."""
    return (os.environ.get("FISH_API_KEY") or
            os.environ.get("FISH_AUDIO_API_KEY") or "").strip()


def _fish_reference_id() -> str:
    return str(_speech().get("fish_reference_id") or "").strip()


def fish_voice_status() -> tuple[bool, str]:
    """Return readiness without logging a credential or the full voice ID."""
    if not _fish_key():
        return False, "api-key-missing"
    reference_id = _fish_reference_id()
    if not reference_id:
        return False, "reference-voice-missing"
    if not (8 <= len(reference_id) <= 128) or not all(
        ch.isalnum() or ch in "_-" for ch in reference_id
    ):
        return False, "reference-voice-invalid"
    return True, f"voice={reference_id[:8]}"


def _provider(kind: str) -> str:
    return str(_speech().get(f"{kind}_provider") or "gemini").lower()


def gemini_stt_enabled() -> bool:
    return bool(_key() and _provider("stt") in {"auto", "gemini"})


def gemini_live_stt_enabled() -> bool:
    enabled = _speech().get("stt_live_enabled", True)
    return bool(gemini_stt_enabled() and enabled and _stt_live_circuit.available())


def gemini_tts_enabled() -> bool:
    return bool(_key() and "gemini" in tts_provider_order())


def fish_tts_enabled() -> bool:
    ready, _ = fish_voice_status()
    return bool(ready and "fish" in tts_provider_order())


def tts_provider_order() -> tuple[str, ...]:
    """Return a validated, de-duplicated cloud TTS failover route."""
    speech = _speech()
    primary = str(speech.get("tts_provider") or "fish").strip().lower()
    raw_fallbacks = speech.get("tts_fallback_providers", ["gemini"])
    if isinstance(raw_fallbacks, str):
        raw_fallbacks = [raw_fallbacks]
    requested = (["fish", "gemini"] if primary == "auto" else [primary])
    requested.extend(raw_fallbacks or [])
    result = []
    for item in requested:
        provider = str(item).strip().lower()
        if provider in {"fish", "gemini"} and provider not in result:
            result.append(provider)
    return tuple(result or ("fish", "gemini"))


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


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(_speech().get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _fish_http_client():
    """One pooled client avoids a new TLS handshake for every sentence."""
    global _fish_client
    with _fish_client_lock:
        if _fish_client is None:
            _fish_client = httpx.Client(
                limits=httpx.Limits(max_connections=4,
                                    max_keepalive_connections=2,
                                    keepalive_expiry=60.0),
                follow_redirects=False,
            )
    return _fish_client


_ROMAN_URDU_STRONG = {
    "acha", "achha", "achhi", "batao", "dekho", "hoon", "hun",
    "kaise", "karo", "karna", "kya", "kyun", "lekin", "mujhe",
    "nahi", "nahin", "pasand", "samjhe", "shukriya", "tumhe",
    "tumhein", "yaar",
}
_ROMAN_URDU_COMMON = {
    "aaj", "agar", "aur", "bohat", "ek", "hai", "hain", "hota",
    "hoti", "ka", "ke", "ki", "ko", "main", "mein", "mera",
    "meri", "sab", "se", "theek", "ye", "woh",
}


def looks_like_roman_urdu(text: str) -> bool:
    """Conservative language hint: avoid changing ordinary English delivery."""
    words = re.findall(r"[a-z']+", str(text).casefold())
    strong = sum(word in _ROMAN_URDU_STRONG for word in words)
    common = sum(word in _ROMAN_URDU_COMMON for word in words)
    return strong >= 2 or (strong >= 1 and common >= 2) or common >= 5


def _fish_delivery(text: str, speech: dict) -> tuple[str, str, float, bool]:
    """Return private synthesis text and quality settings; display text is untouched."""
    roman_urdu = bool(speech.get("fish_roman_urdu_accent", True)
                      and looks_like_roman_urdu(text))
    latency = str(speech.get(
        "fish_roman_urdu_latency" if roman_urdu else "fish_latency",
        "balanced" if roman_urdu else "low",
    )).strip().lower()
    if latency not in {"low", "balanced", "normal"}:
        latency = "balanced"
    speed_key = "fish_roman_urdu_speed" if roman_urdu else "fish_speed"
    speed = _bounded_float(speed_key, 1.0 if roman_urdu else 1.05, 0.5, 2.0)
    if not roman_urdu:
        return text, latency, speed, False
    direction = str(speech.get("fish_roman_urdu_direction") or
                    "speaking natural Pakistani Urdu with a warm native accent")
    return f"[{direction}] {text}", latency, speed, True


def stream_fish_tts(text: str):
    """Yield decoded Fish Audio MP3 as mono int16 PCM.

    The HTTP body is fed into ffmpeg while it arrives, so playback can begin
    before the complete response has downloaded. The generator exposes only
    decoded PCM; callers never need temporary files and cancellation can tear
    down the decoder immediately.
    """
    if not fish_tts_enabled() or not _fish_tts_circuit.available():
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for Fish Audio MP3 decoding")

    speech = _speech()
    model = str(speech.get("fish_model") or "s2.1-pro-free")
    reference_id = _fish_reference_id()
    if not reference_id:
        raise RuntimeError("Fish Audio reference voice is not configured")
    delivery_text, latency, delivery_speed, roman_urdu = \
        _fish_delivery(text, speech)
    try:
        chunk_length = max(100, min(300, int(speech.get("fish_chunk_length") or 200)))
    except (TypeError, ValueError):
        chunk_length = 200
    payload = {
        "text": delivery_text,
        "format": "mp3",
        "reference_id": reference_id,
        "temperature": _bounded_float("fish_temperature", 0.2, 0.0, 1.0),
        "top_p": _bounded_float("fish_top_p", 0.5, 0.0, 1.0),
        "latency": latency,
        "chunk_length": chunk_length,
        "normalize": True,
        "condition_on_previous_chunks": True,
        "prosody": {
            "speed": delivery_speed,
            "volume": 0,
            "normalize_loudness": True,
        },
    }
    headers = {
        "Authorization": f"Bearer {_fish_key()}",
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
        "model": model,
    }
    timeout_seconds = _timeout("fish_tts", 12.0)
    proc = subprocess.Popen(
        [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-f", "s16le",
         "-ar", str(_FISH_RATE), "-ac", "1", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=(subprocess.CREATE_NO_WINDOW
                       if os.name == "nt" else 0),
    )
    feed_error: list[Exception] = []
    response_status: list[int | None] = [None]
    encoded_bytes = [0]
    started = time.monotonic()

    def _feed() -> None:
        try:
            with _fish_http_client().stream(
                    "POST", _FISH_URL, headers=headers, json=payload,
                    timeout=timeout_seconds) as response:
                response_status[0] = response.status_code
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if chunk:
                        encoded_bytes[0] += len(chunk)
                        proc.stdin.write(chunk)
        except Exception as exc:
            feed_error.append(exc)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    feeder = threading.Thread(target=_feed, name="fish-tts-feed", daemon=True)
    feeder.start()
    got_audio = False
    first_audio_ms = None
    try:
        carry = b""
        while True:
            data = proc.stdout.read(4800)
            if not data:
                break
            data = carry + data
            usable = len(data) - (len(data) % 2)
            carry = data[usable:]
            if usable:
                if not got_audio:
                    first_audio_ms = round((time.monotonic() - started) * 1000)
                got_audio = True
                yield _FISH_RATE, np.frombuffer(
                    data[:usable], dtype="<i2").copy()
        feeder.join(timeout=1.0)
        proc.wait(timeout=5.0)
        if feed_error:
            raise feed_error[0]
        if not got_audio:
            raise RuntimeError("Fish Audio returned no decodable speech")
        _fish_tts_circuit.reset()
        total_ms = round((time.monotonic() - started) * 1000)
        log(f"[mouth] Fish TTS ok first_audio_ms={first_audio_ms} "
            f"total_ms={total_ms} encoded_bytes={encoded_bytes[0]} "
            f"model={model} voice={reference_id[:8]} "
            f"latency={latency} roman_urdu={str(roman_urdu).lower()}")
    except GeneratorExit:
        proc.kill()
        raise
    except Exception as exc:
        status = response_status[0]
        _fish_tts_circuit.trip(quota=status == 429)
        log(f"[mouth] Fish TTS unavailable "
            f"(status={status or 'none'}; {type(exc).__name__}: "
            f"{str(exc)[:100]})")
        try:
            proc.kill()
        except Exception:
            pass
        raise


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


def _merge_transcript(current: str, incoming: str) -> str:
    """Merge Live API transcript events whether they are cumulative or incremental."""
    current, incoming = current.strip(), incoming.strip()
    if not incoming:
        return current
    if not current or incoming.startswith(current):
        return incoming
    if current.startswith(incoming):
        return current
    return f"{current} {incoming}".strip()


class GeminiLiveTranscriber:
    """Stream PTT audio while it is captured, leaving almost no release wait."""

    def __init__(self, rate: int = 16000):
        self.rate = rate
        self._chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=640)
        self._pending = bytearray()
        self._cancelled = threading.Event()
        self._done = threading.Event()
        self._text = ""
        self._error: Exception | None = None
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="gemini-live-stt")
        self._thread.start()

    def _put(self, data: bytes | None) -> None:
        if self._done.is_set() or self._cancelled.is_set():
            return
        try:
            self._chunks.put_nowait(data)
        except queue.Full as exc:
            self._error = RuntimeError("Gemini Live audio queue overflow")
            self._cancelled.set()
            raise self._error from exc

    def feed(self, pcm: np.ndarray) -> None:
        if self._done.is_set() or self._cancelled.is_set():
            return
        self._pending.extend(np.asarray(pcm, dtype="<i2").reshape(-1).tobytes())
        chunk_bytes = max(2, int(self.rate * 0.1) * 2)
        while len(self._pending) >= chunk_bytes:
            data = bytes(self._pending[:chunk_bytes])
            del self._pending[:chunk_bytes]
            self._put(data)

    def finish(self) -> str | None:
        released = time.monotonic()
        if self._pending:
            self._put(bytes(self._pending))
            self._pending.clear()
        self._put(None)
        timeout = _bounded_float("stt_live_finish_timeout_s", 3.0, 0.5, 8.0)
        self._done.wait(timeout)
        if not self._done.is_set():
            self._cancelled.set()
            log("[ears] Gemini Live final transcript timed out; trying batch Gemini")
            return None
        if self._error or not self._text.strip():
            return None
        _stt_live_circuit.reset()
        log(f"[ears] Gemini Live STT ok post_release_ms="
            f"{round((time.monotonic() - released) * 1000)} "
            f"total_ms={round((time.monotonic() - self._started) * 1000)}")
        return self._text.strip()

    def cancel(self) -> None:
        self._cancelled.set()
        try:
            self._chunks.put_nowait(None)
        except queue.Full:
            pass

    def _run(self) -> None:
        websocket = None
        try:
            from websockets.sync.client import connect
            key = urllib.parse.quote(_key(), safe="")
            url = f"{_LIVE_URL}?key={key}"
            connect_timeout = _bounded_float(
                "stt_live_connect_timeout_s", 4.0, 1.0, 10.0)
            websocket = connect(url, open_timeout=connect_timeout,
                                close_timeout=1.0, max_size=2**20)
            speech = _speech()
            transcription = {
                "mode": str(speech.get("stt_mode") or "verbatim").upper(),
                "languageCodes": list(speech.get("language_codes") or []),
            }
            vocabulary = [str(x).strip()
                          for x in speech.get("custom_vocabulary") or []
                          if str(x).strip()]
            if vocabulary:
                transcription["customVocabulary"] = vocabulary[:100]
            setup = {
                "setup": {
                    "model": "models/" + str(speech.get(
                        "gemini_stt_live_model") or
                        "gemini-3.5-transcribe-live"),
                    "generationConfig": {"responseModalities": ["TEXT"]},
                    "realtimeInputConfig": {
                        "automaticActivityDetection": {"disabled": True}},
                    "inputAudioTranscription": transcription,
                }
            }
            websocket.send(json.dumps(setup))
            ack = json.loads(websocket.recv(timeout=connect_timeout))
            if "setupComplete" not in ack:
                raise RuntimeError("Gemini Live setup was not acknowledged")
            websocket.send(json.dumps(
                {"realtimeInput": {"activityStart": {}}}))
            while not self._cancelled.is_set():
                chunk = self._chunks.get()
                if chunk is None:
                    break
                websocket.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "mimeType": f"audio/pcm;rate={self.rate}",
                }}}))
            if self._cancelled.is_set():
                return
            websocket.send(json.dumps(
                {"realtimeInput": {"activityEnd": {}}}))
            deadline = time.monotonic() + _bounded_float(
                "stt_live_finish_timeout_s", 3.0, 0.5, 8.0)
            while not self._cancelled.is_set() and time.monotonic() < deadline:
                try:
                    event = json.loads(websocket.recv(
                        timeout=max(0.05, deadline - time.monotonic())))
                except TimeoutError:
                    break
                content = event.get("serverContent") or {}
                interim = content.get("interimInputTranscription") or {}
                final = content.get("inputTranscription") or {}
                if interim.get("text"):
                    self._text = _merge_transcript(self._text, interim["text"])
                if final.get("text"):
                    # The final event is authoritative and may revise wording
                    # or casing from an interim hypothesis.
                    self._text = str(final["text"]).strip()
                    break
                if content.get("turnComplete"):
                    break
        except Exception as exc:
            self._error = exc
            _stt_live_circuit.trip()
            log(f"[ears] Gemini Live transcription unavailable "
                f"({type(exc).__name__}); trying batch Gemini")
        finally:
            if websocket is not None:
                try:
                    websocket.close()
                except Exception:
                    pass
            self._done.set()


def start_gemini_live_transcriber(rate: int = 16000):
    if not gemini_live_stt_enabled():
        return None
    return GeminiLiveTranscriber(rate)


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
