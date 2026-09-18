"""Room creation, token minting and agent dispatch. Implements plan section
4.4 ("Room + token flow (local livekit-api, not web_call/get_token)").

Room name: ``meet_{meeting_code}_{bot_id}``. Two tokens are minted per bot: a
publisher identity (publish-only, carries the Meet audio into the room) and a
hidden subscriber identity (subscribe-only, carries the agent's answer track
back out). Both are minted locally with the ``livekit-api`` ergonomic token
builder (HS256 under the hood) rather than a round trip to any server -- the
same "mint your own JWT" shape the donor's ``LivekitRoomSyncClient`` uses for
its per-participant tokens, just built with the SDK's ``AccessToken`` class
instead of hand-rolled PyJWT claims.

SDK surface used here targets ``livekit-api>=0.8`` (bundled by the
``livekit`` PyPI package, pinned at 1.1.8 in the donor app). Method names on
``AgentDispatchClient`` verified against that version; if a newer/older
``livekit-api`` renames them, this is the one place to fix.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from livekit import api

logger = logging.getLogger(__name__)

_ROOM_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")

# Per plan section 3: publisher captures Meet audio into the room, hidden
# subscriber carries the agent's answer track back out to Meet.
_TOKEN_TTL = timedelta(hours=6)


@dataclass(frozen=True)
class RoomGrant:
    room_name: str
    publisher_token: str
    subscriber_token: str
    livekit_url: str


def _sanitize_room_component(value: str) -> str:
    return _ROOM_NAME_SAFE_RE.sub("-", value)


def room_name_for(bot_id: str, meeting_code: str) -> str:
    return f"meet_{_sanitize_room_component(meeting_code)}_{_sanitize_room_component(bot_id)}"


class RoomController:
    """Owns the LiveKit server-side API client for one process.

    Use as an async context manager, or call :meth:`aclose` explicitly when
    done, to release the underlying HTTP session.
    """

    def __init__(self, *, livekit_url: str, api_key: str, api_secret: str) -> None:
        self._url = livekit_url
        self._api_key = api_key
        self._api_secret = api_secret
        self._lkapi = api.LiveKitAPI(livekit_url, api_key, api_secret)

    async def __aenter__(self) -> "RoomController":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._lkapi.aclose()

    def _mint_token(self, *, identity: str, room_name: str, can_publish: bool, can_subscribe: bool, hidden: bool) -> str:
        grants = api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_subscribe=can_subscribe,
            can_publish_data=can_publish,
            hidden=hidden,
        )
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_ttl(_TOKEN_TTL)
            .with_grants(grants)
            .to_jwt()
        )

    async def create_room_and_tokens(self, *, bot_id: str, meeting_code: str) -> RoomGrant:
        """Create the LiveKit room for this bot and mint its two tokens."""
        room_name = room_name_for(bot_id, meeting_code)

        await self._lkapi.room.create_room(api.CreateRoomRequest(name=room_name))
        logger.info("Created LiveKit room %s", room_name)

        publisher_token = self._mint_token(
            identity=f"meet-bot-pub-{bot_id}",
            room_name=room_name,
            can_publish=True,
            can_subscribe=False,
            hidden=False,
        )
        subscriber_token = self._mint_token(
            identity=f"meet-bot-sub-{bot_id}",
            room_name=room_name,
            can_publish=False,
            can_subscribe=True,
            hidden=True,
        )

        return RoomGrant(
            room_name=room_name,
            publisher_token=publisher_token,
            subscriber_token=subscriber_token,
            livekit_url=self._url,
        )

    async def dispatch_agent(self, *, room_name: str, assistant_ref: str, agent_name: str) -> None:
        """Explicitly dispatch the voice agent worker into ``room_name``.

        ``assistant_ref`` is passed through as JSON dispatch metadata so the
        agent worker (a separate process/image, see meet-agent/agent_run.py)
        can read ``JobContext.job.metadata`` and load the matching local
        assistant record itself -- the control plane never talks to the STT
        /LLM/TTS providers directly.
        """
        metadata = json.dumps({"assistant_ref": assistant_ref})
        await self._lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=agent_name,
                room=room_name,
                metadata=metadata,
            )
        )
        logger.info("Dispatched agent %s into room %s (assistant_ref=%s)", agent_name, room_name, assistant_ref)

    async def delete_room(self, room_name: str) -> None:
        """Best-effort room teardown; failures are logged, never raised."""
        try:
            await self._lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))
            logger.info("Deleted LiveKit room %s", room_name)
        except Exception:
            logger.exception("Failed to delete LiveKit room %s (continuing cleanup)", room_name)
