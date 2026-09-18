"""Low-level X11 mouse/keyboard synthesis.

Near-verbatim port of attendee/bots/web_bot_adapter/x11_input.py -- it was
already Django-independent (only depends on ``python-xlib``). Kept for parity
with the donor's "resilient clicks + mouse wiggle" join behavior; this
adapter's default (non-humanized) click path uses plain Selenium clicks via
``ui_methods.py`` and does not require this module, but it's available for
callers that want to nudge the mouse before a click to avoid Meet's
occasional "not interactable" flakiness on a freshly-rendered button.
"""

from __future__ import annotations

import logging

from Xlib import XK, X, display
from Xlib.ext import xtest

logger = logging.getLogger(__name__)


class X11Input:
    def __init__(self) -> None:
        self.disp = display.Display()
        self.root = self.disp.screen().root

    def move_abs(self, x: int, y: int) -> None:
        xtest.fake_input(self.disp, X.MotionNotify, x=x, y=y)
        self.disp.sync()

    def move_rel(self, dx: int, dy: int) -> None:
        ptr = self.root.query_pointer()._data
        new_x = ptr["root_x"] + dx
        new_y = ptr["root_y"] + dy
        xtest.fake_input(self.disp, X.MotionNotify, x=new_x, y=new_y)
        self.disp.sync()

    def left_click(self) -> None:
        xtest.fake_input(self.disp, X.ButtonPress, 1)
        xtest.fake_input(self.disp, X.ButtonRelease, 1)
        self.disp.sync()

    def key(self, keycode: int) -> None:
        xtest.fake_input(self.disp, X.KeyPress, keycode)
        xtest.fake_input(self.disp, X.KeyRelease, keycode)
        self.disp.sync()

    BUTTON_MAP = {"left": 1, "middle": 2, "right": 3}

    def button_press(self, button_name: str) -> None:
        btn = self.BUTTON_MAP.get(button_name, 1)
        xtest.fake_input(self.disp, X.ButtonPress, btn)
        self.disp.sync()

    def button_release(self, button_name: str) -> None:
        btn = self.BUTTON_MAP.get(button_name, 1)
        xtest.fake_input(self.disp, X.ButtonRelease, btn)
        self.disp.sync()

    SPECIAL_KEY_MAP = {
        "Enter": "Return",
        "Backspace": "BackSpace",
        "Tab": "Tab",
        "Escape": "Escape",
        "ArrowUp": "Up",
        "ArrowDown": "Down",
        "ArrowLeft": "Left",
        "ArrowRight": "Right",
        "Shift": "Shift_L",
        "Control": "Control_L",
        "Alt": "Alt_L",
        "Meta": "Super_L",
        "CapsLock": "Caps_Lock",
        "Delete": "Delete",
        "Home": "Home",
        "End": "End",
        "PageUp": "Page_Up",
        "PageDown": "Page_Down",
        "Insert": "Insert",
        " ": "space",
    }

    def _key_name_to_keycode(self, key_name: str) -> int | None:
        xk_name = self.SPECIAL_KEY_MAP.get(key_name, key_name)
        keysym = XK.string_to_keysym(xk_name)
        if keysym == 0:
            return None
        return self.disp.keysym_to_keycode(keysym)

    def key_press(self, key_name: str) -> None:
        kc = self._key_name_to_keycode(key_name)
        if kc is None:
            logger.warning("Unknown key name: %s", key_name)
            return
        xtest.fake_input(self.disp, X.KeyPress, kc)
        self.disp.sync()

    def key_release(self, key_name: str) -> None:
        kc = self._key_name_to_keycode(key_name)
        if kc is None:
            logger.warning("Unknown key name: %s", key_name)
            return
        xtest.fake_input(self.disp, X.KeyRelease, kc)
        self.disp.sync()
