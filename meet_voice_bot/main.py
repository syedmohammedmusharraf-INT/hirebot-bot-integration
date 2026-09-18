"""Control-plane HTTP API. Implements plan section 6:

    POST /meet-bots {meeting_url, assistant_ref?}
      -> validate + canonicalize -> control creates room + mints tokens +
         dispatches agent -> launch joiner with {room_name, token}
      -> return {bot_id, room_name}

With ``LIVEKIT_ENABLED=false`` (the current default -- see README "Minimal
setup"), ``joiner.MeetBotSession`` skips the room/token/dispatch step
entirely and ``room_name`` in the response is ``""``; the bot still joins
Meet and runs ``audio_probe.AudioCaptureProbe`` to verify/log the Meet -> bot
audio capture path with no LiveKit server involved.

Run with: ``uvicorn meet_voice_bot.main:app --host 0.0.0.0 --port 8000``.
No database -- bot sessions live in an in-process registry for this v1,
matching the plan's "local, not MCP" scope (see README for the single
caveat this implies: state does not survive a control-process restart).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Python's root logger defaults to WARNING with no handler configured, and
# nothing else in this process ever called logging.basicConfig() -- so
# every logger.info() call across the whole app (adapter status changes,
# audio_probe's periodic capture-level lines, "Chrome driver started",
# "Joined meeting", etc.) was being silently dropped; only warning/error/
# exception calls and uvicorn's own request-access lines ever reached
# `docker logs`. This must run before any of this package's other modules
# are imported (they all do `logger = logging.getLogger(__name__)` at
# import time), so it's the first thing here, before those imports below.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

from meet_voice_bot.assistant_store import AssistantNotFoundError, InvalidAssistantConfig, load_assistant  # noqa: E402
from meet_voice_bot.config import get_settings  # noqa: E402
from meet_voice_bot.joiner import MeetBotSession  # noqa: E402
from meet_voice_bot.meet_url import InvalidMeetingUrlError  # noqa: E402
from meet_voice_bot.models import BotSession, BotStatus  # noqa: E402

# Non-terminal states: a session in one of these still has (or is about to
# have) a live Chrome/Xvfb process running.
_ACTIVE_BOT_STATUSES = {BotStatus.PENDING, BotStatus.JOINING, BotStatus.IN_MEETING, BotStatus.LEAVING}

logger = logging.getLogger(__name__)

app = FastAPI(title="Meet Voice Bot Control Plane")

# In-process bot registry. Fine for a single control replica; a multi-replica
# deployment would need to move this to Redis/Postgres, which is out of
# scope for this plan (see plan section 7, non-goals).
_sessions: dict[str, MeetBotSession] = {}


class CreateBotRequest(BaseModel):
    meeting_url: str
    assistant_ref: Optional[str] = None


class CreateBotResponse(BaseModel):
    bot_id: str
    room_name: str


def _serialize(session: BotSession) -> dict:
    return {
        "bot_id": session.bot_id,
        "meeting_url": session.meeting_url,
        "room_name": session.room_name,
        "assistant_ref": session.assistant_ref,
        "status": session.status.value,
        "created_at": session.created_at.isoformat(),
        "error": session.error,
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/meet-bots", response_model=CreateBotResponse, status_code=201)
async def create_meet_bot(request: CreateBotRequest) -> CreateBotResponse:
    settings = get_settings()
    assistant_ref = request.assistant_ref or settings.assistant_ref

    # One bot per container (plan section 7: shared Xvfb display/PulseAudio
    # server/websocket-bridge port would otherwise be split between multiple
    # concurrent Chrome instances -- observed in practice as later bots
    # never getting a working Chrome session, and the whole container
    # becoming unpredictable). Reject a second concurrent join instead of
    # silently colliding; scale by running more containers, not more
    # concurrent bots in one.
    active = [s for s in _sessions.values() if s.status().status in _ACTIVE_BOT_STATUSES]
    if active:
        busy = active[0].status()
        raise HTTPException(
            status_code=409,
            detail=f"This container already has an active bot session (bot_id={busy.bot_id}, status={busy.status.value}). "
            "One bot per container -- wait for it to finish or POST /meet-bots/{bot_id}/leave first, or run another container.",
        )

    # The assistant record only matters once the agent worker is dispatched,
    # i.e. only when LiveKit is enabled -- don't require one to exist (or be
    # valid) just to run the minimal, bot-only setup.
    if settings.livekit_enabled:
        try:
            load_assistant(assistant_ref, settings.assistants_dir)
        except AssistantNotFoundError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except InvalidAssistantConfig as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    bot_id = uuid.uuid4().hex[:12]
    session = MeetBotSession(
        bot_id=bot_id,
        meeting_url=request.meeting_url,
        assistant_ref=assistant_ref,
        settings=settings,
    )

    try:
        # start() blocks briefly (URL canonicalization, plus LiveKit room/
        # token/dispatch when enabled) then backgrounds the Meet join, so
        # room_name is always present in this response, per plan section 6
        # (empty string when LIVEKIT_ENABLED=false -- there is no room).
        await asyncio.to_thread(session.start)
    except InvalidMeetingUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to start bot %s", bot_id)
        raise HTTPException(status_code=502, detail=f"Failed to start bot: {exc}") from exc

    _sessions[bot_id] = session
    status = session.status()
    return CreateBotResponse(bot_id=bot_id, room_name=status.room_name)


@app.get("/meet-bots/{bot_id}")
async def get_meet_bot(bot_id: str) -> dict:
    session = _sessions.get(bot_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown bot_id {bot_id!r}")
    return _serialize(session.status())


@app.get("/meet-bots/{bot_id}/screenshot")
async def screenshot_meet_bot(bot_id: str):
    """Live view of what the bot's Chrome tab currently shows (PNG).

    Meet blocks headless Chrome, so the bot runs headed under Xvfb -- this
    endpoint is the equivalent of "watching" that virtual display. Refresh
    it in your browser while the bot is joining to see where it is stuck.
    409 while the browser isn't up yet / already torn down -- check
    GET /meet-bots/{bot_id} (status/error) in that case."""
    session = _sessions.get(bot_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown bot_id {bot_id!r}")
    path = os.path.join(tempfile.gettempdir(), f"meet-bot-{bot_id}.png")
    ok = await asyncio.to_thread(session.capture_screenshot, path)
    if not ok or not os.path.exists(path):
        raise HTTPException(
            status_code=409,
            detail="Browser not running (bot still starting, or already failed/left -- check GET /meet-bots/{bot_id})",
        )
    return FileResponse(path, media_type="image/png")


@app.post("/meet-bots/{bot_id}/leave", status_code=202)
async def leave_meet_bot(bot_id: str) -> dict:
    session = _sessions.get(bot_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"Unknown bot_id {bot_id!r}")
    session.request_leave()
    return {"bot_id": bot_id, "status": "leave_requested"}
