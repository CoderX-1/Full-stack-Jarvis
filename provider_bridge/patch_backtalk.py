#!/usr/bin/env python3
"""Apply the Gemini/OpenAI compatibility overlay to a Backtalk checkout."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count == 0 and new in text:
        return text
    if count != 1:
        raise RuntimeError(f"Could not patch {label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def insert_before_once(text: str, anchor: str, addition: str,
                       marker: str, label: str) -> str:
    if marker in text:
        return text
    return replace_once(text, anchor, addition + anchor, label)


def patch(target: Path) -> None:
    target = target.resolve()
    package = target / "backtalk"
    pyproject = target / "pyproject.toml"
    main_file = package / "main.py"
    config_file = package / "config.py"
    ears_file = package / "ears.py"
    mouth_file = package / "mouth.py"
    for required in (package / "brain.py", pyproject, main_file, config_file,
                     ears_file, mouth_file):
        if not required.is_file():
            raise RuntimeError(f"Not a Backtalk checkout: missing {required}")

    source_brain = Path(__file__).resolve().parent / "brain.py"
    shutil.copyfile(source_brain, package / "brain.py")
    source_speech = Path(__file__).resolve().parent / "speech_router.py"
    shutil.copyfile(source_speech, package / "speech_router.py")

    project = pyproject.read_text(encoding="utf-8")
    project = "\n".join(
        line for line in project.splitlines() if "claude-agent-sdk" not in line
    ) + "\n"
    if '"transformers>=4.45.0",' not in project:
        project = replace_once(
            project, "dependencies = [\n",
            'dependencies = [\n    "transformers>=4.45.0",\n',
            "local Urdu speech dependency",
        )
    pyproject.write_text(project, encoding="utf-8")

    main = main_file.read_text(encoding="utf-8")
    main = replace_once(
        main,
        "    from claude_agent_sdk import (PermissionResultAllow,\n                                  PermissionResultDeny)",
        "    from backtalk.brain import (PermissionResultAllow,\n                                PermissionResultDeny)",
        "permission result import",
    )
    main = main.replace(
        "couldn't reach my brain, the Claude Code session.",
        "couldn't reach my configured AI provider.",
    ).replace(
        "Claude Code isn't signed in",
        "the API key is missing",
    ).replace(
        "or the plan is out of usage.",
        "or the account is out of API credit.",
    )
    main_file.write_text(main, encoding="utf-8")

    config = config_file.read_text(encoding="utf-8")
    config = config.replace('    "model": "claude-sonnet-5",', '    "model": "",')
    config = config.replace('    "deep_model": "claude-opus-5",', '    "deep_model": "",')
    anchor = '    "name": "Assistant",\n'
    addition = (
        anchor
        + '    # Model API. Keys stay in GEMINI_API_KEY, OPENAI_API_KEY, or AI_API_KEY.\n'
        + '    "provider": "openai",\n'
        + '    "base_url": "",\n'
    )
    if '    "provider": "openai",' not in config:
        config = replace_once(config, anchor, addition, "provider defaults")
    speech_anchor = '    "base_url": "",\n'
    speech_defaults = (
        speech_anchor
        + '    # Free-first multilingual speech with automatic local fallbacks.\n'
        + '    "speech": {\n'
        + '        "stt_provider": "gemini",\n'
        + '        "tts_provider": "gemini",\n'
        + '        "gemini_stt_model": "gemini-3.5-transcribe",\n'
        + '        "gemini_tts_model": "gemini-3.1-flash-tts-preview",\n'
        + '        "gemini_voice": "Charon",\n'
        + '        "stt_mode": "verbatim",\n'
        + '        "language_codes": [],\n'
        + '        "custom_vocabulary": ["Jarvis", "Ayaan", "OpenAI", "Gemini",\n'
        + '                              "WhatsApp", "YouTube", "Spotify"],\n'
        + '        "stt_timeout_s": 30,\n'
        + '        "tts_timeout_s": 45,\n'
        + '        "failure_cooldown_s": 90,\n'
        + '        "stt_failure_cooldown_s": 120,\n'
        + '        "tts_failure_cooldown_s": 3600,\n'
        + '        "tts_style": "calm, polished, intelligent assistant delivery",\n'
        + '        "urdu_fallback_model": "facebook/mms-tts-urd-script_arabic",\n'
        + '    },\n'
    )
    if '    "gemini_stt_model": "gemini-3.5-transcribe",' not in config:
        config = replace_once(config, speech_anchor, speech_defaults,
                              "speech defaults")
    config_file.write_text(config, encoding="utf-8")

    ears = ears_file.read_text(encoding="utf-8")
    ears_wrapper = '''def warm():
    """Prepare the selected speech path without loading fallback models."""
    check_microphone()
    from backtalk.speech_router import gemini_stt_enabled
    if gemini_stt_enabled():
        log("[ears] Gemini multilingual transcription ready; local Whisper standby")
        return None
    return _warm_local()


def transcribe(pcm: np.ndarray) -> str:
    """Gemini multilingual STT first; local Whisper on any cloud failure."""
    from backtalk.speech_router import try_gemini_transcribe
    text = try_gemini_transcribe(pcm, RATE)
    if text is None:
        text = _transcribe_local(pcm)
    return _NONSPEECH.sub("", text).strip()


'''
    if "Gemini multilingual STT first" not in ears:
        ears = replace_once(ears, "def warm():\n", "def _warm_local():\n",
                            "local STT warm function")
        ears = replace_once(ears, "    model = warm()\n",
                            "    model = _warm_local()\n", "local STT call")
        ears = replace_once(ears, "def transcribe(pcm: np.ndarray) -> str:\n",
                            "def _transcribe_local(pcm: np.ndarray) -> str:\n",
                            "local STT function")
        ears = insert_before_once(ears, "class Ears:\n", ears_wrapper,
                                  "Gemini multilingual STT first",
                                  "multilingual STT wrapper")
    ears_file.write_text(ears, encoding="utf-8")

    mouth = mouth_file.read_text(encoding="utf-8")
    old_synth = '''def synth_stream(text: str, timeout: float = 30.0):
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
'''
    new_synth = '''def synth_stream(text: str, timeout: float = 30.0):
    """Gemini streaming TTS first, then language-aware local fallbacks."""
    from backtalk.speech_router import (
        contains_urdu_script, gemini_tts_enabled, stream_gemini_tts,
        stream_local_urdu,
    )
    if gemini_tts_enabled():
        cloud_started = False
        try:
            for rate, pcm in stream_gemini_tts(text):
                cloud_started = True
                yield rate, pcm
            if cloud_started:
                return
        except Exception:
            if cloud_started:
                return
    if contains_urdu_script(text):
        try:
            yield from stream_local_urdu(text)
            return
        except Exception as e:
            log(f"[mouth] local Urdu failed ({str(e)[:80]}) — "
                f"falling back to {CFG['voice']}")
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
'''
    mouth = replace_once(mouth, old_synth, new_synth,
                         "free-first multilingual TTS")
    mouth_file.write_text(mouth, encoding="utf-8")

    (target / "PROVIDER_COMPATIBILITY.md").write_text(
        "# Gemini and OpenAI compatibility\n\n"
        "This checkout is patched by the sibling `fullstack-agent` repository. "
        "The brain uses an OpenAI-compatible Chat Completions API instead of the "
        "Claude Agent SDK.\n\n"
        "Set `provider` and `model` in `backtalk.json`. Keep the secret out of "
        "that file: use `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `AI_API_KEY` in "
        "the launcher's environment. Custom services also need `base_url` in "
        "the config. Re-run `fullstack-agent/provider_bridge/patch_backtalk.py` "
        "after manually updating Backtalk. Speech uses Gemini 3.5 Transcribe "
        "and Gemini 3.1 Flash TTS first, with local Whisper, MMS Urdu, and "
        "Kokoro fallbacks.\n",
        encoding="utf-8",
    )
    print(f"Patched Backtalk for Gemini/OpenAI APIs: {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backtalk_dir", type=Path)
    args = parser.parse_args()
    patch(args.backtalk_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
