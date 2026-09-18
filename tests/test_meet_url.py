"""Unit tests for Google Meet URL canonicalization (plan section 2,
donor bots/meeting_url_utils.py Meet branch)."""

import pytest

from meet_voice_bot.meet_url import InvalidMeetingUrlError, canonicalize_meet_url


def test_plain_meet_url_canonicalizes():
    assert canonicalize_meet_url("https://meet.google.com/abc-defg-hij") == "https://meet.google.com/abc-defg-hij"


def test_meet_url_with_query_params_is_stripped():
    assert canonicalize_meet_url("https://meet.google.com/abc-defg-hij?authuser=0&hs=122") == "https://meet.google.com/abc-defg-hij"


def test_bare_host_without_scheme():
    assert canonicalize_meet_url("meet.google.com/abc-defg-hij") == "https://meet.google.com/abc-defg-hij"


def test_non_meet_url_rejected():
    with pytest.raises(InvalidMeetingUrlError):
        canonicalize_meet_url("https://zoom.us/j/1234567890")


def test_empty_url_rejected():
    with pytest.raises(InvalidMeetingUrlError):
        canonicalize_meet_url("")


def test_multi_url_smuggling_rejected():
    smuggled = "https://meet.google.com/abc-defg-hij https://evil.example.com/phish"
    with pytest.raises(InvalidMeetingUrlError):
        canonicalize_meet_url(smuggled)
