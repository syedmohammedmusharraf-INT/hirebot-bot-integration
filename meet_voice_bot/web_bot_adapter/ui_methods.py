"""Generic, resilient Selenium element-finding/clicking helpers.

Ported from the click/locate helpers in attendee/bots/google_meet_bot_adapter/
google_meet_ui_methods.py (``locate_element``, ``find_element_by_selector``,
``click_element``, ``click_element_forcefully``,
``click_element_with_fallback_to_forceful_click``) -- these are not actually
Meet-specific in the donor despite living in that file, so they're factored
out here as a mixin any web-bot adapter can use. Meet-specific flows (name
input, join button, waiting room) live in ``google_meet_ui_methods.py``.
"""

from __future__ import annotations

import logging

from selenium.common.exceptions import ElementNotInteractableException, NoSuchElementException
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from .exceptions import UiCouldNotClickElementException, UiCouldNotLocateElementException

logger = logging.getLogger(__name__)


class ResilientUIMethods:
    """Mixin expecting ``self.driver: WebDriver`` on the including class."""

    driver: WebDriver

    def locate_element(self, step: str, condition, wait_time_seconds: int = 60) -> WebElement:
        try:
            return WebDriverWait(self.driver, wait_time_seconds).until(condition)
        except Exception as e:
            logger.warning("Exception raised in locate_element for %s: %s", step, type(e).__name__)
            raise UiCouldNotLocateElementException(f"Exception raised in locate_element for {step}", step, e) from e

    def find_element_by_selector(self, selector_type: str, selector: str) -> WebElement | None:
        try:
            return self.driver.find_element(selector_type, selector)
        except NoSuchElementException:
            return None
        except Exception:
            logger.warning("Unknown error occurred in find_element_by_selector for selector %s", selector)
            return None

    def click_element(self, element: WebElement, step: str) -> None:
        try:
            element.click()
        except Exception as e:
            logger.warning("Error clicking element for step %s (%s); may retry", step, type(e).__name__)
            raise UiCouldNotClickElementException("Error occurred when clicking element", step, e) from e

    def click_element_forcefully(self, element: WebElement, step: str) -> None:
        """Click via JS to sidestep ElementNotInteractableException."""
        try:
            self.driver.execute_script("arguments[0].click();", element)
        except Exception as e:
            logger.warning("Error forcefully clicking element for step %s; may retry", step)
            raise UiCouldNotClickElementException("Error occurred when forcefully clicking element", step, e) from e

    def click_element_with_fallback_to_forceful_click(self, element: WebElement, step: str) -> None:
        try:
            self.click_element(element, step)
        except UiCouldNotClickElementException as e:
            if isinstance(e.inner_exception, ElementNotInteractableException):
                logger.warning("Element was not interactable for step %s, falling back to forceful click", step)
                self.click_element_forcefully(element, step)
            else:
                raise

    def click_element_and_retry(self, element: WebElement, step: str, *, num_attempts: int = 5) -> None:
        """Retry a plain click a bounded number of times, re-raising the last failure."""
        for attempt_index in range(num_attempts):
            try:
                self.click_element(element, step)
                return
            except UiCouldNotClickElementException:
                if attempt_index == num_attempts - 1:
                    raise
                logger.warning("Retrying click for step %s (attempt %d/%d)", step, attempt_index + 1, num_attempts)
