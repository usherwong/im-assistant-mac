"""Accessibility write path for native chat apps (Telegram): input box, send button.

The WeChat write path lives in fill.py and stays there — it carries WeChat-specific
window/role heuristics and the #139 history. This module is for apps whose AX tree
exposes a real `AXTextArea` and a real send `AXButton`; Telegram 13.x does
(docs/probe/2026-11-18-telegram.md):

  * the message list is NOT in the tree (`AXList` children = 0), so reading is OCR;
  * the composer is an `AXTextArea` whose `AXValue` can be written — but Telegram
    returns `kAXErrorFailure` (-25200) even when the write lands, so success is decided
    by reading the value back, never by the return code;
  * with text in the box a `AXButton("Send")` appears (it replaces the voice-message
    button); it supports `AXPress`.

Nothing here synthesizes events or touches the pasteboard: writes are AX attribute
writes and sending is an AX action, so there is no HID-level automation for the app's
risk control to detect. A press is verified the same way — the composer must come back
empty (or different) — and never retried, so a silent failure cannot double-send.
"""
from __future__ import annotations

import time

import AppKit
import ApplicationServices as AS

import fill

# Locale variants of the send button label. Only a hint: when the current UI language is
# not listed, the rightmost enabled button in the composer row is used instead.
SEND_LABELS = ("send", "发送", "送出", "senden", "abschicken", "enviar", "enviar",
               "envoyer", "invia", "отправить", "送信")
MIN_TEXT_AREA = 800.0      # points^2; the composer is ~200x25, search fields are smaller
WALK_LIMIT = 800

REASON_NO_APP = "没找到目标聊天应用"
REASON_NO_WINDOW = "聊天窗口未找到"
REASON_NO_INPUT = "未取得可用的聊天输入控件"
REASON_DRAFT = "输入区已有草稿，已停止填入（不会覆盖你的草稿）"
REASON_NOT_VERIFIED = "写入后没读到内容，可能没填进去"
REASON_NO_SEND = "没找到发送按钮，未发送"
REASON_SEND_FAILED = "发送动作没有生效，未发送"


def _attr(el, name):
    try:
        err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    except Exception:
        return None
    return value if err == 0 else None


def _role(el) -> str:
    v = _attr(el, AS.kAXRoleAttribute)
    return v if isinstance(v, str) else ""


def _rect(el):
    """(x, y, w, h) in screen points, or None. AX and CG agree on top-left points."""
    pos = _attr(el, AS.kAXPositionAttribute)
    size = _attr(el, AS.kAXSizeAttribute)
    if pos is None or size is None:
        return None
    try:
        ok1, point = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        ok2, span = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    except Exception:
        return None
    if not (ok1 and ok2):
        return None
    return (float(point.x), float(point.y), float(span.width), float(span.height))


def _children(el) -> list:
    try:
        return list(_attr(el, AS.kAXChildrenAttribute) or [])
    except TypeError:
        return []


def _enabled(el) -> bool:
    v = _attr(el, AS.kAXEnabledAttribute)
    return True if v is None else bool(v)


def walk(el, limit: int = WALK_LIMIT):
    """Bounded depth-first walk; a busy window tree is large and a miss must stay cheap."""
    stack = [el]
    seen = 0
    while stack and seen < limit:
        cur = stack.pop()
        seen += 1
        yield cur
        stack.extend(reversed(_children(cur)))


def running_pid(bundle_ids: tuple[str, ...]) -> int | None:
    for bundle in bundle_ids:
        try:
            apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
        except Exception:
            apps = None
        if apps:
            return int(apps[0].processIdentifier())
    return None


def _app_element(pid: int):
    try:
        return AS.AXUIElementCreateApplication(pid)
    except Exception:
        return None


def _windows(app_el) -> list:
    try:
        return list(_attr(app_el, AS.kAXWindowsAttribute) or [])
    except TypeError:
        return []


def window_element(app_el, win: dict):
    """The AX window matching the captured window rect; focused window as the fallback.

    Geometry is the only bridge between the CGWindow id the read path uses and the AX
    window object the write path needs — same trick as the QQ adapter.
    """
    target = (win.get("x"), win.get("y"), win.get("w"), win.get("h"))
    candidates = _windows(app_el)
    if target and all(v is not None for v in target):
        for el in candidates:
            r = _rect(el)
            if r is not None and fill._same_rect(r, tuple(float(v) for v in target)):
                return el
    focused = _attr(app_el, AS.kAXFocusedWindowAttribute)
    if focused is not None:
        return focused
    if not candidates:
        return None
    def area(el):
        r = _rect(el)
        return r[2] * r[3] if r else 0.0
    return max(candidates, key=area)


def find_text_area(window_el):
    """Largest settable AXTextArea in the window (the composer; search boxes are smaller)."""
    best, best_area = None, 0.0
    for el in walk(window_el):
        if _role(el) != "AXTextArea":
            continue
        r = _rect(el)
        if r is None or r[3] <= 1:
            continue
        try:
            result = AS.AXUIElementIsAttributeSettable(el, AS.kAXValueAttribute, None)
            settable = result[1] if isinstance(result, tuple) else bool(result)
        except Exception:
            settable = False
        area = r[2] * r[3]
        if settable and area >= MIN_TEXT_AREA and area > best_area:
            best, best_area = el, area
    return best


def _resolve(win: dict, bundle_ids):
    if not fill.has_accessibility():
        return None, None, fill.REASON_NO_ACCESS
    pid = running_pid(bundle_ids)
    if pid is None:
        return None, None, REASON_NO_APP
    app_el = _app_element(pid)
    if app_el is None:
        return None, None, REASON_NO_APP
    win_el = window_element(app_el, win)
    if win_el is None:
        return None, None, REASON_NO_WINDOW
    return app_el, win_el, None


