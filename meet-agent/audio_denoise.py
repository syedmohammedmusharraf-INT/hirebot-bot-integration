"""SpeechGate: noise suppression + VAD hard-gating + manual mute (plan section 4.5).

Wire ``process_frame`` into whichever per-session audio-frame hook the pinned
``livekit-agents`` release exposes on ``Agent``/``AgentSession`` (this has moved
between SDK releases, e.g. an ``stt_node`` override or an input audio filter
callback — check the pinned version's docs). ``process_frame`` itself has no
LiveKit dependency, so it is stable regardless of which hook wires it in.

One instance per stream: internal VAD/APM state assumes a single, continuous
audio stream and must not be shared across multiple participants or calls.
"""

from __future__ import annotations

import logging
import os

import numpy as np

from audio_pipeline import resample_48k_mono

try:
    import onnxruntime as ort
except ImportError:  # pragma: no cover - optional import for lint/tooling environments without onnxruntime
    ort = None

try:
    from webrtc_noise_gain import AudioProcessor as _WebRtcNoiseSuppressor
except ImportError:  # pragma: no cover - optional import for lint/tooling environments without the package
    _WebRtcNoiseSuppressor = None

logger = logging.getLogger(__name__)

_VAD_SAMPLE_RATE = 16000
_VAD_WINDOW_SAMPLES = 512
_VAD_CONTEXT_SAMPLES = 64
_VAD_HANGOVER_SEC = 0.6
_VAD_THRESHOLD = 0.5

SILERO_VAD_ONNX_PATH_ENV = "SILERO_VAD_ONNX_PATH"


class SpeechGate:
    """Noise-suppress + VAD-gate + optionally hard-mute a mono int16 audio stream.

    Zeroes gated/muted audio in place rather than dropping frames, so downstream
    STT sees a continuous stream (silence, not gaps) -- required so endpointing
    timers aren't confused by missing packets. No WebRTC AGC/AEC and no
    far_field pre-NS branch (both explicitly out of scope per plan section 4.5).
    """

    def __init__(self, sample_rate: int = 48000):
        self.sample_rate = sample_rate
        self.muted = False

        self._ns = _WebRtcNoiseSuppressor(sample_rate=sample_rate) if _WebRtcNoiseSuppressor else None
        if self._ns is None:
            logger.warning("webrtc_noise_gain not installed; SpeechGate running without noise suppression")

        self._vad_session = None
        onnx_path = os.environ.get(SILERO_VAD_ONNX_PATH_ENV)
        if ort is not None and onnx_path:
            self._vad_session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        else:
            logger.warning(
                "Silero VAD ONNX model not loaded (%s unset or onnxruntime missing); SpeechGate hard-gate disabled",
                SILERO_VAD_ONNX_PATH_ENV,
            )

        # Silero's recurrent state for the streaming ONNX export (v4/v5 shape).
        self._vad_state = np.zeros((2, 1, 128), dtype=np.float32) if self._vad_session is not None else None
        self._vad_context = np.zeros(_VAD_CONTEXT_SAMPLES, dtype=np.float32)
        self._hangover_frames_remaining = 0
        self._hangover_frame_budget = int(_VAD_HANGOVER_SEC * sample_rate)

        # Guards against the SDK invoking process_frame twice for the same
        # audio (some AgentSession wiring re-runs the input pipeline on retry).
        self._last_frame_id: object | None = None
        self._last_frame_result: np.ndarray | None = None

    def process_frame(self, frame_id, pcm: np.ndarray) -> np.ndarray:
        """Process one mono PCM chunk at ``self.sample_rate``.

        ``frame_id`` must be a value that uniquely identifies this chunk (a
        sequence number, timestamp, or the frame object's id()); if the same
        ``frame_id`` is seen twice in a row, the cached result is returned
        instead of processing again.
        """
        if frame_id == self._last_frame_id and self._last_frame_result is not None:
            return self._last_frame_result

        out = pcm.copy()

        if self._ns is not None:
            out = self._ns.process(out)

        speech_detected = self._run_vad(out) if self._vad_session is not None else True

        if speech_detected:
            self._hangover_frames_remaining = self._hangover_frame_budget
        else:
            self._hangover_frames_remaining = max(0, self._hangover_frames_remaining - len(out))

        gated_open = speech_detected or self._hangover_frames_remaining > 0

        if self.muted or not gated_open:
            out[:] = 0

        self._last_frame_id = frame_id
        self._last_frame_result = out
        return out

    def _run_vad(self, pcm_at_stream_rate: np.ndarray) -> bool:
        """Downsample to 16 kHz and run Silero's streaming ONNX model over 512-sample windows."""
        pcm_16k = self._to_16k(pcm_at_stream_rate)
        if pcm_16k.size == 0:
            return False

        windowed = np.concatenate([self._vad_context, pcm_16k])
        speech = False

        while windowed.size >= _VAD_WINDOW_SAMPLES:
            chunk = windowed[:_VAD_WINDOW_SAMPLES]
            windowed = windowed[_VAD_WINDOW_SAMPLES:]

            ort_inputs = {
                "input": chunk[np.newaxis, :].astype(np.float32),
                "sr": np.array(_VAD_SAMPLE_RATE, dtype=np.int64),
                "state": self._vad_state,
            }
            try:
                prob, self._vad_state = self._vad_session.run(None, ort_inputs)
            except Exception:
                logger.exception("Silero VAD ONNX inference failed; failing open (treating frame as speech)")
                return True

            if float(np.asarray(prob).reshape(-1)[0]) >= _VAD_THRESHOLD:
                speech = True

        if windowed.size >= _VAD_CONTEXT_SAMPLES:
            self._vad_context = windowed[-_VAD_CONTEXT_SAMPLES:]
        else:
            self._vad_context = np.pad(windowed, (_VAD_CONTEXT_SAMPLES - windowed.size, 0))

        return speech

    def _to_16k(self, pcm: np.ndarray) -> np.ndarray:
        # Re-uses the transport resampler in reverse, purely for VAD analysis;
        # the audio actually sent downstream is untouched by this conversion.
        float_pcm = (pcm.astype(np.float32) / 32768.0) if pcm.dtype == np.int16 else pcm.astype(np.float32)
        return resample_48k_mono(float_pcm, orig_sr=self.sample_rate, target_sr=_VAD_SAMPLE_RATE)
