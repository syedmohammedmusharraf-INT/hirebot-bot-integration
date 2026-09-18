"""Meet <-> LiveKit audio bridge. Implements plan section 5, Phase 2, step 8
("Bridge: publish Pulse-mixed PCM resampled 48 kHz mono; subscribe agent
track -> botOutputManager mic with mute mirroring; echo guard ... optional
lk.chat text") and section 2.1 ("PulseAudio decision").

The two directions use two different mechanisms, not one shared Pulse
loopback -- mixing them up would make the agent hear its own voice:

* **capture** (Meet -> LiveKit): the audible Chrome tab's mixed PCM is read
  from the ``auto_null.monitor`` PulseAudio source via ``parec`` (the
  standard ``pulseaudio-utils`` CLI) and published into the room. This is
  the only thing PulseAudio is used for here.
* **playback** (LiveKit -> Meet): the agent's answer track is *not* written
  back into PulseAudio (that sink is the same one ``parec`` above is
  monitoring, so doing so would immediately feed the agent's own speech
  back into its own capture pipeline). Instead it is handed to
  ``meet_voice_bot.google_meet_bot_adapter.GoogleMeetBotAdapter.send_raw_audio``,
  supplied by the caller as ``on_agent_audio_frame`` -- that method pushes
  the PCM into the injected ``window.botOutputManager.playPCMAudio`` fake-mic
  MediaStream track via Selenium ``execute_script``, which Chrome then
  transmits into the meeting as the bot's own microphone audio. See
  ``docker/entrypoint.sh`` for the PulseAudio bring-up and
  ``meet_voice_bot/web_bot_adapter/payload/shared_chromedriver_payload.js``
  for the fake-mic track.

Runs its own asyncio event loop on a dedicated background thread (same shape
as the donor's ``LivekitRoomSyncClient``: ``asyncio.new_event_loop()`` +
``run_coroutine_threadsafe`` bridges GLib/sync callers into async LiveKit
calls) so callers on the joiner's control thread never block on I/O.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from livekit import rtc

from meet_voice_bot.control import RoomGrant

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_NUM_CHANNELS = 1
_BYTES_PER_SAMPLE = 2  # s16le
_FRAME_MS = 20
_FRAME_BYTES = _SAMPLE_RATE * _NUM_CHANNELS * _BYTES_PER_SAMPLE * _FRAME_MS // 1000  # 1920 bytes
_CHAT_TOPIC = "lk.chat"

# Agent audio is batched to this size before crossing into Selenium
# (execute_script is an HTTP round trip to chromedriver; calling it once per
# ~20ms LiveKit frame would fall behind real time), so send_raw_audio is
# called roughly every _PLAYBACK_BATCH_MS instead of every frame.
_PLAYBACK_BATCH_MS = 100
_PLAYBACK_BATCH_BYTES = _SAMPLE_RATE * _NUM_CHANNELS * _BYTES_PER_SAMPLE * _PLAYBACK_BATCH_MS // 1000

_CAPTURE_CMD = [
    "parec",
    "--raw",
    "--format=s16le",
    f"--rate={_SAMPLE_RATE}",
    f"--channels={_NUM_CHANNELS}",
    "--device=auto_null.monitor",
]


class MeetAudioBridge:
    """Publishes Meet's audible output into a LiveKit room and plays the
    agent's answer track back into Meet's fake microphone."""

    def __init__(self, grant: RoomGrant, *, on_agent_audio_frame: Optional[Callable[[bytes, int], None]] = None) -> None:
        self._grant = grant
        # Called (off the event loop, via run_in_executor) with a batch of
        # little-endian int16 mono PCM and its sample rate whenever the
        # agent's answer track produces audio. Typically
        # ``GoogleMeetBotAdapter.send_raw_audio``; left unset only for tests.
        self._on_agent_audio_frame = on_agent_audio_frame

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_event_loop, name="meet-audio-bridge", daemon=True)

        self._publisher_room: Optional[rtc.Room] = None
        self._subscriber_room: Optional[rtc.Room] = None
        self._audio_source: Optional[rtc.AudioSource] = None

        self._capture_proc: Optional[asyncio.subprocess.Process] = None
        self._capture_task: Optional[asyncio.Task] = None
        self._playback_tasks: dict[str, asyncio.Task] = {}

        self._started = threading.Event()
        self._stopped = False

    # -- lifecycle -----------------------------------------------------

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coroutine(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def start(self) -> None:
        """Connect both rooms and start the capture/playback pipelines.

        Blocks the calling (joiner control) thread until both LiveKit
        connections are established, then returns; audio flows on the
        background loop from then on.
        """
        self._thread.start()
        future = self._run_coroutine(self._start_async())
        future.result(timeout=30)
        self._started.set()

    async def _start_async(self) -> None:
        self._publisher_room = rtc.Room()
        await self._publisher_room.connect(
            self._grant.livekit_url,
            self._grant.publisher_token,
            options=rtc.RoomOptions(auto_subscribe=False),
        )

        self._audio_source = rtc.AudioSource(_SAMPLE_RATE, _NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("meet-audio", self._audio_source)
        await self._publisher_room.local_participant.publish_track(
            track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        self._capture_task = asyncio.ensure_future(self._run_capture())

        self._subscriber_room = rtc.Room()
        own_publisher_identity = self._publisher_room.local_participant.identity

        @self._subscriber_room.on("track_subscribed")
        def _on_track_subscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if participant.identity == own_publisher_identity:
                # Echo guard: never feed our own captured Meet audio back
                # into the fake mic -- only the agent's synthesized answer
                # track should ever reach send_raw_audio.
                logger.warning("Ignoring own publisher identity %s subscribed as audio source", participant.identity)
                return
            self._playback_tasks[publication.sid] = asyncio.ensure_future(self._run_playback(track, publication.sid))

        @self._subscriber_room.on("track_unsubscribed")
        def _on_track_unsubscribed(track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
            task = self._playback_tasks.pop(publication.sid, None)
            if task:
                task.cancel()

        await self._subscriber_room.connect(
            self._grant.livekit_url,
            self._grant.subscriber_token,
            options=rtc.RoomOptions(auto_subscribe=True),
        )

        logger.info("MeetAudioBridge connected: publisher=%s subscriber=%s room=%s", own_publisher_identity, self._subscriber_room.local_participant.identity, self._grant.room_name)

    # -- capture: Meet (Pulse monitor) -> LiveKit room ------------------

    async def _run_capture(self) -> None:
        try:
            self._capture_proc = await asyncio.create_subprocess_exec(
                *_CAPTURE_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert self._capture_proc.stdout is not None
            while True:
                chunk = await self._capture_proc.stdout.readexactly(_FRAME_BYTES)
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    samples_per_channel=_FRAME_BYTES // (_BYTES_PER_SAMPLE * _NUM_CHANNELS),
                )
                await self._audio_source.capture_frame(frame)
        except asyncio.IncompleteReadError:
            logger.info("parec capture stream ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Meet audio capture pipeline failed")

    # -- playback: agent answer track -> Meet (fake-mic bridge) ----------

    async def _run_playback(self, track: rtc.Track, publication_sid: str) -> None:
        if self._on_agent_audio_frame is None:
            logger.warning("No on_agent_audio_frame callback configured; dropping agent audio track %s", publication_sid)
            return

        buffer = bytearray()
        try:
            audio_stream = rtc.AudioStream(track, sample_rate=_SAMPLE_RATE, num_channels=_NUM_CHANNELS)
            async for event in audio_stream:
                buffer.extend(bytes(event.frame.data))
                if len(buffer) >= _PLAYBACK_BATCH_BYTES:
                    await self._flush_playback_buffer(buffer)
            if buffer:
                await self._flush_playback_buffer(buffer)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Agent answer playback pipeline failed")

    async def _flush_playback_buffer(self, buffer: bytearray) -> None:
        pcm_bytes = bytes(buffer)
        buffer.clear()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._on_agent_audio_frame, pcm_bytes, _SAMPLE_RATE)
        except Exception:
            logger.exception("on_agent_audio_frame callback failed")

    # -- optional text side-channel --------------------------------------

    def send_chat(self, text: str) -> None:
        """Publish ``text`` on the ``lk.chat`` topic from the publisher room."""
        if self._publisher_room is None:
            logger.warning("send_chat called before bridge started; dropping message")
            return
        self._run_coroutine(self._send_chat_async(text))

    async def _send_chat_async(self, text: str) -> None:
        try:
            await self._publisher_room.local_participant.send_text(text, topic=_CHAT_TOPIC)
        except Exception:
            logger.exception("Failed to send lk.chat message")

    # -- teardown ---------------------------------------------------------

    @staticmethod
    async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()

    def stop(self) -> None:
        """Stop capture/playback, disconnect both rooms, stop the loop.

        Mirrors the donor's ``LivekitRoomSyncClient.cleanup()``: run the
        async teardown with a bounded timeout, then always stop the loop and
        join the thread so the process can exit cleanly even if teardown
        hangs.
        """
        if self._stopped:
            return
        self._stopped = True
        try:
            future = self._run_coroutine(self._stop_async())
            future.result(timeout=10)
        except Exception:
            logger.exception("Error while stopping MeetAudioBridge")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)

    async def _stop_async(self) -> None:
        if self._capture_task:
            self._capture_task.cancel()
        if self._capture_proc:
            await self._terminate_process(self._capture_proc)

        for task in list(self._playback_tasks.values()):
            task.cancel()
        self._playback_tasks.clear()

        if self._publisher_room:
            await self._publisher_room.disconnect()
        if self._subscriber_room:
            await self._subscriber_room.disconnect()
