"""Per-call AgentSession builder dispatching on assistant_mode (plan section 4.2, 4.3).

cascade: STT -> openai LLM -> TTS, each separately metered/swappable
(recommended for Meet). pipeline: realtime LLM in text-only modality +
external TTS, OpenAI vendor only (Live API native-audio models can't do
text-only modality, so gemini is rejected here). realtime: one model does
STT+LLM+TTS audio-out, vendor gemini or openai.
"""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import AgentSession, JobContext
from livekit.plugins import silero

try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel as TurnDetectorV1Mini
except ImportError:  # pragma: no cover - surfaced as a clear error at build time instead
    TurnDetectorV1Mini = None

import model_factories
from audio_denoise import SpeechGate
from model_support.allowlists import GEMINI_LIVE_MODELS, REALTIME_MODELS
from model_support.assistant_config import AssistantConfig

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_REALTIME_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_GEMINI_REALTIME_VOICE = "Puck"
DEFAULT_OPENAI_REALTIME_MODEL = "gpt-realtime-1.5"
DEFAULT_OPENAI_REALTIME_VOICE = "marin"


async def build_session(ctx: JobContext, assistant_cfg: AssistantConfig) -> AgentSession:
    """Build (but do not start) the AgentSession for one dispatched job.

    Sets ``session.speaks_first`` (bool) and ``session.speech_gate``
    (SpeechGate) as extra attributes agent_run.py reads after session.start().
    """
    mode = assistant_cfg.assistant_mode

    if mode == "cascade":
        session = _build_cascade_session(assistant_cfg)
    elif mode == "pipeline":
        session = _build_pipeline_session(assistant_cfg)
    elif mode == "realtime":
        session = _build_realtime_session(assistant_cfg)
    else:
        raise ValueError(f"Unknown assistant_mode {mode!r}; expected 'cascade', 'pipeline', or 'realtime'")

    interaction_cfg = assistant_cfg.assistant_interaction_config or {}
    session.speaks_first = bool(interaction_cfg.get("speaks_first", False))

    _wire_input_guard(session, assistant_cfg)
    return session


def _build_cascade_session(cfg: AssistantConfig) -> AgentSession:
    if TurnDetectorV1Mini is None:
        raise RuntimeError("livekit-plugins-turn-detector is not installed; cascade mode requires the pinned v1-mini turn detector")

    stt = model_factories.create_stt(cfg)
    llm = model_factories.create_llm(cfg)
    tts = model_factories.create_tts(cfg)
    vad = silero.VAD.load(min_silence_duration=0.4)

    # Never turn_detection="realtime_llm" in cascade (plan section 4.5): Cloud
    # v1/adaptive interruption modes are unusable self-hosted, so the local
    # v1-mini model is pinned unconditionally here.
    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        turn_detection=TurnDetectorV1Mini(),
    )


def _build_pipeline_session(cfg: AssistantConfig) -> AgentSession:
    llm_cfg = cfg.assistant_llm_config or {}
    provider = llm_cfg.get("provider", "openai")
    if provider != "openai":
        raise ValueError(
            f"pipeline assistant_mode only supports the openai vendor, got {provider!r} "
            "(gemini's Live API native-audio models can't run in text-only modality)"
        )

    from livekit.plugins import openai as openai_plugin

    tts = model_factories.create_tts(cfg)
    realtime_llm = openai_plugin.realtime.RealtimeModel(
        model=llm_cfg.get("model", DEFAULT_OPENAI_REALTIME_MODEL),
        modalities=["text"],
    )
    return AgentSession(llm=realtime_llm, tts=tts)


def _build_realtime_session(cfg: AssistantConfig) -> AgentSession:
    llm_cfg = cfg.assistant_llm_config or {}
    provider = llm_cfg.get("provider", "gemini")

    if provider == "gemini":
        model = llm_cfg.get("model", DEFAULT_GEMINI_REALTIME_MODEL)
        if model not in GEMINI_LIVE_MODELS:
            raise ValueError(f"realtime gemini model {model!r} not in allowlist: {GEMINI_LIVE_MODELS}")

        from livekit.plugins import google

        realtime_llm = google.beta.realtime.RealtimeModel(model=model, voice=llm_cfg.get("voice", DEFAULT_GEMINI_REALTIME_VOICE))
    elif provider == "openai":
        model = llm_cfg.get("model", DEFAULT_OPENAI_REALTIME_MODEL)
        if model not in REALTIME_MODELS.get("openai", []):
            raise ValueError(f"realtime openai model {model!r} not in allowlist: {REALTIME_MODELS.get('openai', [])}")

        from livekit.plugins import openai as openai_plugin

        realtime_llm = openai_plugin.realtime.RealtimeModel(model=model, voice=llm_cfg.get("voice", DEFAULT_OPENAI_REALTIME_VOICE))
    else:
        raise ValueError(f"realtime assistant_mode only supports 'gemini' or 'openai' vendors, got {provider!r}")

    return AgentSession(llm=realtime_llm)


def _wire_input_guard(session: AgentSession, cfg: AssistantConfig) -> None:
    """Mute the input SpeechGate for the duration of each agent reply (plan section 4.5).

    ``input_guard_window_sec`` bounds the mute; it early-unmutes on
    "agent finished speaking" and is skipped entirely when the window is 0.
    """
    interaction_cfg = cfg.assistant_interaction_config or {}
    guard_window_sec = float(interaction_cfg.get("input_guard_window_sec", 3.0))

    gate = SpeechGate()
    session.speech_gate = gate

    if guard_window_sec <= 0:
        logger.info("input_guard_window_sec=0; per-utterance input guard disabled")
        return

    mute_task: asyncio.Task | None = None

    async def _auto_unmute_after(window: float) -> None:
        await asyncio.sleep(window)
        gate.muted = False

    def _on_agent_started_speaking(*_args, **_kwargs) -> None:
        nonlocal mute_task
        gate.muted = True
        if mute_task and not mute_task.done():
            mute_task.cancel()
        mute_task = asyncio.create_task(_auto_unmute_after(guard_window_sec))

    def _on_agent_finished_speaking(*_args, **_kwargs) -> None:
        gate.muted = False
        if mute_task and not mute_task.done():
            mute_task.cancel()

    # Event names per the current AgentSession event surface; verify against the
    # pinned SDK version if these don't fire -- they have been renamed across
    # livekit-agents releases.
    session.on("agent_started_speaking", _on_agent_started_speaking)
    session.on("agent_stopped_speaking", _on_agent_finished_speaking)
