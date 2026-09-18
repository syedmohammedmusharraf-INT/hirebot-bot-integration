"""Google Meet guest-join UI flow.

Trimmed, single-attempt (no SSO/login/mocap/humanized-mouse retry machinery)
port of attendee/bots/google_meet_bot_adapter/google_meet_ui_methods.py.
Kept: ``turn_off_media_inputs`` (donor ~197-250), ``join_now_button_selector``
(donor ~252-253), the blocked/denied/meeting-not-found/waiting-room-timeout
detectors (donor ~124-195), ``wait_for_host_if_needed`` (donor ~577-587),
``fill_out_name_input`` (donor ~475-516, login/mocap branches removed),
``attempt_to_join_meeting`` (donor ~1143-1203) and ``click_leave_button``
(donor ~1276-1296).

Dropped: closed captions (join-admission no longer waits on the captions
button -- see ``wait_until_admitted`` below for the replacement signal),
layout/reactions/incoming-video UI tweaks, Okta/Google-account login,
"humanized" mocap-driven mouse movement, and video-recording DOM patches --
none of those apply to a guest-only voice bot.
"""

from __future__ import annotations

import json
import logging
import os
import time

from selenium.common.exceptions import ElementNotInteractableException, NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from meet_voice_bot.web_bot_adapter.exceptions import (
    UiCouldNotJoinMeetingWaitingForHostException,
    UiCouldNotJoinMeetingWaitingRoomTimeoutException,
    UiCouldNotLocateElementException,
    UiGoogleBlockingUsException,
    UiGoogleWrongAudioConfigurationException,
    UiMeetingNotFoundException,
    UiRequestToJoinDeniedException,
)
from meet_voice_bot.web_bot_adapter.ui_methods import ResilientUIMethods

logger = logging.getLogger(__name__)

_LEAVE_BUTTON_SELECTORS = [
    # NOTE: do NOT pin the jsname attribute here -- Meet rotates jsnames
    # between releases and the pinned value silently stops matching (which
    # used to surface as "Error clicking leave button, retrying" x N during
    # cleanup of a bot that never joined). aria-label alone is stable.
    'button[aria-label="Leave call"]',
    '//button[@aria-label="Leave call"]',
    '//button[.//span[text()="Leave call"]]',
]

# Guest prejoin name field. Meet's exact markup drifts between releases
# (aria-label text, placeholder vs label, input type), so try several
# shapes instead of a single CSS selector -- a single pinned selector is
# what used to surface as "Could not find name input. Timed out." after a
# Meet UI update.
_NAME_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, 'input[aria-label="Your name"]'),
    (By.CSS_SELECTOR, 'input[type="text"][aria-label="Your name"]'),
    (By.CSS_SELECTOR, 'input[placeholder="Your name"]'),
    (By.XPATH, '//input[@placeholder="Your name"]'),
    (By.XPATH, '//input[contains(@aria-label, "name") or contains(@aria-label, "Name")]'),
    (By.XPATH, '//input[@type="text"]'),
]