# Header chrome buttons that are never the conversation title.
HEADER_BUTTONS = {"back", "search", "more", "close", "minimize", "settings", "menu", ""}


def title_from_buttons(buttons, win_rect) -> str:
    """Pick the conversation title button out of the window header.

    Telegram draws the chat header as one wide AXButton ("BotFather", group name, …)
    between the Back and Search buttons. It is the widest labelled button in the top
    third of the window — far more reliable than OCR-ing a header band whose position
    depends on the user's calibration.
    """
    if not win_rect:
        return ""
    wx, wy, ww, wh = win_rect
    best = None
    for label, r in buttons:
        lab = (label or "").strip()
        if not lab or lab.lower() in HEADER_BUTTONS or r is None:
            continue
        x, y, w, h = r
        if (y + h / 2 - wy) / wh > .30:          # header only
            continue
        if w / ww < .15:                         # narrow buttons are icons (Back/Search/More)
            continue
        if best is None or w > best[1]:
            best = (lab, w)
    return best[0] if best else ""


def chat_title(win: dict, bundle_ids: tuple[str, ...]) -> str:
    """Conversation title straight from the AX header button (empty when unavailable)."""
    _, win_el, _ = _resolve(win, bundle_ids)
    if win_el is None:
        return ""
    rect = _rect(win_el)
    if rect is None:
        return ""
    buttons = []
    for el in walk(win_el):
        if _role(el) != "AXButton":
            continue
        label = _attr(el, AS.kAXTitleAttribute) or _attr(el, AS.kAXDescriptionAttribute) or ""
        buttons.append((str(label), _rect(el)))
    return title_from_buttons(buttons, rect)


def locate_input(win: dict, bundle_ids: tuple[str, ...]) -> dict:
    """Return the composer element plus its screen rect; HUD's fill target shape."""
    base = {"box": None, "rect": None, "window": dict(win), "reason": None}
    _, win_el, reason = _resolve(win, bundle_ids)
    if win_el is None:
        return dict(base, reason=reason)
    box = find_text_area(win_el)
    if box is None:
        return dict(base, reason=REASON_NO_INPUT)
    return dict(base, box=box, rect=_rect(box), reason=None)


def read_value(box) -> str | None:
    v = _attr(box, AS.kAXValueAttribute)
    return v if isinstance(v, str) else None


def set_value(box, text: str) -> bool:
    """Write + read back. Telegram reports failure (-25200) on a write that landed."""
    try:
        AS.AXUIElementSetAttributeValue(box, AS.kAXValueAttribute, text)
    except Exception:
        pass
    if read_value(box) == text:
        return True
    time.sleep(0.05)          # one short retry for a slow text view
    return read_value(box) == text


def fill_text(text: str, target=None) -> tuple[bool, str]:
    """Put the candidate into the composer. Never overwrites an existing draft."""
    if not fill.has_accessibility():
        return False, fill.REASON_NO_ACCESS
    box = (target or {}).get("box")
    if box is None:
        return False, REASON_NO_INPUT
    current = read_value(box)
    if current is None:
        return False, REASON_NOT_VERIFIED
    if current.strip():
        return False, REASON_DRAFT
    if not set_value(box, text):
        return False, REASON_NOT_VERIFIED
    return True, "已填入输入框"


def _same_row(br, r, slack: float) -> bool:
    """Button sits in the composer row: vertical centres within one text-box height."""
    by = br[1] + br[3] / 2
    y = r[1] + r[3] / 2
    return abs(by - y) <= max(slack, 1.0)


def find_send_button(window_el, box=None):
    """The button that sends what is in the composer.

    Telegram swaps the voice-message button for a send button once text exists, so the
    send button is the *rightmost* enabled button sharing the composer's row, just right
    of the text area. A localized label is used as the first hint only; the geometry
    fallback keeps languages and layout tweaks from silently disabling sending.
    """
    br = _rect(box) if box is not None else None
    if br is None:
        return None
    cands = []
    for el in walk(window_el):
        if _role(el) != "AXButton" or not _enabled(el):
            continue
        r = _rect(el)
        if r is None or r[2] <= 1 or r[3] <= 1:
            continue
        if r[0] < br[0] + br[2] - 4:          # must sit to the right of the composer
            continue
        if not _same_row(br, r, br[3]):
            continue
        label = (_attr(el, AS.kAXTitleAttribute) or _attr(el, AS.kAXDescriptionAttribute) or "")
        cands.append((str(label).strip().lower(), r, el))
    if not cands:
        return None
    for label, _, el in cands:
        if label in SEND_LABELS:
            return el
    return max(cands, key=lambda c: c[1][0] + c[1][2])[2]


def press(el) -> bool:
    try:
        return AS.AXUIElementPerformAction(el, AS.kAXPressAction) == 0
    except Exception:
        return False


def send(win: dict, bundle_ids: tuple[str, ...]) -> tuple[bool, str]:
    """Press send and verify the composer emptied. Never retried."""
    _, win_el, reason = _resolve(win, bundle_ids)
    if win_el is None:
        return False, reason
    box = find_text_area(win_el)
    if box is None:
        return False, REASON_NO_INPUT
    before = read_value(box) or ""
    if not before.strip():
        return False, "输入区是空的，没有可发送的内容"
    button = find_send_button(win_el, box)
    if button is None:
        return False, REASON_NO_SEND
    if not press(button):
        return False, REASON_SEND_FAILED
    for _ in range(10):                   # up to ~0.4s for the view to clear
        time.sleep(0.04)
        after = read_value(box)
        if after is None or after != before:
            return True, "已发送"
    return False, REASON_SEND_FAILED
