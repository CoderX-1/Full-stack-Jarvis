"""Bounded, transcript-free diagnostics from the most recent Backtalk log session."""

from __future__ import annotations

from pathlib import Path
import re


_PREFIX = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d "
_SESSION = re.compile(_PREFIX + r"\[backtalk\] up\b")
_EARS = re.compile(_PREFIX + r"\[ears\] ")
_MOUTH = re.compile(_PREFIX + r"\[mouth\] ")
_STT_OK = re.compile(_PREFIX + r"\[ears\].*\bSTT ok\b", re.IGNORECASE)
_TTS_OK = re.compile(_PREFIX + r"\[mouth\].*\b(?:Fish|Gemini) TTS ok\b", re.IGNORECASE)
_STT_POST = re.compile(r"\bpost_release_ms=(\d{1,7})\b")
_STT_TOTAL = re.compile(r"\btotal_ms=(\d{1,7})\b")
_TTS_FIRST = re.compile(r"\bfirst_audio_ms=(\d{1,7})\b")
_TTS_TOTAL = re.compile(r"\btotal_ms=(\d{1,7})\b")


def _nearest_rank(values: list[int], percentile: int) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return str(ordered[min(rank, len(ordered)) - 1])


def _metric(lines: list[str], pattern: re.Pattern[str]) -> list[int]:
    values = []
    for line in lines:
        match = pattern.search(line)
        if match:
            values.append(int(match.group(1)))
    return values


def summarize_voice_log(path: Path) -> str:
    """Read at most the final 2 MiB; never return raw log lines or transcripts."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 2 * 1024 * 1024))
            raw = stream.read(2 * 1024 * 1024)
    except OSError:
        return "Voice health unavailable: no readable Backtalk log; no live audio test performed."
    lines = raw.decode("utf-8", errors="replace").splitlines()
    starts = [index for index, line in enumerate(lines) if _SESSION.search(line)]
    if not starts:
        return "Voice health unavailable: no completed session marker in bounded log; no live audio test performed."
    session = lines[starts[-1]:]
    stt = [line for line in session if _STT_OK.search(line)]
    tts = [line for line in session if _TTS_OK.search(line)]
    stt_post = _metric(stt, _STT_POST)
    stt_total = _metric(stt, _STT_TOTAL)
    tts_first = _metric(tts, _TTS_FIRST)
    tts_total = _metric(tts, _TTS_TOTAL)
    stt_fallback = sum(
        bool(_EARS.search(line)) and (
            "transcription unavailable" in line.casefold()
            or "using local whisper" in line.casefold()
        )
        for line in session
    )
    tts_errors = sum(
        bool(_MOUTH.search(line)) and (
            "synth/play error" in line or "cloud TTS route exhausted" in line
        )
        for line in session
    )
    return (
        "Voice health: historical last-session log only; not a live microphone or speaker check | "
        f"stt_success={len(stt)} | stt_fallback_events={stt_fallback} | "
        f"stt_post_release_ms_p50/p95={_nearest_rank(stt_post, 50)}/{_nearest_rank(stt_post, 95)} "
        f"(n={len(stt_post)}) | "
        f"stt_total_ms_p50/p95={_nearest_rank(stt_total, 50)}/{_nearest_rank(stt_total, 95)} "
        f"(n={len(stt_total)}) | "
        f"tts_success={len(tts)} | tts_error_events={tts_errors} | "
        f"tts_first_audio_ms_p50/p95={_nearest_rank(tts_first, 50)}/{_nearest_rank(tts_first, 95)} "
        f"(n={len(tts_first)}) | "
        f"tts_total_ms_p50/p95={_nearest_rank(tts_total, 50)}/{_nearest_rank(tts_total, 95)} "
        f"(n={len(tts_total)}) | transcript_exposed=false"
    )
