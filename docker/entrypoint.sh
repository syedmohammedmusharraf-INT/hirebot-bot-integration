#!/usr/bin/env bash
set -euo pipefail
# Near-verbatim port of attendee/entrypoint.sh -- it was already generic and
# Django-independent (PulseAudio bring-up only), so it's kept essentially
# unchanged. See plan section 2.1: this is what makes `auto_null`/
# `auto_null.monitor` available for meet_voice_bot/livekit_bridge.py's
# parec/paplay capture+playback pipeline.
[[ "${PA_DEBUG:-0}" = "1" ]] && set -x

die(){ echo "FATAL: $*" >&2; exit 1; }
have(){ command -v "$1" >/dev/null 2>&1; }

for b in pulseaudio pactl; do have "$b" || die "Missing $b"; done

# ---- Safe XDG_RUNTIME_DIR selection ----
UID_CUR="$(id -u)"
CANDIDATE="${XDG_RUNTIME_DIR:-}"

usable_dir() {
  local d="$1"
  [[ -n "$d" ]] && [[ -d "$d" ]] && [[ -w "$d" ]] && [[ "$(stat -c %u "$d" 2>/dev/null || echo -1)" -eq "$UID_CUR" ]]
}

if usable_dir "$CANDIDATE"; then
  export XDG_RUNTIME_DIR="$CANDIDATE"
else
  if usable_dir "/run/user/$UID_CUR"; then
    export XDG_RUNTIME_DIR="/run/user/$UID_CUR"
  else
    export XDG_RUNTIME_DIR="/tmp/xdg-${UID_CUR}"
    mkdir -p "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
  fi
fi

export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"
mkdir -p "$PULSE_RUNTIME_PATH"
chmod 700 "$XDG_RUNTIME_DIR" || true

# Make ALSA 'default' point at Pulse (used by the Selenium/Chrome side; the
# capture/playback pipeline in livekit_bridge.py talks to Pulse directly via
# parec/paplay and doesn't go through ALSA).
HOME_DIR="${HOME:-/home/$(id -un)}"
mkdir -p "$HOME_DIR"
cat > "$HOME_DIR/.asoundrc" <<'EOF'
pcm.!default { type pulse }
ctl.!default { type pulse }
EOF

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== ENV ===="
  echo "USER=$(id -un) UID=$(id -u) GID=$(id -g)"
  echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
  echo "PULSE_RUNTIME_PATH=$PULSE_RUNTIME_PATH"
  echo "PULSE_SERVER=${PULSE_SERVER:-<unset>}"
  echo "=============="
fi

if [[ -z "${PULSE_SERVER:-}" ]]; then
  rm -f "${PULSE_RUNTIME_PATH}/pid" 2>/dev/null || true
  echo "Starting PulseAudio (per-user)..."
  pulseaudio --daemonize=yes \
             --exit-idle-time="${PA_IDLE_TIME:--1}" \
             --realtime=no --high-priority=no \
             --log-level="${PA_LOG_LEVEL:-info}" --log-target=stderr \
             --disallow-exit || die "pulseaudio failed to start"
  export PULSE_SERVER="unix:${PULSE_RUNTIME_PATH}/native"
else
  echo "Using external Pulse server at $PULSE_SERVER"
fi

for i in {1..50}; do pactl info >/dev/null 2>&1 && break; sleep 0.1; done
pactl info >/dev/null || die "pactl cannot reach PulseAudio"

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== PACTL INFO ===="
  pactl info || true
  echo "==== SINKS (short) ===="
  pactl list short sinks || true
  echo "==== SOURCES (short) ===="
  pactl list short sources || true
fi

# Prefer an existing null sink (auto_null) -- this is the fake speaker/mic
# pair the bot's Chrome plays into and the LiveKit publisher captures from.
if pactl list short sinks | awk '{print $2}' | grep -qx "auto_null"; then
  pactl set-default-sink auto_null || true
  pactl set-default-source auto_null.monitor || true
fi

if [[ "${PA_DEBUG:-0}" = "1" ]]; then
  echo "==== FINAL ===="
  echo "Default Sink:   $(pactl info | sed -n 's/^Default Sink: //p')"
  echo "Default Source: $(pactl info | sed -n 's/^Default Source: //p')"
  pactl list short sinks || true
  pactl list short sources || true
  echo "================"
fi

echo "[entrypoint] PulseAudio ready. Exec: $*"
exec "$@"
