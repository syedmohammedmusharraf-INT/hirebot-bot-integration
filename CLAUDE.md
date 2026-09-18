# CLAUDE.md

Context for Claude Code (or any future contributor) working in this repo. Read this before making changes — it captures decisions and gotchas that aren't obvious from the code alone. Full detail lives in `README.md`; the original design doc is `../attendee/plan/google_meet_livekit_voice_bot_plan.md`.

## What this is

A standalone backend that joins a Google Meet call as a guest bot and bridges its audio to a self-hosted LiveKit room, where a `livekit-agents` voice pipeline (STT → LLM → TTS) talks back into the call. It vendors only the Meet-join layer (headed Chrome + Xvfb + Selenium/CDP) from `../attendee`, trimmed to be Django-independent. **Nothing here calls into attendee's Django app, DB, Celery, or any MCP server at runtime** — LiveKit room/token/dispatch, the audio bridge, and the voice pipeline are all implemented natively in this repo.

Two Docker images, kept strictly separate — never let dependencies cross:
- **control** (`docker/Dockerfile.meet-voice`, source `meet_voice_bot/`) — Meet joiner + HTTP API + LiveKit room/token/dispatch. Never gets an STT/LLM/TTS SDK or provider API key.
- **agent** (`docker/Dockerfile.agent`, source `meet-agent/`) — `livekit-agents` voice pipeline worker. Never gets Selenium/Chrome/Xvfb/PulseAudio.

Both import `model_support/` (stdlib-only) so they can't disagree about which `(assistant_mode, provider, model)` combinations are legal.

## Current mode: minimal setup, LiveKit disabled by default

**`LIVEKIT_ENABLED` defaults to `false`.** In this mode the bot joins Meet and `meet_voice_bot/audio_probe.py`'s `AudioCaptureProbe` logs the captured Meet audio level (dBFS) so the capture path can be verified without any LiveKit server, agent, or provider keys. `control.RoomController` and `livekit_bridge.MeetAudioBridge` are **not deleted**, just not called — flip `LIVEKIT_ENABLED=true` (and fill in `LIVEKIT_URL`/`LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`) to run the full flow. `docker-compose.minimal.yaml` (default, one service) vs `docker-compose.meet-voice.yaml` (full stack: livekit-server + control + agent) are two separate compose files for this — don't merge them.

When touching `joiner.py`, remember `MeetBotSession` branches on `settings.livekit_enabled` in two places: `start()` (skip room/token/dispatch) and `_run_meeting()` (construct `AudioCaptureProbe` instead of `MeetAudioBridge`). Both implement the same `start()`/`stop()` shape on purpose so `_cleanup()` doesn't need to branch.

## The audio path — read this before touching `livekit_bridge.py`

Meet↔LiveKit audio is **two different mechanisms**, not one shared PulseAudio loopback:

- **Capture** (Meet → LiveKit/probe): Chrome's audible output goes to PulseAudio's `auto_null` sink; `parec` reads `auto_null.monitor` and either publishes it into the LiveKit room (`MeetAudioBridge`) or just measures its level (`AudioCaptureProbe`).
- **Playback** (LiveKit → Meet, agent's answer): **never** written back into PulseAudio — that would feed straight back into the `auto_null.monitor` capture above and make the agent hear itself. It goes through `GoogleMeetBotAdapter.send_raw_audio`, which pushes PCM into a JS-injected fake-mic `MediaStream` (`window.botOutputManager.playPCMAudio`) via Selenium `execute_script`, batched (~100ms) since `execute_script` is an HTTP round trip and can't keep up per-20ms-frame.

This got it wrong once already during a multi-agent build (playback was wired through `paplay`→`auto_null`, which would have looped the agent's own voice back into its own capture). If you're editing this area, keep the two directions on separate mechanisms.

## Repo layout quick reference

See `README.md`'s "Repository layout" for the full annotated tree. Key modules:
- `meet_voice_bot/joiner.py` — `MeetBotSession`, per-bot lifecycle, the LIVEKIT_ENABLED branch point.
- `meet_voice_bot/control.py` — `RoomController`: room create, token mint (`livekit-api`'s `AccessToken`/`VideoGrants`), explicit agent dispatch.
- `meet_voice_bot/livekit_bridge.py` / `audio_probe.py` — the two capture/playback implementations described above.
- `meet_voice_bot/web_bot_adapter/`, `google_meet_bot_adapter/` — vendored, trimmed, Django-independent port of the donor's Selenium/Xvfb/CDP join flow. Ported from `../attendee/bots/web_bot_adapter/` and `../attendee/bots/google_meet_bot_adapter/`; don't re-vendor more from there without checking the plan's "forbidden" list (room-sync, transcription, Zoom/Teams/SSO, etc. are explicitly out of scope).
- `model_support/` — shared validation (`assistant_config.py`, `allowlists.py`, `knobs.py`), stdlib only, imported by both images.
- `meet-agent/` — voice pipeline worker. Directory has a hyphen on purpose (not an importable Python package); its modules import each other as flat top-level modules, matching how the Docker image copies them in.

## Environment / tooling

- Python 3.12, managed with `uv`. `uv sync` for a fresh clone, `uv add <pkg>` to add a dependency (updates `pyproject.toml` + `uv.lock` + installs in one step) — don't hand-edit `pyproject.toml`'s dependency list.
- `requirements.txt` (repo root) is the union of `requirements-control.txt`/`requirements-agent.txt`, kept only as a readable reference for what's in `pyproject.toml` — it is **not** what the Docker images install. `docker/Dockerfile.meet-voice` installs `requirements-control.txt` only; `docker/Dockerfile.agent` installs `requirements-agent.txt` only. Keep that split intact.
- `webrtc-noise-gain` (agent-only, in `meet-agent/audio_denoise.py`) has no Windows wheel and won't build under MSVC — it's marked `sys_platform != "win32"` in both `requirements-agent.txt`/`requirements.txt` and `pyproject.toml`. Exercise that code path under Docker/Linux, not on a Windows dev box.
- Tests: `uv run pytest tests/ -v` — no external services, no API keys, no subprocess/PulseAudio needed (all pure-function / mocked-env unit tests: `model_support`, `meet_url`, `config`'s LIVEKIT_ENABLED gating, `audio_probe`'s dBFS math).
- Chrome/chromedriver are resolved in lockstep at build time in `docker/Dockerfile.meet-voice` (current `google-chrome-stable` from Google's apt repo + the same-version chromedriver from Chrome-for-Testing). Don't pin a direct `dl.google.com` .deb URL — Google prunes old .debs (134.0.6998.88 now 404s).
- Chrome comes from Google's official apt repo and chromedriver from Google's official Chrome-for-Testing bucket, so no mirror is needed.

## Resolved: Chrome-version-specific join failures (read before touching Chrome flags/payload JS)

Running against **current** `google-chrome-stable` (fetched live from Google's apt repo, currently resolving to Chrome 153.x — much newer than the donor's pinned 134.x) surfaced two real, 100%-reproducible bugs that had nothing to do with network/TLS/DNS (all independently verified fine via `curl`/headless `--dump-dom` from inside the container). Both are now fixed; if a future Chrome update reintroduces either symptom, `scripts/diag_join.py` is the tool that found them (bisect by commenting out pieces of the injected payload's "Wiring" section and rerunning it):

1. **Navigation hang**: `driver.get(meeting_url)` would return after 40-70s having never left the blank `current_url='data:,'` tab, no exception raised, with a stray `net::ERR_ABORTED` for an empty-URL "Document" in the CDP performance log. Root cause: `google_meet_chromedriver_payload.js`'s `WebSocketClient` opened `ws://localhost:PORT` **synchronously in its constructor**, and having that live connection attempt in flight during/around the navigation commit made Chrome abort the navigation outright — independent of which document (blank vs. real) actually ran the code. Fix: `WebSocketClient._connect()` now defers the actual `new WebSocket(...)` call until `window.load`. Nothing needs the bridge connected any earlier — join-detection (`wait_until_admitted` in `google_meet_ui_methods.py`) polls the DOM directly, not this socket.
2. **Local Network Access block**: even after fix #1, the deferred WebSocket connection itself was rejected with `net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS` — recent Chrome blocks a public `https://` page from opening a connection to `localhost` by default. Fixed with `--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,BlockInsecurePrivateNetworkRequests,BlockInsecurePrivateNetworkRequestsForNavigations` in `web_bot_adapter.py`'s `init_driver()`. These are internal Chromium feature-flag names, not a stable public API — if a future Chrome renames/removes them and the roster/status bridge silently stops connecting again (bot still joins fine, but `MeetingStatusChange`/`removed_from_meeting` events never fire — natural-leave detection breaks, `MAX_UPTIME_SECONDS` still works as a backstop), check `chrome://net-export` or the console for the current `ERR_BLOCKED_BY_*` name and update the flag list, or switch to the Enterprise policy `InsecurePrivateNetworkRequestsAllowedForUrls` (`/etc/opt/chrome/policies/managed/*.json` — same directory `GoogleMeetBotAdapter.add_subclass_specific_chrome_options`'s domain-allowlist code already writes into, though as of this writing that write path targets the wrong filename and has no effect either way — see next section).
3. Also still present but not the cause of either bug above: `GoogleMeetBotAdapter.add_subclass_specific_chrome_options`'s domain-allowlist policy write (`enforce_domain_allowlist=True`) writes to `/tmp/meet-voice-bot-chrome-policies.json`, which nothing reads — there's no symlink from `/etc/opt/chrome/policies/managed/` into `/tmp` (the donor's Dockerfile set one up; ours doesn't). It's currently a harmless no-op, not an active allowlist. Fix it properly (correct target path, or drop `enforce_domain_allowlist` until it's wired up) before relying on it.

## Known soft spots (flagged, not blocking)

- `livekit-agents`/`livekit-api` exact method/event names (`agent_dispatch.create_dispatch`, `AgentSession` speaking-state events, `livekit.plugins.sarvam`/`livekit.plugins.google` module paths) were written without live PyPI/docs access — verify against whatever version you actually install before a first real run against a live LiveKit server.
- `GoogleMeetBotAdapter.set_mic_muted()` exists but is unwired — the plan's per-utterance input guard is implemented entirely agent-side via `SpeechGate.muted`, not through this.
- `meet_voice_bot/web_bot_adapter/x11_input.py` (humanized mouse movement) is ported but not called from anywhere; needs `python-xlib` (commented out in requirements) only if you wire it in.
- In-process bot registry in `main.py` — fine for one control replica; would need Redis/Postgres for multi-replica, out of scope for now.

## Working conventions in this repo

- Keep the control/agent dependency split strict — never add a provider SDK to `requirements-control.txt` or Selenium/Chrome to `requirements-agent.txt`.
- Docstrings cite which plan section a module implements (e.g. "plan section 4.4") — keep that pattern when adding files, it's how this maps back to `google_meet_livekit_voice_bot_plan.md`.
- When porting more from `../attendee`, check the plan's Section 2 "what to inherit" table and Section 2 "forbidden" list first — this repo intentionally does not vendor room-sync, transcription providers, or non-Meet platform adapters.
- Don't delete/replace `docker-compose.meet-voice.yaml` when working on the minimal setup, and don't add LiveKit calls back into the `LIVEKIT_ENABLED=false` path — the disable/enable switch is the point.
