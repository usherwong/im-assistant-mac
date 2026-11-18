"""微信自动填入/发送的写入层：合成事件（CGEvent），高风险、默认关闭。

这是全项目唯一允许调用 CGEvent 的地方，且只在 `JEV_SYNTHETIC_INPUT=1` 时工作。
上游 #139（2026-09-28）记录过它的代价：合成鼠标/键盘事件被微信风控识别，多名用户
**账号被强制登出**，上游因此把整条「视觉填入」路径退役。本 fork 的自动回复是显式
的 opt-in 功能：默认关闭、白名单、限频、倒计时、熔断，出问题先关总闸。

流程（微信 4.2.0 的 AX 树读不到输入框，只能这样）：
  填入 = 点输入区 → 读剪贴板 → 写入候选 → ⌘V → OCR 复核 → 还原剪贴板
  发送 = 全窗 OCR 找「发送」按钮 → 合成点击 → 复核输入区已空

每一步都可验证：填完 OCR 读回，发完 OCR 确认输入框已清空。任一复核失败即停止，
不重试、不猜——宁可没发出去，也不能发错内容或发两次。
"""
from __future__ import annotations

import re
import time

import AppKit
import Quartz
import ApplicationServices as AS

import perception

SEND_RE = re.compile(r"^\s*(发送|Send)\b|^\s*发送[（(]?", re.I)
MIN_INPUT_TEXT = 2
CLICK_SETTLE_S = 0.12
PASTE_SETTLE_S = 0.30

REASON_GATE = "合成事件总闸未开启（JEV_SYNTHETIC_INPUT）"
REASON_NO_RECT = "没有可用的输入区位置"
REASON_DRAFT = "输入区已有草稿，自动填入已停止"
REASON_PASTE_FAILED = "粘贴后没读到候选内容"
REASON_NO_SEND = "没找到「发送」按钮，未发送"
REASON_SEND_FAILED = "点发送后输入区没有清空，可能没发出去"


def enabled() -> bool:
    import userconfig
    return (userconfig.get("JEV_SYNTHETIC_INPUT") or "").strip().lower() in ("1", "true", "yes", "on")


# ------------------------------------------------------------------ 事件

def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def click(point: tuple[float, float]) -> bool:
    """Synthetic left click at a global point (points, top-left origin)."""
    try:
        x, y = float(point[0]), float(point[1])
        down = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
        up = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
        _post(down)
        time.sleep(0.03)
        _post(up)
        time.sleep(CLICK_SETTLE_S)
        return True
    except Exception:
        return False


def _keystroke(keycode: int, flags: int = 0) -> bool:
    try:
        down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
        if flags:
            Quartz.CGEventSetFlags(down, flags)
            Quartz.CGEventSetFlags(up, flags)
        _post(down)
        time.sleep(0.02)
        _post(up)
        return True
    except Exception:
        return False


def cmd_v() -> bool:
    return _keystroke(9, Quartz.kCGEventFlagMaskCommand)      # kVK_ANSI_V


def press_return() -> bool:
    return _keystroke(36)                                     # kVK_Return


# ------------------------------------------------------------------ 读回

def _region(win: dict, rect: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    """Screen rect (points) → Vision region (normalized, bottom-origin) inside the window."""
    x, y, w, h = (float(v) for v in rect)
    if w <= 0 or h <= 0:
        return None
    nx = (x - win["x"]) / win["w"]
    ny = 1.0 - ((y + h - win["y"]) / win["h"])
    return (nx, ny, w / win["w"], h / win["h"])


def _ocr(win: dict, rect=None) -> list:
    image = perception.capture_image(win["wid"], nominal=False)
    if image is None:
        return []
    region = _region(win, rect) if rect else None
    return perception.ocr_image(image, chat_only=False, region=region)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def input_text(win: dict, rect) -> str:
    return "".join(b.text for b in sorted(_ocr(win, rect), key=lambda b: -b.y))


def _matches(candidate: str, seen: str) -> bool:
    """OCR readback check: the candidate's beginning must be there (OCR drops glyphs)."""
    want = _norm(candidate)
    got = _norm(seen)
    if not want or not got:
        return False
    head = want[:max(4, min(12, len(want)))]
    if head in got:
        return True
    import difflib
    return difflib.SequenceMatcher(None, want[:40], got).ratio() >= 0.55


# ------------------------------------------------------------------ 填入 / 发送

def fill(text: str, win: dict, rect) -> tuple[bool, str]:
    if not enabled():
        return False, REASON_GATE
    if not text or rect is None:
        return False, REASON_NO_RECT
    existing = input_text(win, rect)
    if _norm(existing):
        return False, REASON_DRAFT
    x, y, w, h = (float(v) for v in rect)
    if not click((x + w / 2, y + h / 2)):
        return False, "定位输入区失败"
    board = AppKit.NSPasteboard.generalPasteboard()
    previous = board.stringForType_(AppKit.NSPasteboardTypeString)
    try:
        board.clearContents()
        board.setString_forType_(text, AppKit.NSPasteboardTypeString)
        if not cmd_v():
            return False, REASON_PASTE_FAILED
        time.sleep(PASTE_SETTLE_S)
        seen = input_text(win, rect)
        if not _matches(text, seen):
            return False, REASON_PASTE_FAILED
        return True, "已填入输入框"
    finally:
        if previous is not None:
            board.clearContents()
            board.setString_forType_(previous, AppKit.NSPasteboardTypeString)


def _send_button(win: dict, rect) -> tuple[float, float] | None:
    """Find the 发送 button: OCR the window, keep blocks in the composer's row area."""
    blocks = _ocr(win, None)
    if not blocks:
        return None
    x, y, w, h = (float(v) for v in rect)
    best = None
    for b in blocks:
        if not SEND_RE.search(b.text) or len(b.text.strip()) > 10:
            continue
        # Vision block → window-normalized top-origin, then screen points
        bx = win["x"] + b.x * win["w"]
        by = win["y"] + (1.0 - b.y - b.h) * win["h"]
        bw, bh = b.w * win["w"], b.h * win["h"]
        if bx < x + w - 4:
            continue                       # must start at/after the composer's right edge
        # (校准输入区要求排除发送按钮；用起点而不是中心判定，避免把紧贴右缘的正文当按钮)
        if not (y - 2 * h <= by + bh / 2 <= y + 2.2 * h):
            continue                       # a menu item elsewhere in the window
        best = (bx + bw / 2, by + bh / 2)
    return best


def send(win: dict, rect, candidate: str = "") -> tuple[bool, str]:
    """Click 发送, or fall back to Return when the button is drawn without text.

    WeChat 4.x normally renders a 「发送(S)」 label, but some builds/themes draw an icon
    only. Return is the same keystroke a human would use; if the account is configured
    to Ctrl+Enter-to-send the composer keeps its text and the read-back below fails —
    which is exactly the safe outcome (nothing sent, surface the reason).
    """
    if not enabled():
        return False, REASON_GATE
    if rect is None:
        return False, REASON_NO_RECT
    target = _send_button(win, rect)
    if target is not None:
        if not click(target):
            return False, REASON_SEND_FAILED
        how = "已发送"
    elif press_return():
        how = "已发送（回车）"
    else:
        return False, REASON_NO_SEND
    time.sleep(0.45)
    left = input_text(win, rect)
    if _norm(left):
        return False, REASON_SEND_FAILED
    return True, how
