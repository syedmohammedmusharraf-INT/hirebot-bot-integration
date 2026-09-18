"""Agent worker entrypoint (plan section 4.2, section 5 Phase 0/2/3).

Runnable as ``python agent_run.py dev`` locally or
``python -m livekit.agents start agent_run.py`` per the plan's deployment model.

Env vars: ASSISTANT_REF (default "meet-voice-bot-v1"), ASSISTANTS_DIR (default
"/app/assistants", expects "{ref}.yaml" files), AGENT_NAME (default
"meet-voice-agent").
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import yaml
from livekit import agents, rtc
from livekit.agents import JobContext, WorkerOptions

from model_support.assistant_config import InvalidAssistantConfig, load_assistant_config
from session_cfg import build_session
from usage import UsageTracker, run_periodic_snapshots

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

DEFAULT_ASSISTANT_REF = os.environ.get("ASSISTANT_REF", "meet-voice-bot-v1")
ASSISTANTS_DIR = os.environ.get("ASSISTANTS_DIR", "/app/assistants")

ANSWER_READY_TIMEOUT_SEC = 10.0
RTP_WARMUP_SEC = 1.0
DRAIN_TIMEOUT_SEC = 4.0


def _load_assistant_ref_from_job(ctx: JobContext) -> str:
    metadata = getattr(ctx.job, "metadata", None)
    if not metadata:
        return DEFAULT_ASSISTANT_REF
    try:
        return json.loads(metadata).get("assistant_ref", DEFAULT_ASSISTANT_REF)
    except (json.JSONDecodeError, AttributeError):
        logger.warning("Could not parse job metadata as JSON; falling back to default assistant_ref")
        return DEFAULT_ASSISTANT_REF


def _load_local_assistant_config(assistant_ref: str):
    # Duplicated (deliberately) from the control-plane's assistant_store.py:
    # this image doesn't ship that module, and the schema is small enough that
    # keeping two ~8-line loaders in sync is cheaper than a cross-image import.
    path = os.path.join(ASSISTANTS_DIR, f"{assistant_ref}.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return load_assistant_config(data)


async def _wait_for_answer_ready(ctx: JobContext, timeout_sec: float) -> None:
    """Resolve once the bot's publisher participant's audio track is subscribed.

    Registered before session.start() per plan section 4.2: tool load and TTS
    prewarm can take seconds, and track/data-channel events aren't replayed to
    listeners that subscribe late.
    """
    ready = asyncio.Event()

    def _on_track_subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            ready.set()

    ctx.room.on("track_subscribed", _on_track_subscribed)

    for participant in ctx.room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.kind == rtc.TrackKind.KIND_AUDIO and publication.subscribed:
                ready.set()

    try:
        await asyncio.wait_for(ready.wait(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning("Timed out after %.1fs waiting for the bot's publisher audio track; starting session anyway", timeout_sec)
    finally:
        ctx.room.off("track_subscribed", _on_track_subscribed)


async def entrypoint(ctx: JobContext) -> None:
    hangup = asyncio.Event()
    ctx.room.on("disconnected", lambda *_args: hangup.set())

    # Recording confirmation (plan section 4.2's Exotel-derived gating list) is
    # owned by the Meet-joining bot process, not this LiveKit worker, so it has
    # no equivalent wait here.
    answer_ready_task = asyncio.create_task(_wait_for_answer_ready(ctx, ANSWER_READY_TIMEOUT_SEC))
    hangup_wait_task = asyncio.create_task(hangup.wait())

    await ctx.connect()

    assistant_ref = _load_assistant_ref_from_job(ctx)
    try:
        assistant_cfg = _load_local_assistant_config(assistant_ref)
    except (OSError, InvalidAssistantConfig):
        logger.exception("Failed to load assistant config %r", assistant_ref)
        raise

    session = await build_session(ctx, assistant_cfg)
    usage_tracker = UsageTracker()

    done, pending = await asyncio.wait({answer_ready_task, hangup_wait_task}, return_when=asyncio.FIRST_COMPLETED)

    if hangup_wait_task in done:
        logger.info("Room disconnected before the bot's audio track was ready; aborting session start")
        for task in pending:
            task.cancel()
        return

    await asyncio.sleep(RTP_WARMUP_SEC)

    await session.start(room=ctx.room)

    if getattr(session, "speaks_first", False):
        session.generate_reply()

    snapshot_task = asyncio.create_task(run_periodic_snapshots(usage_tracker, session))

    await hangup.wait()

    snapshot_task.cancel()

    try:
        await asyncio.wait_for(session.commit_user_turn(skip_reply=True), timeout=DRAIN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning("commit_user_turn(skip_reply=True) drain timed out after %.1fs", DRAIN_TIMEOUT_SEC)
    except Exception:
        logger.exception("Error draining STT tail at session end")

    await usage_tracker.finalize(session)


def build_worker_options() -> WorkerOptions:
    return WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name=os.environ.get("AGENT_NAME", "meet-voice-agent"),
    )


if __name__ == "__main__":
    agents.cli.run_app(build_worker_options())
