"""Minimal-setup audio-capture verification probe (no LiveKit).

While ``LIVEKIT_ENABLED=false`` (the current default -- see README "Minimal
setup"), ``meet_voice_bot/joiner.py`` uses this instead of
``livekit_bridge.MeetAudioBridge``. It reads the exact same
``auto_null.monitor`` PulseAudio source the LiveKit bridge's capture half
would read (see ``livekit_bridge.py``'s module docstring) via ``parec``, and
-- instead of publishing frames into a LiveKit room -- computes and logs a
running audio level, so the question "is the Meet participant's audio
actually reaching this container" is answerable from logs alone, with no
LiveKit server involved. LiveKit itself is untouched by this module; it is
only not called while disabled.

Implements the same ``start()``/``stop()`` shape as ``MeetAudioBridge`` so
``joiner.py`` can use either one interchangeably.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
import wave
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_NUM_CHANNELS = 1
_BYTES_PER_SAMPLE = 2  # s16le

_CAPTURE_CMD = [
    "parec",
    "--raw",
    "--format=s16le",
    f"--rate={_SAMPLE_RATE}",
    f"--channels={_NUM_CHANNELS}",
    "--device=auto_null.monitor",
]


def compute_dbfs(chunk: bytes) -> float:
    """RMS level of a little-endian int16 mono PCM chunk, in dBFS (0 dBFS is
    full scale). Returns ``-inf`` for empty/all-zero (true silence) input."""
    if not chunk:
        return float("-inf")
    samples = np.frombuffer(chunk, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return float("-inf")
    rms = math.sqrt(float(np.mean(samples**2)))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32768.0)


class AudioCaptureProbe:
    """Logs Meet's captured audio level on a fixed interval: an explicit
    "audio DETECTED" line the first time a window crosses the silence
    threshold, a periodic level line every ``log_interval_seconds``, and a
    warning if nothing crosses the threshold within
    ``no_signal_warning_seconds`` of starting (repeated every
    ``no_signal_warning_seconds`` after that) -- so a capture-path problem
    (wrong Pulse device, Meet tab muted, bot never actually joined) shows up
    in logs instead of silently doing nothing.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        log_interval_seconds: float = 1.0,
        silence_threshold_dbfs: float = -50.0,
        no_signal_warning_seconds: float = 15.0,
        record_path: Optional[str] = None,
    ) -> None:
        self._bot_id = bot_id
        self._log_interval_seconds = log_interval_seconds
        self._silence_threshold_dbfs = silence_threshold_dbfs
        self._no_signal_warning_seconds = no_signal_warning_seconds
        # When set, every raw byte read from parec is also written here as a
        # standard PCM WAV file (mono/48kHz/16-bit) -- the *entire* captured
        # stream for the session, not just the leveled windows used for
        # logging, so you can play back exactly what the bot heard.
        self._record_path = record_path
        self._wave_writer: Optional[wave.Wave_write] = None

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_event_loop, name=f"audio-probe-{bot_id}", daemon=True)

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    # -- lifecycle -----------------------------------------------------

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coroutine(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def start(self) -> None:
        """Start the probe. Unlike ``MeetAudioBridge.start()`` this doesn't
        block on any network connection -- there is none, LiveKit is
        disabled -- it just launches ``parec`` on the background loop."""
        self._thread.start()
        future = self._run_coroutine(self._start_async())
        future.result(timeout=10)
        logger.info(
            "bot=%s audio_probe started (LIVEKIT_ENABLED=false) -- watching auto_null.monitor for Meet participant audio, logging level every %.1fs",
            self._bot_id,
            self._log_interval_seconds,
        )

    async def _start_async(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *_CAPTURE_CMD,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if self._record_path:
            os.makedirs(os.path.dirname(self._record_path) or ".", exist_ok=True)
            self._wave_writer = wave.open(self._record_path, "wb")
            self._wave_writer.setnchannels(_NUM_CHANNELS)
            self._wave_writer.setsampwidth(_BYTES_PER_SAMPLE)
            self._wave_writer.setframerate(_SAMPLE_RATE)
            logger.info("bot=%s audio_probe recording full session audio to %s", self._bot_id, self._record_path)
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        window_bytes = int(_SAMPLE_RATE * _NUM_CHANNELS * _BYTES_PER_SAMPLE * self._log_interval_seconds)
        buffer = bytearray()
        voice_active = False
        ever_detected_voice = False
        started_at = time.monotonic()
        last_warning_at = started_at

        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    logger.info("bot=%s audio_probe: capture stream ended", self._bot_id)
                    self._close_wave_writer()
                    return
                if self._wave_writer is not None:
                    try:
                        self._wave_writer.writeframes(chunk)
                    except Exception:
                        logger.exception("bot=%s audio_probe: failed writing to recording file; disabling recording for the rest of this session", self._bot_id)
                        self._wave_writer = None
                buffer.extend(chunk)

                if len(buffer) < window_bytes:
                    continue

                window = bytes(buffer[:window_bytes])
                del buffer[:window_bytes]

                level_dbfs = compute_dbfs(window)
                is_voice = level_dbfs > self._silence_threshold_dbfs

                if is_voice:
                    ever_detected_voice = True
                if is_voice and not voice_active:
                    logger.info(
                        "bot=%s audio_probe: Meet audio DETECTED (level=%.1f dBFS >= threshold=%.1f dBFS) -- "
                        "capture path (Meet tab -> PulseAudio auto_null.monitor -> bot) confirmed working",
                        self._bot_id,
                        level_dbfs,
                        self._silence_threshold_dbfs,
                    )
                elif not is_voice and voice_active:
                    logger.info("bot=%s audio_probe: back to silence (level=%.1f dBFS)", self._bot_id, level_dbfs)
                voice_active = is_voice

                logger.info(
                    "bot=%s audio_probe level=%.1fdBFS bytes=%d window=%.1fs state=%s",
                    self._bot_id,
                    level_dbfs,
                    len(window),
                    self._log_interval_seconds,
                    "VOICE" if is_voice else "silence",
                )

                now = time.monotonic()
                should_warn = not ever_detected_voice and (now - started_at) >= self._no_signal_warning_seconds and (now - last_warning_at) >= self._no_signal_warning_seconds
                if should_warn:
                    logger.warning(
                        "bot=%s audio_probe: no audio above %.1f dBFS detected in the last %.0fs -- check that "
                        "Meet's tab is unmuted/audible, PulseAudio's default sink/source are auto_null/"
                        "auto_null.monitor (see docker/entrypoint.sh), and the bot has actually joined the call",
                        self._bot_id,
                        self._silence_threshold_dbfs,
                        now - started_at,
                    )
                    last_warning_at = now
        except asyncio.CancelledError:
            self._close_wave_writer()
            raise
        except Exception:
            logger.exception("bot=%s audio_probe pipeline failed", self._bot_id)
            self._close_wave_writer()

    def _close_wave_writer(self) -> None:
        if self._wave_writer is None:
            return
        try:
            self._wave_writer.close()
            logger.info("bot=%s audio_probe: recording saved to %s", self._bot_id, self._record_path)
        except Exception:
            logger.exception("bot=%s audio_probe: error closing recording file", self._bot_id)
        finally:
            self._wave_writer = None

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            future = self._run_coroutine(self._stop_async())
            future.result(timeout=10)
        except Exception:
            logger.exception("bot=%s error while stopping audio_probe", self._bot_id)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
        logger.info("bot=%s audio_probe stopped", self._bot_id)

    async def _stop_async(self) -> None:
        if self._task:
            self._task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
