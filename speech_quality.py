"""Reject pathological transcription loops without restricting languages."""
import re


_SUPPORTED_NATIVE_SCRIPT = re.compile(r"[\u0600-\u08ff\u0900-\u097f]")


def romanize_supported_speech(text: str) -> str:
    """Turn Gemini's Urdu/Devanagari transcript into fast local Latin text."""
    value = str(text or "").strip()
    if not _SUPPORTED_NATIVE_SCRIPT.search(value):
        return value
    from unidecode import unidecode
    value = unidecode(value)
    return re.sub(r"\s+([,.!?;:])", r"\1", re.sub(r"\s+", " ", value)).strip()


def transcript_problem(text: str) -> str | None:
    text = text.strip()
    if not text:
        return 'empty'
    if '\ufffd' in text:
        return 'invalid text encoding'
    # Observed decoder loops repeat one letter/syllable or a short phrase.
    if re.search(r'(\S{1,12})\1{11,}', text):
        return 'repeated character sequence'
    words = text.casefold().split()
    for size in range(1, 5):
        for offset in range(len(words) - size * 8 + 1):
            block = words[offset:offset + size]
            if all(words[offset + n*size:offset + (n+1)*size] == block for n in range(1, 8)):
                return 'repeated phrase'
    return None
