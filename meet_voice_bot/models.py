"""In-process bot session state. Implements plan section 5, Phase 0."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class BotStatus(str, Enum):
    PENDING = "pending"
    JOINING = "joining"
    IN_MEETING = "in_meeting"
    LEAVING = "leaving"
    ENDED = "ended"
    ERROR = "error"


@dataclass
class BotSession:
    bot_id: str
    meeting_url: str
    room_name: str
    assistant_ref: str
    status: BotStatus
    created_at: datetime
    error: str | None = None
