"""Usage accounting: 15s snapshots + authoritative teardown write (plan section 4.6).

Reads ``session.usage.model_usage`` (one entry per (provider, model) pair) and
stores the raw rows for pricing -- flat dashboard sums are computed by callers
from these, not by this module. Sarvam STT is self-measured locally because it
doesn't report usable plugin metrics; every other cascade provider's own
metrics are trusted as-is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from livekit.agents import AgentSession

logger = logging.getLogger(__name__)

SARVAM_STT_PROVIDER_KEY = "sarvam"


class UsageTracker:
    def __init__(self, on_snapshot: Optional[Callable[[dict], None]] = None):
        self.records: list[dict] = []
        self.on_snapshot = on_snapshot
        self.stt_audio_seconds: float = 0.0

    def add_stt_audio_seconds(self, seconds: float) -> None:
        """Accumulate locally-measured STT input duration, used for Sarvam self-measurement."""
        self.stt_audio_seconds += seconds

    def snapshot(self, session: AgentSession) -> dict:
        record = {
            "ts": time.time(),
            "model_usage": _read_model_usage(session),
            "usage_finalized": False,
        }
        self.records.append(record)
        if self.on_snapshot:
            self.on_snapshot(record)
        return record

    async def finalize(self, session: AgentSession) -> dict:
        model_usage = _read_model_usage(session)

        for row in model_usage:
            if row.get("provider") == SARVAM_STT_PROVIDER_KEY and row.get("kind") == "stt":
                logger.warning(
                    "Dropping Sarvam STT plugin metrics for %s; using locally-measured audio duration (%.2fs) instead to avoid double-counting",
                    row.get("model"),
                    self.stt_audio_seconds,
                )
                row["duration_seconds"] = self.stt_audio_seconds
                row["self_measured"] = True

        record = {
            "ts": time.time(),
            "model_usage": model_usage,
            "usage_finalized": True,
        }
        self.records.append(record)
        if self.on_snapshot:
            self.on_snapshot(record)
        return record


def _read_model_usage(session: AgentSession) -> list[dict]:
    usage = getattr(session, "usage", None)
    model_usage = getattr(usage, "model_usage", None) if usage is not None else None
    if not model_usage:
        return []
    return [dict(row) if isinstance(row, dict) else vars(row) for row in model_usage]


async def run_periodic_snapshots(tracker: UsageTracker, session: AgentSession, interval_sec: float = 15.0) -> None:
    try:
        while True:
            await asyncio.sleep(interval_sec)
            tracker.snapshot(session)
    except asyncio.CancelledError:
        pass