class GoogleMeetUIMethods(ResilientUIMethods):
    """Mixin expecting ``self.driver``, ``self.meeting_url``, ``self.bot_name``,
    ``self.waiting_room_timeout_seconds`` and ``self.wait_for_host_timeout_seconds``
    on the including class (see ``GoogleMeetBotAdapter``)."""

    def join_now_button_selector(self) -> str:
        return '//button[.//span[text()="Ask to join" or text()="Ask to join anyway" or text()="Join now" or text()="Join the call now" or text()="Join anyway" or text()="Join here too"]]'

    def click_this_meeting_is_being_recorded_join_now_button(self, step: str) -> None:
        button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Join now"]]')
        if button:
            logger.info("Clicking 'this meeting is being recorded' join now button")
            self.click_element(button, step)
            return
        dialog_button = self.find_element_by_selector(By.XPATH, '//div[@role="alertdialog"]//button[@data-mdc-dialog-action="ok"][.//span[text()="Join"]]')
        if dialog_button:
            logger.info("Clicking 'this meeting is being captured' join button")
            self.click_element_forcefully(dialog_button, step)

    def click_others_may_see_your_meeting_differently_button(self, step: str) -> None:
        button = self.find_element_by_selector(By.XPATH, '//button[.//span[text()="Got it"]]')
        if button:
            self.click_element_forcefully(button, step)

    def check_if_meeting_is_found(self) -> None:
        meeting_not_found_texts = [
            "Check your meeting code",
            "Invalid video call name",
            "Your meeting code has expired",
            "The meeting code you entered doesn’t work",
        ]
        xpath = "//*[" + " or ".join(f'contains(text(), "{text}")' for text in meeting_not_found_texts) + "]"
        if self.find_element_by_selector(By.XPATH, xpath):
            raise UiMeetingNotFoundException("Meeting not found", "check_if_meeting_is_found")

    def look_for_blocked_element(self, step: str) -> None:
        cannot_join_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "You can\'t join this video call") or contains(text(), "There is a problem connecting to this video call")]')
        if cannot_join_element:
            logger.warning("Google is blocking the join attempt (retryable): %s", cannot_join_element.text)
            raise UiGoogleBlockingUsException("You can't join this video call", step)

    def look_for_denied_your_request_element(self, step: str) -> None:
        actively_denied_texts = [f"Someone {p} the call {v} your request to join" for p in ("in", "on") for v in ("denied", "has denied")]
        no_one_responded_texts = [f"No one {v} to your request to join the call" for v in ("responded", "has responded")]
        left_meeting_texts = ["You left the meeting"]
        all_texts = actively_denied_texts + no_one_responded_texts + left_meeting_texts

        element = self.find_element_by_selector(By.XPATH, "//*[" + " or ".join(f'contains(text(), "{t}")' for t in all_texts) + "]")
        if not element:
            return

        text = element.text
        if any(t in text for t in actively_denied_texts):
            raise UiRequestToJoinDeniedException("Someone in the call denied your request to join", step)
        elif any(t in text for t in no_one_responded_texts):
            raise UiRequestToJoinDeniedException("No one responded to your request to join the call", step)
        else:
            raise UiRequestToJoinDeniedException("You left the meeting", step)

    def look_for_asking_to_be_let_in_element_after_waiting_period_expired(self, step: str) -> None:
        if self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Asking to be let in")]'):
            raise UiRequestToJoinDeniedException("Bot was not let in after waiting period expired", step)

    def turn_off_media_inputs(self) -> None:
        MIC_OFF_SELECTOR = 'div[aria-label="Turn off microphone"], button[aria-label="Turn off microphone"]'
        MIC_ON_SELECTOR = 'div[aria-label="Turn on microphone"], button[aria-label="Turn on microphone"]'
        CAM_OFF_SELECTOR = 'div[aria-label="Turn off camera"], button[aria-label="Turn off camera"]'
        CAM_ON_SELECTOR = 'div[aria-label="Turn on camera"], button[aria-label="Turn on camera"]'

        for _ in range(5):
            mic_button = self.locate_element(step="turn_off_microphone_button", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, MIC_OFF_SELECTOR)), wait_time_seconds=6)
            self.click_element(mic_button, "turn_off_microphone_button")
            try:
                self.locate_element(step="wait_for_microphone_to_be_off", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, MIC_ON_SELECTOR)), wait_time_seconds=2)
                break
            except Exception:
                logger.warning("Microphone did not appear to turn off, retrying")

        for _ in range(5):
            cam_button = self.locate_element(step="turn_off_camera_button", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, CAM_OFF_SELECTOR)), wait_time_seconds=6)
            self.click_element(cam_button, "turn_off_camera_button")
            try:
                self.locate_element(step="wait_for_camera_to_be_off", condition=EC.element_to_be_clickable((By.CSS_SELECTOR, CAM_ON_SELECTOR)), wait_time_seconds=2)
                break
            except Exception:
                logger.warning("Camera did not appear to turn off, retrying")

    def retrieve_name_input_element(self):
        """Fast sweep over _NAME_INPUT_SELECTORS (instant finds, no per-
        selector waits). The caller (fill_out_name_input) loops with a 1s
        sleep, so one sweep ~= instant and 30 attempts ~= ~35s total. (A
        WebDriverWait per selector here multiplied out to 200s+ of silence
        before ever failing -- don't do that.)"""
        for by, selector in _NAME_INPUT_SELECTORS:
            try:
                el = self.driver.find_element(by, selector)
            except NoSuchElementException:
                continue
            except Exception:
                continue
            try:
                if el.is_displayed() and el.is_enabled():
                    return el
            except StaleElementReferenceException:
                continue
        raise TimeoutException("no name input matched any known selector")

    def _dump_join_debug_info(self, step: str) -> None:
        """Log what the Meet tab actually shows, and persist a screenshot +
        page source under /tmp for `docker cp` inspection. Called when the
        name input (or join button) can't be found, so the next failure says
        *why* (cert error? sign-in wall? invalid code?) instead of just
        "Timed out"."""
        try:
            url = self.driver.current_url
        except Exception:
            url = "<unknown>"
        try:
            title = self.driver.title
        except Exception:
            title = "<unknown>"
        logger.error("join-debug step=%s url=%s title=%s", step, url, title)
        try:
            inputs = self.driver.find_elements(By.CSS_SELECTOR, "input")
            logger.error(
                "join-debug step=%s inputs=%s",
                step,
                [(i.get_attribute("type"), i.get_attribute("aria-label"), i.get_attribute("placeholder")) for i in inputs[:10]],
            )
        except Exception as e:
            logger.error("join-debug step=%s could not list inputs: %s", step, e)
        try:
            buttons = self.driver.find_elements(By.CSS_SELECTOR, "button")
            logger.error("join-debug step=%s buttons=%s", step, [b.text for b in buttons[:15] if b.text])
        except Exception as e:
            logger.error("join-debug step=%s could not list buttons: %s", step, e)
        try:
            body_text = self.driver.find_element(By.TAG_NAME, "body").text
            logger.error("join-debug step=%s body-text=%.2000s", step, body_text)
        except Exception as e:
            logger.error("join-debug step=%s could not read body text: %s", step, e)
        dump_dir = f"/tmp/meet-join-debug-{int(time.time())}"
        try:
            os.makedirs(dump_dir, exist_ok=True)
            self.driver.save_screenshot(os.path.join(dump_dir, f"{step}.png"))
            with open(os.path.join(dump_dir, f"{step}.html"), "w", encoding="utf-8") as f:
                f.write(self.driver.page_source)
            logger.error("join-debug step=%s screenshot+html saved to %s (docker cp <container>:%s .)", step, dump_dir, dump_dir)
        except Exception as e:
            logger.error("join-debug step=%s could not save screenshot/html: %s", step, e)
        self._dump_console_and_network_logs(step, dump_dir)

    def _dump_console_and_network_logs(self, step: str, dump_dir: str) -> None:
        """Log the tab's JS console + failed network requests: the trace a
        blank white page otherwise leaves behind. Renderer-can't-paint shows
        a clean network log with the Meet URL committed; proxy/cert/DNS
        trouble shows loadingFailed / 4xx-5xx entries naming the blocked
        URLs. Requires the goog:loggingPrefs capability in init_driver."""
        try:
            browser_logs = self.driver.get_log("browser")
        except Exception as e:
            logger.error("join-debug step=%s could not read browser console: %s", step, e)
            browser_logs = []
        if browser_logs:
            for entry in browser_logs[-30:]:
                logger.error("join-debug step=%s console [%s] %.500s", step, entry.get("level"), entry.get("message", ""))
        else:
            logger.error("join-debug step=%s console: <empty>", step)
        try:
            perf_logs = self.driver.get_log("performance")
        except Exception as e:
            logger.error("join-debug step=%s could not read performance logs: %s", step, e)
            return
        failures: list[str] = []
        http_errors: list[str] = []
        for entry in perf_logs:
            try:
                msg = json.loads(entry.get("message", "{}")).get("message", {})
            except Exception:
                continue
            method = msg.get("method", "")
            params = msg.get("params", {})
            if method == "Network.loadingFailed":
                failures.append(f"{params.get('type') or '?'} {params.get('errorText')} {params.get('blockedReason', '')} :: {params.get('documentURL', '')}"[:300])
            elif method == "Network.responseReceived":
                resp = params.get("response", {})
                if isinstance(resp.get("status"), int) and resp["status"] >= 400:
                    http_errors.append(f"{resp['status']} {resp.get('url', '')}"[:300])
        logger.error("join-debug step=%s net-failures(%d)=%s", step, len(failures), failures[:20] or "<none>")
        logger.error("join-debug step=%s http-errors(%d)=%s", step, len(http_errors), http_errors[:20] or "<none>")
        try:
            with open(os.path.join(dump_dir, f"{step}.console-network.json"), "w", encoding="utf-8") as f:
                json.dump({"console": browser_logs[-200:], "net_failures": failures, "http_errors": http_errors}, f, indent=1)
        except Exception as e:
            logger.error("join-debug step=%s could not save console/network json: %s", step, e)

    def capture_screenshot(self, path: str) -> bool:
        """Save what the bot's (Xvfb-headed) Chrome currently shows. Used by
        GET /meet-bots/{id}/screenshot for live tracing -- Meet blocks
        headless Chrome, so screenshots of the virtual display are the way
        to watch the bot instead."""
        try:
            driver = getattr(self, "driver", None)
            if driver is None:
                return False
            return bool(driver.save_screenshot(path))
        except Exception as e:
            logger.warning("capture_screenshot failed: %s", e)
            return False

    def fill_out_name_input(self) -> None:
        num_attempts = 30
        for attempt_index in range(num_attempts):
            try:
                name_input = self.retrieve_name_input_element()
                name_input.send_keys(self.bot_name)
                return
            except TimeoutException as e:
                self.look_for_blocked_element("name_input")
                self.check_if_meeting_is_found()
                # Heartbeat while waiting: log what page we're actually on
                # and leave a fresh screenshot so GET .../screenshot (or
                # docker cp of this file) shows the stuck state live.
                if attempt_index % 10 == 9:
                    try:
                        logger.warning(
                            "name_input still not found after %ds (url=%s title=%s)",
                            attempt_index + 1, self.driver.current_url, self.driver.title,
                        )
                    except Exception:
                        pass
                    self.capture_screenshot("/tmp/meet-join-progress.png")
                if attempt_index == num_attempts - 1:
                    self._dump_join_debug_info("name_input")
                    raise UiCouldNotLocateElementException("Could not find name input. Timed out.", "name_input", e) from e
            except (ElementNotInteractableException, StaleElementReferenceException) as e:
                if attempt_index == num_attempts - 1:
                    self._dump_join_debug_info("name_input")
                    raise UiCouldNotLocateElementException("Could not find name input. Non interactable or stale.", "name_input", e) from e
            time.sleep(1)

    def wait_for_host_if_needed(self) -> None:
        host_element = self.find_element_by_selector(By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')
        if not host_element:
            return
        logger.info("Waiting up to %ds for the host to start the meeting", self.wait_for_host_timeout_seconds)
        try:
            WebDriverWait(self.driver, self.wait_for_host_timeout_seconds).until(EC.invisibility_of_element_located((By.XPATH, '//*[contains(text(), "Waiting for the host to join")]')))
        except TimeoutException as e:
            raise UiCouldNotJoinMeetingWaitingForHostException("Host did not join the meeting in time", "wait_for_host_if_needed") from e

    def wait_until_admitted(self) -> None:
        """Poll until the in-call "Leave call" button appears (== admitted),
        or raise on denial/blocking/waiting-room timeout.

        Replaces the donor's captions-button retry loop (donor
        ``click_captions_button``, ~518-562), which used the captions button
        as its "we're actually in the call now" signal. This system never
        turns on captions, so it polls for the leave-call button instead --
        the same landmark ``click_leave_button`` already relies on.
        """
        waiting_room_timeout_started_at = time.time()
        poll_interval_seconds = 1.0

        while True:
            if any(self.find_element_by_selector(By.CSS_SELECTOR, s) if not s.startswith("//") else self.find_element_by_selector(By.XPATH, s) for s in _LEAVE_BUTTON_SELECTORS):
                return

            self.look_for_blocked_element("wait_until_admitted")
            self.look_for_denied_your_request_element("wait_until_admitted")
            self.click_this_meeting_is_being_recorded_join_now_button("wait_until_admitted")
            self.click_others_may_see_your_meeting_differently_button("wait_until_admitted")

            elapsed = time.time() - waiting_room_timeout_started_at
            if elapsed > self.waiting_room_timeout_seconds:
                self.look_for_asking_to_be_let_in_element_after_waiting_period_expired("wait_until_admitted")
                raise UiCouldNotJoinMeetingWaitingRoomTimeoutException("Waiting room timeout exceeded", "wait_until_admitted")

            time.sleep(poll_interval_seconds)

    def verify_expected_audio_configuration(self) -> None:
        if os.getenv("VERIFY_EXPECTED_AUDIO_CONFIGURATION_FOR_GOOGLE_MEET_BOT", "true") == "false":
            return
        audio_elements = self.driver.find_elements(By.CSS_SELECTOR, "audio")
        if len(audio_elements) == 0:
            raise UiGoogleWrongAudioConfigurationException("audio elements are not present", "verify_audio_elements_are_present")

    def attempt_to_join_meeting(self) -> None:
        """Single join attempt; raises a ``UiException`` subclass on any
        failure. Callers that want retry-with-fresh-driver semantics (as the
        donor's ``repeatedly_attempt_to_join_meeting`` does) implement that
        at the ``MeetBotSession``/joiner layer, not here."""
        from urllib.parse import urlparse

        # Browser.grantPermissions takes an *origin* (scheme + host), not a
        # full URL -- passing the meeting link with its path silently fails
        # on newer Chrome, leaving the mic/camera permission prompt up.
        # Grant before navigating so the permission applies on page load.
        origin = f"{urlparse(self.meeting_url).scheme}://{urlparse(self.meeting_url).netloc}"
        self.driver.execute_cdp_cmd(
            "Browser.grantPermissions",
            {
                "origin": origin,
                "permissions": ["audioCapture", "videoCapture", "displayCapture"],
            },
        )

        self.driver.get(self.meeting_url)

        self.check_if_meeting_is_found()
        self.fill_out_name_input()
        self.turn_off_media_inputs()
        self.verify_expected_audio_configuration()

        logger.info("Waiting for the 'Ask to join' / 'Join now' button...")
        try:
            join_button = self.locate_element(
                step="join_button",
                condition=EC.presence_of_element_located((By.XPATH, self.join_now_button_selector())),
                wait_time_seconds=60,
            )
        except UiCouldNotLocateElementException as e:
            self._dump_join_debug_info("join_button")
            raise
        logger.info("Clicking the join button...")
        self.click_element_with_fallback_to_forceful_click(join_button, "join_button")

        self.wait_for_host_if_needed()
        self.wait_until_admitted()

        logger.info("Joined meeting %s as %r", self.meeting_url, self.bot_name)

    def click_leave_button(self) -> None:
        # Best-effort: the bot may never have joined (e.g. name input was
        # never found), in which case no leave button exists. Keep per-try
        # waits short so cleanup of a failed join doesn't stall for ~80s.
        num_attempts = 3
        for attempt_index in range(num_attempts):
            for selector in _LEAVE_BUTTON_SELECTORS:
                by = By.XPATH if selector.startswith("//") else By.CSS_SELECTOR
                try:
                    leave_button = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((by, selector)))
                    leave_button.click()
                    return
                except Exception:
                    continue
            logger.warning("Leave button not found with any known selector, retrying (%d/%d)", attempt_index + 1, num_attempts)
        raise UiCouldNotLocateElementException("Could not find leave button with any known selector.", "leave_button")
