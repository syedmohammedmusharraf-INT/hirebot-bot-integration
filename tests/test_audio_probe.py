"""Unit tests for the minimal-setup audio-capture level math (no subprocess,
no PulseAudio -- see meet_voice_bot/audio_probe.py)."""

import struct

from meet_voice_bot.audio_probe import compute_dbfs


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def test_silence_is_negative_infinity():
    assert compute_dbfs(_pcm([0] * 100)) == float("-inf")


def test_empty_chunk_is_negative_infinity():
    assert compute_dbfs(b"") == float("-inf")


def test_full_scale_square_wave_is_near_zero_dbfs():
    samples = [32767, -32768] * 50
    level = compute_dbfs(_pcm(samples))
    assert -1.0 < level <= 0.0


def test_quiet_signal_is_well_below_loud_signal():
    quiet = compute_dbfs(_pcm([100, -100] * 50))
    loud = compute_dbfs(_pcm([20000, -20000] * 50))
    assert quiet < loud
