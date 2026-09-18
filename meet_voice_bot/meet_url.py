"""Google Meet URL canonicalization. Implements plan section 2, row
"bots/meeting_url_utils.py -- Meet branch only (165-172)".

Ported from the donor's ``normalize_meeting_url_raw`` Google Meet branch and
its ``contains_multiple_urls`` guard, with the Zoom/Teams branches dropped and
the ``tldextract`` dependency replaced by a plain ``urlparse`` host check
(Google Meet links are always served from the exact ``meet.google.com`` host,
so the extra dependency and its registrable-domain data file are unnecessary
here).
"""

from __future__ import annotations

import base64
import re
from urllib.parse import unquote, urlparse

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_MEET_HOST = "meet.google.com"
_MEET_CODE_RE = re.compile(r"meet\.google\.com/([a-zA-Z0-9-]+)")


class InvalidMeetingUrlError(Exception):
    """Raised when a string is not a valid, single Google Meet meeting URL."""


def _contains_multiple_urls(url: str) -> bool:
    """Port of the donor's ``contains_multiple_urls``.

    Guards against prompt-injection-style strings that smuggle a second URL
    inside the meeting_url field (plain, single/double URL-decoded, or
    base64-decoded) by scanning every suffix of the string for an embedded
    http(s) URL and rejecting if more than one is found.
    """
    if not url:
        return False

    found = set()
    for i in range(len(url)):
        suffix = url[i:]

        if _HTTP_URL_RE.match(suffix):
            found.add(suffix)
            continue

        decoded_once = unquote(suffix)
        if _HTTP_URL_RE.match(decoded_once):
            found.add(decoded_once)
            continue

        decoded_twice = unquote(decoded_once)
        if _HTTP_URL_RE.match(decoded_twice):
            found.add(decoded_twice)
            continue

        try:
            decoded_b64 = base64.b64decode(suffix).decode("utf-8")
            if _HTTP_URL_RE.match(decoded_b64):
                found.add(decoded_b64)
                continue
        except Exception:
            pass

    return len(found) > 1


def _host(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower() or None


def canonicalize_meet_url(url: str) -> str:
    """Validate and canonicalize a Google Meet URL.

    Returns ``https://meet.google.com/{code}`` for a well-formed, single
    Google Meet link. Raises :class:`InvalidMeetingUrlError` for anything
    else: empty input, a non-Meet URL, or a string containing more than one
    embedded URL.
    """
    if not url or not url.strip():
        raise InvalidMeetingUrlError("meeting_url is empty")

    cleaned = url.strip().rstrip(">")

    if _contains_multiple_urls(cleaned):
        raise InvalidMeetingUrlError("meeting_url contains more than one embedded URL")

    host = _host(cleaned)
    if host != _MEET_HOST:
        raise InvalidMeetingUrlError(f"meeting_url is not a {_MEET_HOST} URL: {url!r}")

    match = _MEET_CODE_RE.search(cleaned)
    if not match:
        raise InvalidMeetingUrlError(f"could not extract a meeting code from meeting_url: {url!r}")

    return f"https://{_MEET_HOST}/{match.group(1)}"
