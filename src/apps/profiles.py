"""Per-app profiles for the screenshot read path.

Everything the capture path needs to know about one chat app lives here: the bundle ids
and owner names that identify it, the window-size gate, the layout fractions the OCR
path was tuned on, UI noise patterns, how the conversation title is picked out of the
header band, and which env keys hold its manual calibration.

WeChat's values are the constants the OCR path was built and tuned on (perception.py);
Telegram's are from the 2026-11-18 probe (docs/probe/2026-11-18-telegram.md): its AX tree
exposes the input box and the send button but not the message list, so messages are read
by screenshot OCR while writes go through the Accessibility API.
"""
from __future__ import annotations

from dataclasses import dataclass

import fill
from perception import (CHAT_PANE_X_MIN, INPUT_AREA_Y_MIN, SIDEBAR_X_MAX,
                        TITLE_BAR_Y_MAX, UI_NOISE, WECHAT_APP_NAMES)


@dataclass(frozen=True)
class Layout:
    """Layout fractions of a window, all normalized and top-origin."""

    chat_pane_x_min: float
    title_bar_y_max: float
    input_area_y_min: float
    sidebar_x_max: float
    min_window_w: float
    min_window_h: float


@dataclass(frozen=True)
class Profile:
    key: str
    display_name: str
    bundle_ids: tuple[str, ...]
    app_names: tuple[str, ...]
    layout: Layout
    noise: tuple[str, ...] = UI_NOISE
    needs_screen_capture: bool = True
    # Some apps read fine without a manual selection (WeChat's auto layout). Apps whose
    # window layout is not auto-detected require the one-time calibration instead of
    # silently OCR-ing the wrong band.
    requires_calibration: bool = False
    # "top": the topmost header band is the conversation title (WeChat draws no title bar
    # text). "nearest": the header line closest to the message region wins — macOS draws
    # the window title ("Telegram @ account") above the conversation title.
    title_pick: str = "top"
    # Telegram draws the message time inside the bubble's bottom-right corner, so Vision
    # merges it into the body ("hello 19:18"). The capture path strips a trailing
    # whitespace-separated time plus read marks; WeChat's time is a separate line.
    strip_inline_time: bool = False

    @property
    def message_key(self) -> str:
        return ("JEV_MESSAGE_REGION" if self.key == "wechat"
                else f"JEV_MESSAGE_REGION_{self.key.upper()}")

    @property
    def input_key(self) -> str:
        return ("JEV_INPUT_REGION" if self.key == "wechat"
                else f"JEV_INPUT_REGION_{self.key.upper()}")


WECHAT = Profile(
    key="wechat",
    display_name="聊天应用",
    bundle_ids=(fill.WECHAT_BUNDLE_ID,),
    app_names=WECHAT_APP_NAMES,
    layout=Layout(chat_pane_x_min=CHAT_PANE_X_MIN,
                  title_bar_y_max=TITLE_BAR_Y_MAX,
                  input_area_y_min=INPUT_AREA_Y_MIN,
                  sidebar_x_max=SIDEBAR_X_MAX,
                  min_window_w=500,
                  min_window_h=400),
)

# Telegram's own UI strings that Vision may pick up inside the message band. Kept short:
# a real message can contain any of these words, and the region/ownership rules decide
# admission — noise patterns only steer the title band and the auto path.
TELEGRAM_NOISE = UI_NOISE + (
    r"^Write a message\.?$", r"^Saved Messages$", r"^Today$", r"^Yesterday$",
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(day)?\b",
    # Group/private-chat subtitle under the conversation title: "5 members, 2 online",
    # "23 members", "last seen recently". Without these the nearest-line title rule
    # picks the subtitle and every chat in a group collides on one key.
    r"^\d+\s+(members?|subscribers?)\b", r"^(typing|online|last seen)\b.*$",
)

TELEGRAM = Profile(
    key="telegram",
    display_name="Telegram",
    bundle_ids=("ru.keepcoder.Telegram",),
    app_names=("Telegram",),
    layout=Layout(chat_pane_x_min=0.0,
                  title_bar_y_max=0.90,
                  input_area_y_min=0.10,
                  sidebar_x_max=0.0,
                  min_window_w=380,
                  min_window_h=400),
    noise=TELEGRAM_NOISE,
    requires_calibration=True,
    title_pick="nearest",
    strip_inline_time=True,
)

PROFILES = {p.key: p for p in (WECHAT, TELEGRAM)}


def by_key(key: str) -> Profile | None:
    return PROFILES.get(key)
