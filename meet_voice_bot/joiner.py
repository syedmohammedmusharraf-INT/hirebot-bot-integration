"""Per-bot lifecycle orchestration. Implements plan section 5, Phases 1-3.

``MeetBotSession`` ties together the pieces that make up one running bot: URL
canonicalization, LiveKit room/token/dispatch (control.py), the Meet<->
LiveKit audio bridge (livekit_bridge.py) and the vendored Selenium adapter
(google_meet_bot_adapter/). It never talks to Selenium/CDP or LiveKit's wire
protocol directly -- it only calls the public methods those modules expose.

**Minimal setup (``LIVEKIT_ENABLED=false``, the current default):** the
LiveKit solution (control.RoomController and livekit_bridge.MeetAudioBridge)
is skipped entirely -- not removed, just not called -- and
``audio_probe.AudioCaptureProbe`` runs in its place so the Meet-join and the
Meet -> bot audio *capture* path can be verified end to end (with logging)
without a LiveKit server. Set ``LIVEKIT_ENABLED=true`` (and fill in
``LIVEKIT_URL``/``LIVEKIT_API_KEY``/``LIVEKIT_API_SECRET``) to turn the full
room/token/dispatch/bridge flow back on -- see README "Minimal setup".
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Union

from meet_voice_bot.audio_probe import AudioCaptureProbe
from meet_voice_bot.config import Settings
from meet_voice_bot.control import RoomController, RoomGrant
from meet_voice_bot.google_meet_bot_adapter.google_meet_bot_adapter import GoogleMeetBotAdapter
from meet_voice_bot.livekit_bridge import MeetAudioBridge
from meet_voice_bot.meet_url import canonicalize_meet_url
from meet_voice_bot.models import BotSession, BotStatus
from meet_voice_bot.web_bot_adapter.exceptions import UiRetryableException

logger = logging.getLogger(__name__)


class MeetBotSession:
    """Orchestrates one bot end to end.

    ``start()`` canonicalizes the URL and creates the LiveKit room + tokens +
    agent dispatch synchronously (fast, so a caller such as ``main.py`` can
    await it via ``asyncio.to_thread`` and return ``room_name`` in the
    initial HTTP response), then hands the rest of the flow -- starting the
    audio bridge, joining the Meet call and monitoring until leave -- to a
    background thread.
    """

    def __init__(self, *, bot_id: str, meeting_url: str, assistant_ref: str, settings: Settings) -> None:
        self._bot_id = bot_id
        self._raw_meeting_url = meeting_url
        self._assistant_ref = assistant_ref
        self._settings = settings

        self._lock = threading.Lock()
        self._session = BotSession(
            bot_id=bot_id,
            meeting_url=meeting_url,
            room_name="",
            assistant_ref=assistant_ref,
            status=BotStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )

        self._room_grant: RoomGrant | None = None
        self._adapter: GoogleMeetBotAdapter | None = None
        self._bridge: Union[MeetAudioBridge, AudioCaptureProbe, None] = None
        self._thread: threading.Thread | None = None

        self._leave_requested = threading.Event()
        self._natural_end = threading.Event()

    # Donor parity (attendee's repeatedly_attempt_to_join_meeting, minus the
    # SSO/login branches -- guest-only here): one cold-container first paint
    # on software rendering can exceed a single attempt's wait window, so
    # retry transient failures with a completely fresh driver.
    _MAX_JOIN_ATTEMPTS = 3
    _JOIN_RETRY_BACKOFF_SECONDS = 3

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        canonical_url = canonicalize_meet_url(self._raw_meeting_url)
        meeting_code = canonical_url.rsplit("/", 1)[-1]

        self._update_status(BotStatus.JOINING)

        if self._settings.livekit_enabled:
            self._room_grant = asyncio.run(self._create_room_and_dispatch(meeting_code))
            with self._lock:
                self._session.room_name = self._room_grant.room_name
        else:
            logger.info(
                "bot=%s LIVEKIT_ENABLED=false -- skipping room create/token mint/agent dispatch; "
                "joining Meet and running audio_probe only (see README 'Minimal setup')",
                self._bot_id,
            )

        self._thread = threading.Thread(target=self._run_meeting, args=(canonical_url,), daemon=True, name=f"meet-bot-{self._bot_id}")
        self._thread.start()

    def request_leave(self) -> None:
        self._leave_requested.set()

    def capture_screenshot(self, path: str) -> bool:
        """Screenshot the bot's browser tab (False if the driver isn't up).
        Served via GET /meet-bots/{id}/screenshot for live tracing."""
        adapter = self._adapter
        if adapter is None:
            return False
        try:
            return adapter.capture_screenshot(path)
        except Exception:
            logger.exception("bot=%s screenshot failed", self._bot_id)
            return False

    def status(self) -> BotSession:
        with self._lock:
            return replace(self._session)

    # -- control-plane calls (own short-lived event loops) -----------------

    async def _create_room_and_dispatch(self, meeting_code: str) -> RoomGrant:
        async with RoomController(
            livekit_url=self._settings.livekit_url,
            api_key=self._settings.livekit_api_key,
            api_secret=self._settings.livekit_api_secret,
        ) as controller:
            grant = await controller.create_room_and_tokens(bot_id=self._bot_id, meeting_code=meeting_code)
            await controller.dispatch_agent(
                room_name=grant.room_name,
                assistant_ref=self._assistant_ref,
                agent_name=self._settings.agent_name,
            )
            return grant

    async def _delete_room(self, room_name: str) -> None:
        async with RoomController(
            livekit_url=self._settings.livekit_url,
            api_key=self._settings.livekit_api_key,
            api_secret=self._settings.livekit_api_secret,
        ) as controller:
            await controller.delete_room(room_name)

    # -- background thread: join + monitor + cleanup ------------------------

    def _run_meeting(self, canonical_url: str) -> None:
        try:
            if self._settings.livekit_enabled:
                assert self._room_grant is not None
                # The bridge's playback path calls straight into the
                # adapter's fake-mic bridge (send_raw_audio) rather than
                # through PulseAudio -- see livekit_bridge.py's module
                # docstring for why. _send_raw_audio_safe resolves the
                # *current* adapter at call time because _join_with_retries
                # recreates the adapter per attempt; send_raw_audio no-ops
                # until that attempt's driver is initialized.
                self._bridge = MeetAudioBridge(self._room_grant, on_agent_audio_frame=self._send_raw_audio_safe)
            else:
                # Minimal setup: no LiveKit room to publish into, so just
                # verify -- with logging -- that Meet's audio is reaching
                # this container's PulseAudio capture device. See
                # audio_probe.py.
                self._bridge = AudioCaptureProbe(
                    bot_id=self._bot_id,
                    log_interval_seconds=self._settings.audio_probe_log_interval_seconds,
                    silence_threshold_dbfs=self._settings.audio_probe_silence_threshold_dbfs,
                )
            self._bridge.start()

            # The status listener is wired in _build_adapter (via
            # on_status_change) before each attempt's init()/
            # attempt_to_join_meeting() run, so no early status transition
            # (e.g. an immediate "removed") can be missed.
            self._join_with_retries(canonical_url)

            self._update_status(BotStatus.IN_MEETING)
            self._monitor_until_leave()
        except Exception as exc:
            logger.exception("MeetBotSession %s failed", self._bot_id)
            with self._lock:
                self._session.error = str(exc)
            self._update_status(BotStatus.ERROR)
        finally:
            self._cleanup()

    def _send_raw_audio_safe(self, pcm_bytes: bytes, sample_rate: int) -> None:
        """Bridge callback that tolerates adapter swaps between join retries
        (the adapter object is recreated per attempt)."""
        adapter = self._adapter
        if adapter is not None:
            adapter.send_raw_audio(pcm_bytes, sample_rate)

    def _build_adapter(self, canonical_url: str) -> GoogleMeetBotAdapter:
        return GoogleMeetBotAdapter(
            meeting_url=canonical_url,
            bot_name=self._settings.meet_bot_name,
            chrome_binary_path=self._settings.chrome_binary_path,
            chromedriver_path=self._settings.chromedriver_path,
            wait_for_host_timeout_seconds=self._settings.wait_for_host_timeout_seconds,
            waiting_room_timeout_seconds=self._settings.waiting_room_timeout_seconds,
            on_status_change=self._on_adapter_status_change,
        )

    def _join_with_retries(self, canonical_url: str) -> None:
        """Up to _MAX_JOIN_ATTEMPTS joins, each with a fresh driver. Only
        UiRetryableException (missing element, click failure, transient
        Google block, alternate audio config) is retried -- denial,
        meeting-not-found and host/waiting-room timeouts fail immediately."""
        last_exc: Exception | None = None
        for attempt in range(1, self._MAX_JOIN_ATTEMPTS + 1):
            self._adapter = self._build_adapter(canonical_url)
            try:
                self._adapter.init()
                self._adapter.attempt_to_join_meeting()
                return
            except UiRetryableException as exc:
                last_exc = exc
                logger.warning("bot=%s join attempt %d/%d failed (retryable): %s", self._bot_id, attempt, self._MAX_JOIN_ATTEMPTS, exc)
            except Exception:
                raise
            try:
                self._adapter.teardown_driver()
            except Exception:
                logger.exception("bot=%s failed to tear down driver after attempt %d", self._bot_id, attempt)
            self._adapter = None
            if attempt < self._MAX_JOIN_ATTEMPTS:
                time.sleep(self._JOIN_RETRY_BACKOFF_SECONDS)
        assert last_exc is not None
        raise last_exc

    def _on_adapter_status_change(self, status: str, extra: dict) -> None:
        logger.info("bot=%s adapter status=%s extra=%s", self._bot_id, status, extra)
        # "removed" (kicked/removed_from_meeting) and "meeting_ended" (host
        # ended it / everyone left) are the only genuine leave signals the
        # adapter emits for an already-admitted bot -- see
        # google_meet_bot_adapter.py::handle_websocket_message's docstring
        # for why "not_in_meeting" is deliberately NOT one of them (it's the
        # bot's normal status while still waiting to be admitted, not a
        # leave signal; treating it as one made the bot leave the instant
        # it was admitted).
        if status in ("removed", "meeting_ended"):
            self._natural_end.set()

    def _monitor_until_leave(self) -> None:
        deadline = time.monotonic() + self._settings.max_uptime_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.info("bot=%s hit MAX_UPTIME_SECONDS", self._bot_id)
                return
            if self._leave_requested.wait(timeout=min(remaining, 1.0)):
                return
            if self._natural_end.is_set():
                return

    def _cleanup(self) -> None:
        """Phase 3 shutdown order: stop bridge -> disconnect room ->
        adapter.leave() -> adapter.teardown_driver()."""
        self._update_status(BotStatus.LEAVING)

        if self._bridge is not None:
            try:
                self._bridge.stop()
            except Exception:
                logger.exception("bot=%s failed to stop audio bridge", self._bot_id)

        if self._room_grant is not None:
            try:
                asyncio.run(self._delete_room(self._room_grant.room_name))
            except Exception:
                logger.exception("bot=%s failed to delete LiveKit room", self._bot_id)

        if self._adapter is not None:
            try:
                self._adapter.leave()
            except Exception:
                logger.exception("bot=%s failed to leave meeting cleanly", self._bot_id)
            try:
                self._adapter.teardown_driver()
            except Exception:
                logger.exception("bot=%s failed to tear down driver", self._bot_id)

        with self._lock:
            if self._session.status != BotStatus.ERROR:
                self._session.status = BotStatus.ENDED

    def _update_status(self, status: BotStatus) -> None:
        with self._lock:
            self._session.status = status
