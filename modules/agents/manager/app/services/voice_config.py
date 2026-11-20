# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""#2087 — personal agents speak and listen through the box's Speaches (stts).

One place for the endpoint and the model names the agents are given — the same
models cli/post-install.sh configures Open WebUI's audio with. The endpoint is
always set: with the `stts` profile off, `speaches` does not resolve and voice
fails closed instead of falling back to a cloud provider (Hermes' default TTS
is Edge, a cloud service)."""
from __future__ import annotations

SPEACHES_BASE_URL = "http://speaches:8000/v1"
#: Speaches takes no key. A non-empty value only stops the clients from falling
#: back to OPENAI_API_KEY — the agent's LLM key, which must not travel to a
#: second service.
SPEACHES_API_KEY = "speaches-local"
STT_MODEL = "Systran/faster-whisper-small"
TTS_MODEL = "ufozone/piper-de_DE-jarvis-high"
TTS_VOICE = "de_DE-jarvis-high"


def hermes_voice_commands() -> list[str]:
    """`hermes config set` lines for the boot script (same mechanism as model.*)."""
    pairs = (
        ("tts.provider", "openai"),
        ("tts.openai.base_url", SPEACHES_BASE_URL),
        ("tts.openai.api_key", SPEACHES_API_KEY),
        ("tts.openai.model", TTS_MODEL),
        ("tts.openai.voice", TTS_VOICE),
        ("stt.provider", "openai"),
        ("stt.openai.base_url", SPEACHES_BASE_URL),
        ("stt.openai.api_key", SPEACHES_API_KEY),
        ("stt.openai.model", STT_MODEL),
    )
    return [f'hermes config set {key} "{value}" 2>/dev/null' for key, value in pairs]


def moltis_voice_env() -> dict[str, str]:
    """moltis.toml [voice.tts.openai] / [voice.stt.whisper], as MOLTIS_* env."""
    return {
        'MOLTIS_VOICE__TTS__PROVIDER': 'openai',
        'MOLTIS_VOICE__TTS__OPENAI__BASE_URL': SPEACHES_BASE_URL,
        'MOLTIS_VOICE__TTS__OPENAI__API_KEY': SPEACHES_API_KEY,
        'MOLTIS_VOICE__TTS__OPENAI__MODEL': TTS_MODEL,
        'MOLTIS_VOICE__TTS__OPENAI__VOICE': TTS_VOICE,
        'MOLTIS_VOICE__STT__PROVIDER': 'whisper',
        'MOLTIS_VOICE__STT__WHISPER__BASE_URL': SPEACHES_BASE_URL,
        'MOLTIS_VOICE__STT__WHISPER__API_KEY': SPEACHES_API_KEY,
        'MOLTIS_VOICE__STT__WHISPER__MODEL': STT_MODEL,
    }
