"""Telegram（原生 App）的 AX 写入层：定位输入框、写入并读回校验。

Telegram 的 `AXTextArea` 可以写，但 `AXUIElementSetAttributeValue` 经常返回
-25200（kAXErrorFailure）而值其实已经写进去了——成功与否只认读回，不看返回码。
不做发送、不碰剪贴板、不合成事件（P2 范围：读 + 建议 + 填入）。
"""
from __future__ import annotations

import time

import AppKit
import ApplicationServices as AS

import fill

MIN_TEXT_AREA = 800.0     # 点²；输入框实测 ~200×25，搜索框更小
WALK_LIMIT = 800

REASON_NO_APP = "没找到目标聊天应用"
REASON_NO_WINDOW = "聊天窗口未找到"
REASON_NO_INPUT = "未取得可用的聊天输入控件"
REASON_DRAFT = "输入区已有草稿，已停止填入（不会覆盖你的草稿）"
REASON_NOT_VERIFIED = "写入后没读到内容，可能没填进去"


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


def walk(el, limit: int = WALK_LIMIT):
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


def _same_rect(a, b, tol: float = 2.0) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def window_element(app_el, win: dict):
    """按几何把 CGWindow 和 AX 窗口对上；对不上就退回焦点窗口/最大窗口。"""
    windows = _attr(app_el, AS.kAXWindowsAttribute) or []
    target = (win.get("x"), win.get("y"), win.get("w"), win.get("h"))
    if all(v is not None for v in target):
        for el in windows:
            if _same_rect(_rect(el), tuple(float(v) for v in target)):
                return el
    focused = _attr(app_el, AS.kAXFocusedWindowAttribute)
    if focused is not None:
        return focused
    if not windows:
        return None

    def area(el):
        r = _rect(el)
        return r[2] * r[3] if r else 0.0

    return max(windows, key=area)


def find_text_area(window_el):
    """窗口里最大且可写的 AXTextArea（输入框）。"""
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
    app_el = AS.AXUIElementCreateApplication(pid)
    win_el = window_element(app_el, win)
    if win_el is None:
        return None, None, REASON_NO_WINDOW
    return app_el, win_el, None


def locate_input(win: dict, bundle_ids: tuple[str, ...]) -> dict:
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
    """写入 + 读回。Telegram 返回 -25200 但可能已写入，所以不看返回码。"""
    try:
        AS.AXUIElementSetAttributeValue(box, AS.kAXValueAttribute, text)
    except Exception:
        pass
    if read_value(box) == text:
        return True
    time.sleep(0.05)
    return read_value(box) == text


def fill_text(text: str, target=None) -> tuple[bool, str]:
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
