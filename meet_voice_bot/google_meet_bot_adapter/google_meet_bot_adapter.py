"""Google Meet bot adapter: guest join only, no SAML/Okta.

Trimmed port of attendee/bots/google_meet_bot_adapter/google_meet_bot_adapter.py
(174 lines). Kept: the payload file list hook, the domain allowlist, and
Meet's roster/lifecycle websocket event mapping (``UsersUpdate`` /
``MeetingStatusChange`` -> ``on_status_change``). Dropped: closed-captions
language selection, Google-account login/Okta, video sending, chat-over-
Meet, and every ``subclass_specific_*`` hook the donor only needed for
recording/video-frame plumbing this system doesn't have.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from meet_voice_bot.web_bot_adapter.web_bot_adapter import STATUS_IN_MEETING, STATUS_MEETING_ENDED, STATUS_NOT_IN_MEETING, STATUS_REMOVED, WebBotAdapter

from .google_meet_ui_methods import GoogleMeetUIMethods

logger = logging.getLogger(__name__)

_PAYLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload")

# Domains Chrome is allowed to navigate to while joining as a guest. Ported
# from the donor's subclass_specific_domain_allowlist (google account login
# domains kept for parity even though this adapter never logs in -- Meet's
# guest flow can still bounce through accounts.google.com transiently).
DEFAULT_DOMAIN_ALLOWLIST = [
    "accounts.google.com",
    "meet.google.com",
    "www.google.com",
    ".gstatic.com",
    ".googleusercontent.com",
    ".apps.google.com",
]


class GoogleMeetBotAdapter(WebBotAdapter, GoogleMeetUIMethods):
    def __init__(
        self,
        *,
        meeting_url: str,
        bot_name: str,
        display_var: str = ":99",
        chrome_binary_path: str = "/usr/bin/google-chrome",
        chromedriver_path: str = "/usr/local/bin/chromedriver",
        wait_for_host_timeout_seconds: int = 600,
        waiting_room_timeout_seconds: int = 900,
        enforce_domain_allowlist: bool = True,
        on_status_change: Callable[[str, dict], None] | None = None,
    ) -> None:
        super().__init__(
            display_var=display_var,
            chrome_binary_path=chrome_binary_path,
            chromedriver_path=chromedriver_path,
            on_status_change=on_status_change,
        )
        self.meeting_url = meeting_url
        self.bot_name = bot_name
        self.wait_for_host_timeout_seconds = wait_for_host_timeout_seconds
        self.waiting_room_timeout_seconds = waiting_room_timeout_seconds
        # Accepted for API parity with the donor; currently unenforced (see
        # add_subclass_specific_chrome_options above).
        self.enforce_domain_allowlist = enforce_domain_allowlist

        self._current_user_device_id: str | None = None

    # -- WebBotAdapter overrides ------------------------------------------

    def get_chromedriver_payload_file_list(self) -> list[str]:
        return [os.path.join(_PAYLOAD_DIR, "google_meet_chromedriver_payload.js")]

    def add_subclass_specific_chrome_options(self, options) -> None:
        # Intentionally a no-op. The donor enforces its domain allowlist via
        # a Chrome managed-policy file only when ENFORCE_DOMAIN_ALLOWLIST_IN_
        # CHROME=true (default false); this port likewise enforces nothing,
        # matching the donor default. (An earlier revision wrote
        # /tmp/meet-voice-bot-chrome-policies.json, a path Chrome never
        # reads -- removed so nothing claims protection it doesn't provide.)
        # DEFAULT_DOMAIN_ALLOWLIST above is kept as documentation of the
        # domains a guest join is expected to stay within.
        return

    def subclass_specific_initial_data_code(self) -> str:
        return ""

    def handle_websocket_message(self, message: dict) -> None:
        """Map the trimmed payload's ``UsersUpdate``/``MeetingStatusChange``
        events onto ``on_status_change``. ``active = humanized_status ==
        "in_meeting"`` is this system's join-success signal (plan section
        2, donor web_bot_adapter.py line ~456)."""
        message_type = message.get("type")

        if message_type == "UsersUpdate":
            for user in [*message.get("newUsers", []), *message.get("updatedUsers", []), *message.get("removedUsers", [])]:
                active = user.get("humanized_status") == "in_meeting"
                if user.get("isCurrentUser"):
                    self._current_user_device_id = user.get("deviceId")
                    if active:
                        self._emit_status(STATUS_IN_MEETING, {"device_id": user.get("deviceId")})
                    elif user.get("humanized_status") == "removed_from_meeting":
                        self._emit_status(STATUS_REMOVED, {"device_id": user.get("deviceId")})
                    else:
                        self._emit_status(STATUS_NOT_IN_MEETING, {"device_id": user.get("deviceId")})
            return

        if message_type == "MeetingStatusChange":
            change = message.get("change")
            if change == "removed_from_meeting":
                self._emit_status(STATUS_REMOVED, {})
            elif change == "meeting_ended":
                self._emit_status(STATUS_MEETING_ENDED, {})
            elif change == "failed_to_join":
                self._emit_status("failed_to_join", {"reason": message.get("reason")})
            return

        logger.debug("Unhandled websocket bridge message type: %s", message_type)

    # -- public join/leave surface -----------------------------------------

    def attempt_to_join_meeting(self) -> None:  # type: ignore[override]
        """No-arg wrapper: guest join only, always via a fresh, unauthenticated
        session (never attempts Google account SSO)."""
        GoogleMeetUIMethods.attempt_to_join_meeting(self)

    def leave(self) -> None:
        """Best-effort leave; swallows UI errors since the meeting may
        already be ending by the time this is called."""
        try:
            self.click_leave_button()
        except Exception as e:
            logger.warning("Error clicking leave button (meeting may already be ending): %s", e)
