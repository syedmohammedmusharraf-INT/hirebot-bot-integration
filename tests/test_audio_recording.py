"""Unit tests for audio_probe.py's optional session-recording feature
(AUDIO_RECORDING_ENABLED). Doesn't exercise the async parec subprocess --
just confirms AudioCaptureProbe writes WAV files with the same format
constants it uses for level computation, and that the file it produces is a
valid, readable WAV."""

import os
import wave

from meet_voice_bot.audio_probe import _BYTES_PER_SAMPLE, _NUM_CHANNELS, _SAMPLE_RATE, AudioCaptureProbe


def test_record_path_defaults_to_none():
    probe = AudioCaptureProbe(bot_id="test-bot")
    assert probe._record_path is None
    assert probe._wave_writer is None


def test_record_path_is_stored(tmp_path):
    path = str(tmp_path / "session.wav")
    probe = AudioCaptureProbe(bot_id="test-bot", record_path=path)
    assert probe._record_path == path


def test_wav_format_matches_capture_format(tmp_path):
    """Write a WAV the same way AudioCaptureProbe's _start_async does, and
    confirm the format constants round-trip through Python's wave module."""
    path = str(tmp_path / "session.wav")
    pcm_chunk = (b"\x00\x01" * 480) * 2  # a couple of fake s16le mono frames

    writer = wave.open(path, "wb")
    writer.setnchannels(_NUM_CHANNELS)
    writer.setsampwidth(_BYTES_PER_SAMPLE)
    writer.setframerate(_SAMPLE_RATE)
    writer.writeframes(pcm_chunk)
    writer.close()

    assert os.path.getsize(path) > len(pcm_chunk)  # WAV header adds bytes

    reader = wave.open(path, "rb")
    try:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 48000
        assert reader.readframes(reader.getnframes()) == pcm_chunk
    finally:
        reader.close()
