"""Control-plane configuration. Implements plan section 6 ("Config reference").

All settings are read from environment variables so the control image can be
configured purely through docker-compose/.env, matching the plan's "local,
not MCP" configuration model.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


class MissingSettingError(RuntimeError):
    """Raised when a required environment variable is not set."""


def _env_str(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise MissingSettingError(f"Required environment variable {name} is not set")
    return value  # type: ignore[return-value]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    meet_bot_name: str
    waiting_room_timeout_seconds: int
    wait_for_host_timeout_seconds: int
    livekit_enabled: bool
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    assistant_ref: str
    assistants_dir: str
    chrome_binary_path: str
    chromedriver_path: str
    max_uptime_seconds: int
    control_http_host: str
    control_http_port: int
    agent_name: str
    audio_probe_log_interval_seconds: float
    audio_probe_silence_threshold_dbfs: float
    audio_recording_enabled: bool
    audio_recording_dir: str


def _load_settings() -> Settings:
    # LIVEKIT_ENABLED gates the entire LiveKit solution (room/token/dispatch
    # in control.py, the room publish/subscribe half of livekit_bridge.py).
    # It is disabled by default for the current minimal setup -- see README
    # "Minimal setup (bot-only)" -- so LIVEKIT_URL/KEY/SECRET are only
    # required when it's turned back on, not pruned or deleted.
    livekit_enabled = _env_bool("LIVEKIT_ENABLED", False)

    return Settings(
        meet_bot_name=_env_str("MEET_BOT_NAME", "Voice Bot"),
        waiting_room_timeout_seconds=_env_int("WAITING_ROOM_TIMEOUT_SECONDS", 900),
        wait_for_host_timeout_seconds=_env_int("WAIT_FOR_HOST_TIMEOUT_SECONDS", 600),
        livekit_enabled=livekit_enabled,
        livekit_url=_env_str("LIVEKIT_URL", "", required=livekit_enabled),
        livekit_api_key=_env_str("LIVEKIT_API_KEY", "", required=livekit_enabled),
        livekit_api_secret=_env_str("LIVEKIT_API_SECRET", "", required=livekit_enabled),
        assistant_ref=_env_str("ASSISTANT_REF", "meet-voice-bot-v1"),
        assistants_dir=_env_str("ASSISTANTS_DIR", "./assistants"),
        chrome_binary_path=_env_str("CHROME_BINARY_PATH", "/usr/bin/google-chrome"),
        chromedriver_path=_env_str("CHROMEDRIVER_PATH", "/usr/local/bin/chromedriver"),
        max_uptime_seconds=_env_int("MAX_UPTIME_SECONDS", 5400),
        control_http_host=_env_str("CONTROL_HTTP_HOST", "0.0.0.0"),
        control_http_port=_env_int("CONTROL_HTTP_PORT", 8000),
        agent_name=_env_str("AGENT_NAME", "meet-voice-agent"),
        audio_probe_log_interval_seconds=float(_env_str("AUDIO_PROBE_LOG_INTERVAL_SECONDS", "1.0")),
        audio_probe_silence_threshold_dbfs=float(_env_str("AUDIO_PROBE_SILENCE_THRESHOLD_DBFS", "-50.0")),
        # Only used while LIVEKIT_ENABLED=false (audio_probe.AudioCaptureProbe
        # is what's running the capture in that mode -- see its record_path
        # param). Writes the whole session's captured Meet audio to a WAV
        # file for manual playback verification.
        audio_recording_enabled=_env_bool("AUDIO_RECORDING_ENABLED", False),
        audio_recording_dir=_env_str("AUDIO_RECORDING_DIR", "./recordings"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide settings accessor.

    Cached with ``lru_cache`` rather than a plain module-level singleton so
    tests can call ``get_settings.cache_clear()`` to force a re-read of the
    environment between cases.
    """
    return _load_settings()
