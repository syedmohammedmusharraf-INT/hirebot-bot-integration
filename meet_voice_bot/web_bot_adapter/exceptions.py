"""UI failure taxonomy for the Selenium/CDP join flow.

Ported from attendee/bots/web_bot_adapter/ui_methods.py (the donor's
Ui*Exception hierarchy) and attendee/bots/google_meet_bot_adapter/
google_meet_ui_methods.py (the two Google-specific subclasses). Login/SSO/
Okta/mocap/captcha/incorrect-password exceptions are dropped -- this system
is guest-join only, so those failure modes cannot occur here.
"""

from __future__ import annotations


class UiException(Exception):
    """Base class for every join-flow failure. ``step`` names the join stage
    that failed (e.g. ``"name_input"``, ``"join_button"``) and ``inner_exception``
    carries the underlying Selenium exception, if any, for diagnostics."""

    def __init__(self, message: str, step: str | None = None, inner_exception: Exception | None = None) -> None:
        self.step = step
        self.inner_exception = inner_exception
        super().__init__(message)


class UiRetryableException(UiException):
    """The join attempt failed but a fresh attempt (new driver, same bot) may succeed."""


class UiRetryableExpectedException(UiRetryableException):
    """A retryable failure that is expected to happen occasionally in normal
    operation (e.g. Google rate-limiting joins) and should not count against
    the retry budget as heavily as a truly unexpected error."""


class UiInfinitelyRetryableException(UiException):
    """Always retried; it is up to the caller to eventually stop hitting the
    condition that raises this (e.g. waiting for a still-loading page)."""


class UiCouldNotLocateElementException(UiRetryableException):
    """A required element did not appear within its wait window."""


class UiCouldNotClickElementException(UiRetryableException):
    """An element was found but could not be clicked (stale, intercepted, etc.)."""


class UiRequestToJoinDeniedException(UiException):
    """Someone in the call denied the bot's request to join, nobody responded
    to the request before Meet gave up, or the bot was shown "You left the
    meeting" as a result. Not retryable -- the caller should stop."""


class UiCouldNotJoinMeetingWaitingForHostException(UiException):
    """The host never started the meeting within ``wait_for_host_timeout_seconds``."""


class UiCouldNotJoinMeetingWaitingRoomTimeoutException(UiException):
    """The bot waited in the "Asking to be let in" state for longer than
    ``waiting_room_timeout_seconds`` without being admitted."""


class UiMeetingNotFoundException(UiException):
    """Google Meet reported the meeting code as invalid, expired, or unknown."""


class UiGoogleBlockingUsException(UiRetryableExpectedException):
    """Google showed a generic "can't join this call" / "problem connecting"
    error. Usually transient; safe to retry with a fresh driver."""


class UiGoogleWrongAudioConfigurationException(UiRetryableExpectedException):
    """Google Meet's A/B-tested alternate audio pipeline was served for this
    session (no ``<audio>`` elements present); retrying usually lands on the
    standard configuration this adapter expects."""


class UiMocapSequenceNotAvailableException(UiRetryableExpectedException):
    """The generated humanized mouse-movement sequence didn't land on the
    target element after several attempts; a fresh attempt usually succeeds."""
