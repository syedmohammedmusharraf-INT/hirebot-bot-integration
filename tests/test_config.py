"""Unit tests for meet_voice_bot/config.py's LIVEKIT_ENABLED gating (minimal
setup: LiveKit is off by default and its URL/key/secret only become
required once it's turned on)."""

import pytest

from meet_voice_bot.config import MissingSettingError, _load_settings


def _clear_livekit_env(monkeypatch):
    for name in ("LIVEKIT_ENABLED", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(name, raising=False)


def test_livekit_disabled_by_default_and_url_not_required(monkeypatch):
    _clear_livekit_env(monkeypatch)
    settings = _load_settings()
    assert settings.livekit_enabled is False
    assert settings.livekit_url == ""


def test_livekit_enabled_requires_url(monkeypatch):
    _clear_livekit_env(monkeypatch)
    monkeypatch.setenv("LIVEKIT_ENABLED", "true")
    with pytest.raises(MissingSettingError):
        _load_settings()


def test_livekit_enabled_with_all_vars_set(monkeypatch):
    _clear_livekit_env(monkeypatch)
    monkeypatch.setenv("LIVEKIT_ENABLED", "true")
    monkeypatch.setenv("LIVEKIT_URL", "ws://localhost:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret")
    settings = _load_settings()
    assert settings.livekit_enabled is True
    assert settings.livekit_url == "ws://localhost:7880"
