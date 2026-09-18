"""Local assistant record schema + validation (plan section 4.3).

The studied `POST /assistant/create` body becomes a local YAML record (see
`assistants/*.yaml`). This module is the single place that decides whether one
of those records is legal, so the control plane (which loads it to dispatch
the agent) and the agent worker (which loads it to build the session) can
never disagree.
"""

from dataclasses import dataclass, field

from model_support.allowlists import (
    ASSISTANT_MODES,
    CASCADE_STT_DEFAULT_MODEL,
    CASCADE_STT_DEFAULT_PROVIDER,
    CASCADE_STT_PROVIDERS,
    CASCADE_TTS_PROVIDERS,
    GEMINI_LIVE_MODELS,
    OPENAI_CASCADE_DEFAULT_MODEL,
    OPENAI_CASCADE_MODELS,
    OPENAI_REALTIME_MODELS,
    PIPELINE_VENDORS,
    PROVIDER_MODE_MATRIX,
)

_REQUIRED_TOP_LEVEL_KEYS = {
    "assistant_name",
}

_ALLOWED_TOP_LEVEL_KEYS = {
    "assistant_name",
    "assistant_mode",
    "assistant_stt_model",
    "assistant_stt_config",
    "assistant_llm_config",
    "assistant_tts_model",
    "assistant_tts_config",
    "assistant_interaction_config",
}


class InvalidAssistantConfig(Exception):
    """Raised for any unknown key, illegal mode/provider/model combination, or
    missing required field in a local assistant record."""


@dataclass(frozen=True)
class AssistantConfig:
    assistant_name: str
    assistant_mode: str = "cascade"
    assistant_stt_model: str | None = None
    assistant_stt_config: dict = field(default_factory=dict)
    assistant_llm_config: dict = field(default_factory=dict)
    assistant_tts_model: str | None = None
    assistant_tts_config: dict = field(default_factory=dict)
    assistant_interaction_config: dict = field(default_factory=dict)


def load_assistant_config(data: dict) -> AssistantConfig:
    """Validate a raw dict (parsed from an assistant YAML/JSON record) and
    return an `AssistantConfig`. Raises `InvalidAssistantConfig` with a clear,
    specific message on any problem instead of silently degrading -- the plan
    is explicit that silent fallback (e.g. an omitted `assistant_mode` quietly
    becoming `pipeline`) is a diagnosable-but-surprising failure mode, so the
    *validator* stays strict even though individual fields do have documented
    fallback defaults once validation passes.
    """
    if not isinstance(data, dict):
        raise InvalidAssistantConfig(f"assistant record must be a mapping, got {type(data).__name__}")

    unknown_keys = set(data.keys()) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_keys:
        raise InvalidAssistantConfig(f"unknown assistant config key(s): {sorted(unknown_keys)}")

    missing_keys = _REQUIRED_TOP_LEVEL_KEYS - set(data.keys())
    if missing_keys:
        raise InvalidAssistantConfig(f"missing required assistant config key(s): {sorted(missing_keys)}")

    assistant_mode = data.get("assistant_mode", "pipeline")
    if assistant_mode not in ASSISTANT_MODES:
        raise InvalidAssistantConfig(f"assistant_mode must be one of {ASSISTANT_MODES}, got {assistant_mode!r}")

    llm_config = data.get("assistant_llm_config") or {}
    if not isinstance(llm_config, dict):
        raise InvalidAssistantConfig("assistant_llm_config must be a mapping")
    llm_provider = llm_config.get("provider")
    llm_model = llm_config.get("model")

    if assistant_mode == "cascade":
        if llm_provider not in (None, "openai"):
            raise InvalidAssistantConfig(f"cascade mode is OpenAI-only for the LLM stage, got provider={llm_provider!r}")
        llm_model = llm_model or OPENAI_CASCADE_DEFAULT_MODEL
        if llm_model not in OPENAI_CASCADE_MODELS:
            raise InvalidAssistantConfig(f"assistant_llm_config.model {llm_model!r} is not in OPENAI_CASCADE_MODELS {OPENAI_CASCADE_MODELS}")

        stt_model = data.get("assistant_stt_model") or CASCADE_STT_DEFAULT_PROVIDER
        if stt_model not in CASCADE_STT_PROVIDERS:
            raise InvalidAssistantConfig(f"assistant_stt_model {stt_model!r} is not in CASCADE_STT_PROVIDERS {CASCADE_STT_PROVIDERS}")
        if stt_model == "sarvam" and not (data.get("assistant_stt_config") or {}).get("model"):
            # Documented unset default per plan section 4.3.
            data = {**data, "assistant_stt_config": {**(data.get("assistant_stt_config") or {}), "model": CASCADE_STT_DEFAULT_MODEL}}

        tts_model = data.get("assistant_tts_model")
        if not tts_model:
            raise InvalidAssistantConfig("assistant_tts_model is required in cascade mode -- there is no server default voice")
        if tts_model not in CASCADE_TTS_PROVIDERS:
            raise InvalidAssistantConfig(f"assistant_tts_model {tts_model!r} is not in CASCADE_TTS_PROVIDERS {CASCADE_TTS_PROVIDERS}")

    elif assistant_mode == "pipeline":
        if llm_provider not in (None, *PIPELINE_VENDORS):
            raise InvalidAssistantConfig(f"pipeline mode only supports vendor(s) {PIPELINE_VENDORS}, got {llm_provider!r}")
        tts_model = data.get("assistant_tts_model")
        if not tts_model:
            raise InvalidAssistantConfig("assistant_tts_model is required in pipeline mode -- there is no server default voice")

    elif assistant_mode == "realtime":
        if llm_provider not in ("gemini", "openai", None):
            raise InvalidAssistantConfig(f"realtime mode only supports vendor(s) ('gemini', 'openai'), got {llm_provider!r}")
        vendor = llm_provider or "gemini"
        realtime_models = GEMINI_LIVE_MODELS if vendor == "gemini" else OPENAI_REALTIME_MODELS
        if llm_model and llm_model not in realtime_models:
            raise InvalidAssistantConfig(f"realtime model {llm_model!r} is not legal for vendor {vendor!r}: {realtime_models}")

    if llm_provider and assistant_mode in PROVIDER_MODE_MATRIX and assistant_mode not in PROVIDER_MODE_MATRIX.get(llm_provider, []):
        raise InvalidAssistantConfig(f"provider {llm_provider!r} may not be used in assistant_mode {assistant_mode!r}")

    interaction_config = data.get("assistant_interaction_config") or {}
    if not isinstance(interaction_config, dict):
        raise InvalidAssistantConfig("assistant_interaction_config must be a mapping")
    guard_window = interaction_config.get("input_guard_window_sec", 3.0)
    if not isinstance(guard_window, (int, float)) or guard_window < 0:
        raise InvalidAssistantConfig(f"assistant_interaction_config.input_guard_window_sec must be a non-negative number, got {guard_window!r}")

    return AssistantConfig(
        assistant_name=data["assistant_name"],
        assistant_mode=assistant_mode,
        assistant_stt_model=data.get("assistant_stt_model"),
        assistant_stt_config=data.get("assistant_stt_config") or {},
        assistant_llm_config={"provider": llm_provider or "openai", "model": llm_model},
        assistant_tts_model=data.get("assistant_tts_model"),
        assistant_tts_config=data.get("assistant_tts_config") or {},
        assistant_interaction_config={
            "speaks_first": bool(interaction_config.get("speaks_first", False)),
            "input_guard_window_sec": float(guard_window),
        },
    )
