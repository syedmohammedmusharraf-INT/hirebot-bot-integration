"""Standalone base class for browser-based meeting bots (Selenium + Xvfb + CDP).

Trimmed, Django-independent port of attendee/bots/web_bot_adapter/
web_bot_adapter.py. Kept: ``init_driver()`` (Chrome options, fake-media
flags, payload injection -- donor lines ~756-841), ``init()`` Xvfb bring-up
(donor lines ~933-959), ``teardown_driver()`` process-tree SIGKILL (donor
lines ~712-754), and the JSON half of ``handle_websocket()`` (donor lines
~420-513) that carries roster/lifecycle events from the injected JS bridge.

Dropped entirely: the DB-backed bot object, Celery/Redis, video frame /
mixed-audio / per-participant-audio websocket message types (Meet audio
capture for this system goes through PulseAudio -- see
docker/entrypoint.sh and meet_voice_bot/livekit_bridge.py -- never through
this websocket), captions/chat/recording/webpage-streamer/room-sync
plumbing, and the debug screen recorder.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from typing import Callable

from pyvirtualdisplay import Display
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from websockets.sync.server import serve as ws_serve

logger = logging.getLogger(__name__)

_PAYLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload")
_SHARED_PAYLOAD_PATH = os.path.join(_PAYLOAD_DIR, "shared_chromedriver_payload.js")

# Status strings passed to on_status_change(status, extra). Subclasses may
# emit additional platform-specific values (see GoogleMeetBotAdapter).
STATUS_IN_MEETING = "in_meeting"
STATUS_NOT_IN_MEETING = "not_in_meeting"
STATUS_REMOVED = "removed"
STATUS_MEETING_ENDED = "meeting_ended"


class WebBotAdapter:
    """Selenium/Xvfb/CDP plumbing shared by every web-based meeting bot.

    Subclasses (e.g. ``GoogleMeetBotAdapter``) provide the platform-specific
    join flow and payload file; this class only owns the browser lifecycle
    and the JSON websocket bridge the injected JS payload talks back over.
    """

    def __init__(
        self,
        *,
        display_var: str = ":99",
        window_width: int = 1920,
        window_height: int = 1080,
        chrome_binary_path: str = "/usr/bin/google-chrome",
        chromedriver_path: str = "/usr/local/bin/chromedriver",
        on_status_change: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.display_var = display_var
        self.window_width = window_width
        self.window_height = window_height
        self.chrome_binary_path = chrome_binary_path
        self.chromedriver_path = chromedriver_path
        self._on_status_change = on_status_change

        self.driver: webdriver.Chrome | None = None
        self._display: Display | None = None
        # Subclasses (e.g. GoogleMeetBotAdapter) set this before init() runs;
        # used by get_initial_data_code() to seed window.initialData.botName.
        self.bot_name: str = ""

        self._websocket_port: int | None = None
        self._websocket_server = None
        self._websocket_thread: threading.Thread | None = None
        self.last_websocket_message_processed_time: float | None = None

    # -- lifecycle -------------------------------------------------------

    def init(self) -> None:
        """Start Xvfb (if no real display is already available) then the
        driver and the local websocket bridge server."""
        if os.environ.get("DISPLAY") is None:
            self._display = Display(visible=0, size=(self.window_width + 10, self.window_height + 10), use_xauth=True)
            self._display.start()
            logger.info("Started virtual display %s", self._display.new_display_var)

        self._websocket_thread = threading.Thread(target=self._run_websocket_server, daemon=True)
        self._websocket_thread.start()
        self._wait_for_websocket_server_to_start()

        self.init_driver()

    def _wait_for_websocket_server_to_start(self, timeout_seconds: float = 10) -> None:
        deadline = time.time() + timeout_seconds
        while not self._websocket_port and time.time() < deadline:
            time.sleep(0.1)
        if not self._websocket_port:
            raise RuntimeError(f"WebSocket bridge server failed to start within {timeout_seconds}s")

    def _run_websocket_server(self) -> None:
        port = 8765
        max_retries = 10
        for attempt in range(max_retries):
            try:
                self._websocket_server = ws_serve(self._handle_websocket, "localhost", port, compression=None, max_size=None)
                self._websocket_port = port
                logger.info("Websocket bridge listening on ws://localhost:%d", port)
                self._websocket_server.serve_forever()
                return
            except OSError as e:
                if e.errno == 98 and attempt < max_retries - 1:  # address already in use
                    port += 1
                    continue
                raise

    def _handle_websocket(self, websocket) -> None:
        try:
            for message in websocket:
                self._dispatch_raw_message(message)
                self.last_websocket_message_processed_time = time.time()
        except Exception:
            logger.info("Websocket bridge connection closed")

    def _dispatch_raw_message(self, message: bytes) -> None:
        """Decode one frame in the donor's wire format: 4-byte little-endian
        message type + payload. Only type 1 (JSON) is handled -- video/audio
        frame types from the donor protocol are never sent by the trimmed
        payload, since Meet audio capture happens over PulseAudio instead."""
        if len(message) < 4:
            return
        message_type = int.from_bytes(message[:4], byteorder="little")
        if message_type != 1:
            logger.debug("Ignoring non-JSON websocket bridge message type %d", message_type)
            return
        try:
            json_data = json.loads(message[4:].decode("utf-8"))
        except Exception:
            logger.warning("Could not decode JSON websocket bridge message")
            return
        if isinstance(json_data, dict):
            self.handle_websocket_message(json_data)

    def handle_websocket_message(self, message: dict) -> None:
        """Forward roster/lifecycle events from the JS bridge to
        ``on_status_change``. Base implementation is a no-op hook point;
        ``GoogleMeetBotAdapter`` supplies the Meet-specific interpretation."""
        return

    def _emit_status(self, status: str, extra: dict | None = None) -> None:
        if self._on_status_change:
            try:
                self._on_status_change(status, extra or {})
            except Exception:
                logger.exception("on_status_change callback raised for status=%s", status)

    # -- driver setup ------------------------------------------------------

    def get_chromedriver_payload_file_list(self) -> list[str]:
        """Override point: absolute paths of platform-specific JS files to
        inject before page scripts run. Base returns none -- only the shared
        payload (status bridge + fake-mic BotOutputManager) is injected."""
        return []

    def get_js_library_paths(self) -> list[str]:
        """Third-party JS bundled next to the shared payload (pako, protobufjs)."""
        return [
            os.path.join(_PAYLOAD_DIR, "protobuf.min.js"),
            os.path.join(_PAYLOAD_DIR, "pako.min.js"),
        ]

    def get_initial_data_code(self) -> str:
        """JS that seeds ``window.initialData`` before Meet's own scripts run."""
        return f"window.initialData = {{websocketPort: {self._websocket_port}, botName: {json.dumps(self.bot_name)}}};" + self.subclass_specific_initial_data_code()

    def subclass_specific_initial_data_code(self) -> str:
        return ""

    def add_subclass_specific_chrome_options(self, options: "webdriver.ChromeOptions") -> None:
        return

    def init_driver(self) -> None:
        options = webdriver.ChromeOptions()
        options.binary_location = self.chrome_binary_path

        options.add_argument("--autoplay-policy=no-user-gesture-required")
        # Fake mic/cam device: Chrome will accept a synthesized MediaStream in
        # place of real hardware, which is how the agent's answer audio
        # (BotOutputManager.playPCMAudio) reaches the meeting as the bot's
        # own microphone input.
        options.add_argument("--use-fake-device-for-media-stream")
        options.add_argument("--use-fake-ui-for-media-stream")
        options.add_argument(f"--window-size={self.window_width},{self.window_height}")
        options.add_argument("--start-fullscreen")
        # Headless MUST stay disabled -- Google Meet blocks headless Chrome.
        options.add_argument("--disable-gpu")
        # Since Chrome 139, the software-rendering fallback (SwiftShader) is
        # gated behind this flag. Without it, a GPU-less container running
        # current stable (e.g. 153) + --disable-gpu cannot paint Meet at all
        # (blank white tab). No-op on older Chrome versions.
        options.add_argument("--enable-unsafe-swiftshader")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-application-cache")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        # Fresh container profile on every run: suppress Chrome's first-run
        # splash / "Restore pages" / default-browser prompts, which would
        # otherwise cover the Meet tab and hide the guest name input.
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        # The build already needs --no-check-certificate for wget because the
        # host may run TLS-intercepting AV/firewall (see
        # docker/Dockerfile.meet-voice). The same interception breaks Meet's
        # own TLS inside this container (cert-error interstitial instead of
        # the prejoin page), so ignore cert errors here too.
        options.add_argument("--ignore-certificate-errors")
        # Recent Chrome (Local Network Access) blocks a public https:// page
        # from opening a connection to a "local" target like localhost by
        # default (net::ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS) -- which
        # is exactly what the injected payload's WebSocketClient does to
        # reach the status-bridge server this class runs (see
        # _run_websocket_server below). Without this, the roster/status
        # bridge silently never connects and MeetingStatusChange events
        # (removed/ended) never reach on_status_change; join-detection
        # itself doesn't depend on it (wait_until_admitted polls the DOM),
        # so this fails closed rather than blocking the join.
        options.add_argument("--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,BlockInsecurePrivateNetworkRequests,BlockInsecurePrivateNetworkRequestsForNavigations")

        if os.getenv("ENABLE_CHROME_SANDBOX", "false").lower() != "true":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-setuid-sandbox")

        options.add_experimental_option(
            "prefs",
            {
                "credentials_enable_service": False,
                "profile.password_manager_enabled": False,
            },
        )
        # Browser-console + CDP-network capture for join-failure diagnosis
        # (see _dump_join_debug_info). Without these, driver.get_log() has
        # nothing to return and a blank Meet tab leaves no trace of why.
        options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})

        self.add_subclass_specific_chrome_options(options)

        self.driver = webdriver.Chrome(options=options, service=Service(executable_path=self.chromedriver_path))
        logger.info("Chrome driver started (session port %s)", self.driver.service.port)

        combined_code = self._build_payload_script()
        self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": combined_code})

    def _build_payload_script(self) -> str:
        libraries_code = "\n".join(open(path, "r", encoding="utf-8").read() for path in self.get_js_library_paths())

        with open(_SHARED_PAYLOAD_PATH, "r", encoding="utf-8") as f:
            shared_payload_code = f.read()

        subclass_payload_code = "\n".join(open(path, "r", encoding="utf-8").read() for path in self.get_chromedriver_payload_file_list())

        return f"""
            {self.get_initial_data_code()}
            {libraries_code}
            {shared_payload_code}
            {subclass_payload_code}
        """

    # -- output audio (agent's answer -> fake mic) --------------------------

    def send_raw_audio(self, pcm_bytes: bytes, sample_rate: int) -> None:
        """Push little-endian int16 PCM into the fake mic track via the
        injected ``window.botOutputManager.playPCMAudio``, so it is
        transmitted into the meeting as the bot's own microphone audio.
        Ported from web_bot_adapter.py's ``send_raw_audio`` (donor lines
        ~1373-1388)."""
        if not self.driver:
            logger.warning("send_raw_audio called before driver was initialized; dropping audio")
            return
        import numpy as np

        audio_data = np.frombuffer(pcm_bytes, dtype=np.int16).tolist()
        self.driver.execute_script("window.botOutputManager.playPCMAudio(arguments[0], arguments[1]);", audio_data, sample_rate)

    def set_mic_muted(self, muted: bool) -> None:
        """Toggle the fake mic track via the payload's mute hook, independent
        of Meet's own mic UI button (used for the agent's per-utterance input
        guard, not for the join-time mute in ``turn_off_media_inputs``)."""
        if not self.driver:
            return
        self.driver.execute_script("window.botOutputManager?.setMicMuted(arguments[0]);", muted)

    # -- teardown ------------------------------------------------------------

    def _descendant_pids(self, pid: int) -> list[int]:
        try:
            out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return []
        pids: list[int] = []
        for child in [int(p) for p in out.split()]:
            pids.extend(self._descendant_pids(child))
            pids.append(child)
        return pids

    def teardown_driver(self) -> None:
        """Quit the Selenium driver, stop Xvfb, and SIGKILL any leftover
        chrome/chromedriver process tree. Ported from web_bot_adapter.py's
        ``teardown_driver`` (donor lines ~712-754), minus the graceful
        30s-timeout dance -- with no in-progress recording to flush cleanly,
        a bounded direct shutdown is enough here."""
        driver = self.driver
        self.driver = None

        if driver is not None:
            chromedriver_pid = getattr(getattr(driver.service, "process", None), "pid", None)

            try:
                driver.quit()
            except Exception as e:
                logger.warning("Error quitting driver: %s", e)

            if chromedriver_pid:
                for pid in self._descendant_pids(chromedriver_pid) + [chromedriver_pid]:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception as e:
                        logger.warning("Error killing pid %d: %s", pid, e)
                logger.info("Killed chromedriver pid %s and descendants", chromedriver_pid)

        if self._websocket_server:
            try:
                self._websocket_server.shutdown()
            except Exception as e:
                logger.warning("Error shutting down websocket bridge server: %s", e)

        if self._display:
            try:
                self._display.stop()
            except Exception as e:
                logger.warning("Error stopping virtual display: %s", e)
