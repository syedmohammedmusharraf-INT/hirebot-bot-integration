"""Model factory functions for the cascade voice pipeline (plan section 4.2, 4.3).

Builds STT / LLM / TTS plugin instances for an ``AssistantConfig``, enforcing
the provider allowlists and language-code conventions the plan calls out so a
misconfigured assistant record fails fast instead of producing a silent call.
"""

from __future__ import annotations

import logging

from livekit.plugins import cartesia, deepgram, elevenlabs, openai, sarvam

from model_support.allowlists import CASCADE_STT_PROVIDERS, CASCADE_TTS_PROVIDERS, OPENAI_CASCADE_MODELS
from model_support.assistant_config import AssistantConfig
from model_support.knobs import is_reasoning_model

logger = logging.getLogger(__name__)

DEFAULT_CASCADE_LLM_MODEL = "gpt-4.1"


def create_stt(cfg: AssistantConfig):
    """Build the cascade STT plugin per plan section 4.3.

    ``assistant_stt_model`` unset falls back to Sarvam ``saaras:v3`` with
    ``language=unknown`` (Sarvam's own "auto" convention), matching the plan's
    stated unset-default rather than silently picking a different vendor.
    """
    provider = cfg.assistant_stt_model
    stt_cfg = cfg.assistant_stt_config or {}

    if provider is None:
        return sarvam.STT(model=stt_cfg.get("model", "saaras:v3"), language=stt_cfg.get("language", "unknown"))

    if provider not in CASCADE_STT_PROVIDERS:
        raise ValueError(f"assistant_stt_model {provider!r} is not an allowed cascade STT provider: {CASCADE_STT_PROVIDERS}")

    if provider == "deepgram":
        return deepgram.STT(model=stt_cfg.get("model", "nova-3"), language=stt_cfg.get("language", "multi"))
    if provider == "sarvam":
        return sarvam.STT(model=stt_cfg.get("model", "saaras:v3"), language=stt_cfg.get("language", "unknown"))
    if provider == "elevenlabs":
        return elevenlabs.STT(model=stt_cfg.get("model", "scribe_v1"), language=stt_cfg.get("language"))
    if provider == "cartesia":
        return cartesia.STT(model=stt_cfg.get("model", "ink-whisper"), language=stt_cfg.get("language", "en"))
    if provider == "openai":
        return openai.STT(model=stt_cfg.get("model", "gpt-4o-transcribe"), language=stt_cfg.get("language"))

    raise ValueError(f"No STT factory branch wired for allowlisted provider {provider!r}")


def create_llm(cfg: AssistantConfig):
    """Build the cascade LLM per plan section 4.3 ("cascade is OpenAI-only; omitted = gpt-4.1").

    Gates ``temperature`` vs ``reasoning_effort``/``verbosity`` on the model
    family so a reasoning model doesn't get an every-turn 400 from an
    unsupported knob (plan section 4.3 / section 5 step 12 "silent-call class
    of failures").
    """
    llm_cfg = cfg.assistant_llm_config or {}
    provider = llm_cfg.get("provider", "openai")
    if provider != "openai":
        raise ValueError(f"cascade assistant_mode only supports the openai LLM provider, got {provider!r}")

    model = llm_cfg.get("model", DEFAULT_CASCADE_LLM_MODEL)
    if model not in OPENAI_CASCADE_MODELS:
        raise ValueError(f"assistant_llm_config.model {model!r} is not in the cascade allowlist: {OPENAI_CASCADE_MODELS}")

    knobs: dict = {}
    if is_reasoning_model(model):
        knobs["reasoning_effort"] = llm_cfg.get("reasoning_effort", "medium")
        knobs["verbosity"] = llm_cfg.get("verbosity", "medium")
        llm = openai.LLM(model=model, reasoning_effort=knobs["reasoning_effort"], verbosity=knobs["verbosity"])
    else:
        knobs["temperature"] = llm_cfg.get("temperature", 0.7)
        llm = openai.LLM(model=model, temperature=knobs["temperature"])

    # has_tools is hardcoded False here: no tool/function-calling wiring in this
    # pass of the plan. Update this line if/when tools are added to AgentSession.
    logger.info("Cascade LLM built | model=%s | has_tools=%s | knobs=%s", model, False, knobs)
    return llm


def create_tts(cfg: AssistantConfig):
    """Build the cascade/pipeline TTS per plan section 4.3 ("required in cascade/pipeline; no server default voice")."""
    provider = cfg.assistant_tts_model
    if provider is None:
        raise ValueError("assistant_tts_model is required in cascade/pipeline mode; there is no server default voice")

    if provider not in CASCADE_TTS_PROVIDERS:
        raise ValueError(f"assistant_tts_model {provider!r} is not an allowed TTS provider: {CASCADE_TTS_PROVIDERS}")

    tts_cfg = cfg.assistant_tts_config or {}
    voice_id = tts_cfg.get("voice_id")
    if not voice_id:
        raise ValueError("assistant_tts_config.voice_id is required; no server default voice")

    if provider == "cartesia":
        return cartesia.TTS(voice=voice_id, language=tts_cfg.get("language", "en"))
    if provider == "elevenlabs":
        return elevenlabs.TTS(voice_id=voice_id, language=tts_cfg.get("language"))
    if provider == "openai":
        return openai.TTS(voice=voice_id)

    raise ValueError(f"No TTS factory branch wired for allowlisted provider {provider!r}")
