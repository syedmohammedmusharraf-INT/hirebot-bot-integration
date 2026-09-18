"""Legal model/provider sets per assistant_mode (plan section 4.2 / 4.3).

Kept as plain data so both the control plane and the agent worker validate
against the exact same source of truth.
"""

# assistant_mode values a local assistant record may declare.
ASSISTANT_MODES = ("pipeline", "realtime", "cascade")

# realtime mode: one model does STT+LLM+TTS audio-out.
GEMINI_LIVE_MODELS = [
    "gemini-2.5-flash-native-audio-preview-12-2025",
]
OPENAI_REALTIME_MODELS = [
    "gpt-realtime-1.5",
]
REALTIME_MODELS = {
    "gemini": GEMINI_LIVE_MODELS,
    "openai": OPENAI_REALTIME_MODELS,
}
REALTIME_DEFAULT_VOICE = {
    "gemini": "Puck",
    "openai": "marin",
}

# pipeline mode (half-cascade): realtime LLM in text-only modality + external TTS.
# Gemini Live native-audio models cannot run text-only modality, so pipeline is
# OpenAI-only per plan section 4.2.
PIPELINE_VENDORS = ("openai",)

# cascade mode: plugin-STT -> openai.responses.LLM -> plugin-TTS. LLM is
# OpenAI-only in cascade; STT/TTS are swappable plugins.
OPENAI_CASCADE_MODELS = [
    "gpt-4.1",
    "gpt-4.1-mini",
]
OPENAI_CASCADE_DEFAULT_MODEL = "gpt-4.1"

CASCADE_STT_PROVIDERS = ["deepgram", "sarvam", "elevenlabs", "cartesia", "openai"]
CASCADE_STT_DEFAULT_PROVIDER = "sarvam"
CASCADE_STT_DEFAULT_MODEL = "saaras:v3"

CASCADE_TTS_PROVIDERS = ["cartesia", "elevenlabs", "openai"]

# LLM provider -> assistant_mode values it may legally be used in.
PROVIDER_MODE_MATRIX = {
    "openai": ["pipeline", "realtime", "cascade"],
    "gemini": ["realtime"],
}

# STT language-code convention per provider (plan section 4.3) -- not
# enforced strictly here (providers accept free-form strings) but documented
# so callers don't assume one code standard is portable across providers.
STT_LANGUAGE_CODE_CONVENTION = {
    "sarvam": "BCP-47 Indic (e.g. hi-IN) or 'unknown' for auto-detect",
    "cartesia": "ISO 639-1 (e.g. en)",
    "deepgram": "BCP-47 or the literal string 'multi'",
    "elevenlabs": "ISO 639-3",
    "openai": "ISO 639-1",
}
