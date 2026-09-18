"""Pure-DSP helpers for the transport audio path (plan section 4.5).

Transport target is 48 kHz mono int16 in-room. Resampling uses a polyphase FIR
(never linear interpolation, which aliases), output is soft-limited instead of
hard-clipped, and a stateful high-pass strips DC/hum without touching male
pitch (a 300 Hz bandpass would; deliberately not implemented here).

No LiveKit imports on purpose: this module operates on plain numpy arrays so it
can be unit tested without a room/session in scope.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import resample_poly


def resample_48k_mono(pcm: np.ndarray, orig_sr: int, target_sr: int = 48000) -> np.ndarray:
    """Resample int16 or float32 mono PCM to ``target_sr`` via polyphase FIR filtering."""
    if orig_sr == target_sr:
        return pcm

    divisor = gcd(target_sr, orig_sr)
    up, down = target_sr // divisor, orig_sr // divisor

    is_int16 = pcm.dtype == np.int16
    working = (pcm.astype(np.float32) / 32768.0) if is_int16 else pcm.astype(np.float32)

    resampled = resample_poly(working, up, down).astype(np.float32)

    if is_int16:
        return np.clip(resampled * 32768.0, -32768, 32767).astype(np.int16)
    return resampled


def soft_limit(pcm: np.ndarray, threshold: float = 0.9) -> np.ndarray:
    """Tanh-style soft limiter: transients compress smoothly instead of hard-clipping."""
    is_int16 = pcm.dtype == np.int16
    x = (pcm.astype(np.float32) / 32768.0) if is_int16 else pcm.astype(np.float32)

    over = np.abs(x) > threshold
    limited = np.where(
        over,
        np.sign(x) * (threshold + (1 - threshold) * np.tanh((np.abs(x) - threshold) / max(1 - threshold, 1e-6))),
        x,
    )

    if is_int16:
        return np.clip(limited * 32768.0, -32768, 32767).astype(np.int16)
    return limited.astype(np.float32)


class DCHighPassFilter:
    """Stateful ~80 Hz high-pass biquad for DC/hum removal only.

    One instance per audio stream. Filter state (``_x1``/``_x2``/``_y1``/``_y2``)
    is carried across successive ``process()`` calls so packet boundaries don't
    introduce clicks. Deliberately NOT a 300 Hz bandpass: per plan section 4.5,
    that would strip the fundamental of male voices.
    """

    def __init__(self, sample_rate: int = 48000, cutoff_hz: float = 80.0):
        self.sample_rate = sample_rate
        self.cutoff_hz = cutoff_hz
        self._b, self._a = self._design_biquad(sample_rate, cutoff_hz)
        self._x1 = self._x2 = self._y1 = self._y2 = 0.0

    @staticmethod
    def _design_biquad(sample_rate: float, cutoff_hz: float):
        # RBJ audio-EQ-cookbook high-pass biquad, Q = 1/sqrt(2) (Butterworth).
        w0 = 2 * np.pi * cutoff_hz / sample_rate
        alpha = np.sin(w0) / (2 * (1 / np.sqrt(2)))
        cos_w0 = np.cos(w0)

        b0 = (1 + cos_w0) / 2
        b1 = -(1 + cos_w0)
        b2 = (1 + cos_w0) / 2
        a0 = 1 + alpha
        a1 = -2 * cos_w0
        a2 = 1 - alpha

        b = np.array([b0, b1, b2]) / a0
        a = np.array([1.0, a1 / a0, a2 / a0])
        return b, a

    def process(self, pcm: np.ndarray) -> np.ndarray:
        is_int16 = pcm.dtype == np.int16
        x = (pcm.astype(np.float64) / 32768.0) if is_int16 else pcm.astype(np.float64)

        out = np.empty_like(x)
        b, a = self._b, self._a
        x1, x2, y1, y2 = self._x1, self._x2, self._y1, self._y2

        for i, xn in enumerate(x):
            yn = b[0] * xn + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
            out[i] = yn
            x2, x1 = x1, xn
            y2, y1 = y1, yn

        self._x1, self._x2, self._y1, self._y2 = x1, x2, y1, y2

        if is_int16:
            return np.clip(out * 32768.0, -32768, 32767).astype(np.int16)
        return out.astype(np.float32)
