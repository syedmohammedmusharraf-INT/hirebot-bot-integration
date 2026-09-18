"""Unit tests for the shared model_support validation package (plan section 4.3)."""

import pytest

from model_support.assistant_config import InvalidAssistantConfig, load_assistant_config
from model_support.knobs import is_reasoning_model, llm_sampling_kwargs


def test_valid_cascade_record_loads():
    cfg = load_assistant_config(
        {
            "assistant_name": "Meet Voice Bot",
            "assistant_mode": "cascade",
            "assistant_stt_model": "deepgram",
            "assistant_stt_config": {"model": "nova-3", "language": "multi"},
            "assistant_llm_config": {"provider": "openai", "model": "gpt-4.1-mini"},
            "assistant_tts_model": "cartesia",
            "assistant_tts_config": {"voice_id": "abc123", "language": "en"},
            "assistant_interaction_config": {"speaks_first": False, "input_guard_window_sec": 3.0},
        }
    )
    assert cfg.assistant_mode == "cascade"
    assert cfg.assistant_llm_config == {"provider": "openai", "model": "gpt-4.1-mini"}
    assert cfg.assistant_interaction_config["input_guard_window_sec"] == 3.0


def test_unknown_top_level_key_rejected():
    with pytest.raises(InvalidAssistantConfig, match="unknown assistant config key"):
        load_assistant_config({"assistant_name": "x", "not_a_real_field": 1})


def test_missing_assistant_name_rejected():
    with pytest.raises(InvalidAssistantConfig, match="missing required"):
        load_assistant_config({"assistant_mode": "cascade"})


def test_cascade_requires_tts_model():
    with pytest.raises(InvalidAssistantConfig, match="assistant_tts_model is required"):
        load_assistant_config({"assistant_name": "x", "assistant_mode": "cascade", "assistant_llm_config": {"model": "gpt-4.1-mini"}})


def test_cascade_rejects_non_openai_llm_provider():
    with pytest.raises(InvalidAssistantConfig, match="OpenAI-only"):
        load_assistant_config(
            {
                "assistant_name": "x",
                "assistant_mode": "cascade",
                "assistant_llm_config": {"provider": "gemini", "model": "gpt-4.1-mini"},
                "assistant_tts_model": "cartesia",
            }
        )


def test_pipeline_rejects_gemini_vendor():
    with pytest.raises(InvalidAssistantConfig, match="pipeline mode only supports"):
        load_assistant_config(
            {
                "assistant_name": "x",
                "assistant_mode": "pipeline",
                "assistant_llm_config": {"provider": "gemini"},
                "assistant_tts_model": "cartesia",
            }
        )


def test_realtime_rejects_illegal_model_for_vendor():
    with pytest.raises(InvalidAssistantConfig, match="not legal for vendor"):
        load_assistant_config(
            {
                "assistant_name": "x",
                "assistant_mode": "realtime",
                "assistant_llm_config": {"provider": "gemini", "model": "gpt-realtime-1.5"},
            }
        )


def test_reasoning_model_detection():
    assert is_reasoning_model("o3-mini") is True
    assert is_reasoning_model("gpt-4.1-mini") is False
    assert llm_sampling_kwargs("o3-mini") == {"reasoning_effort": "medium", "verbosity": "medium"}
    assert llm_sampling_kwargs("gpt-4.1-mini") == {"temperature": 0.7}
