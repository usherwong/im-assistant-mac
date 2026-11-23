"""Telegram 读取层：AX 定位布局 + 单次 ROI OCR + 像素指纹短路。

实测（2026-11-23，Telegram 13.x / macOS 27 / 480×616 窗口）：

* AX 树**不暴露消息行**：`AXRows` 返回 -25200、`AXCellForColumnAndRow` 返回 -25212，
  VoiceOver 关闭时也不变——正文只能截图 OCR。
* 但头部会话标题按钮、输入框、`Back` 按钮稳定可读：布局全靠它们推，不需要用户校准。
* 成本：整窗 OCR 541ms；消息区 OCR（2x）314–406ms；截图 104–128ms；像素指纹 3–8ms。
  所以标题/输入框走 AX（微秒级），正文只在**变化帧**OCR 一次，未变化帧用指纹短路。

窗口形态：
* 单栏（有 `Back` 按钮）：聊天区占满窗口宽度，左边界 = 0；
* 分栏（无 `Back`）：左边界 ≈ 标题按钮左边 − 3%，即聊天列表右缘。
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import AppKit
import ApplicationServices as AS

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import perception
from apps.profiles import TELEGRAM
from perception import Message

PAD_TOP = 0.008          # 标题按钮底边 → 消息区上边界
PAD_INPUT = 0.015        # 输入框顶边 → 消息区下边界
MIN_TEXTAREA = 800.0     # 点²；输入框实测 ~200×25
HEADER_BAND = 0.30       # 头部只在这个高度内找标题/Back
TITLE_MIN_W = 0.15       # 标题按钮至少占窗口宽 15%，用来排除图标按钮
MAX_NODES = 1200

# 头部 chrome 按钮：永不当作会话标题。
CHROME_BUTTONS = {"back", "search", "more", "call", "close", "minimize",
                  "settings", "menu", "forward", ""}

# 气泡右下角的时间/已读勾被 Vision 并进正文（实测「可以在这里测试 Jev 20:37」）。
# 只剥离「空格 + 时间」这种紧贴气泡右缘的尾巴；正好以时间结尾的正文会被误伤，
# 属已知取舍（与微信分支的 strip_inline_time 同一策略）。
INLINE_TIME_RE = re.compile(r"(?<=\s)\d{1,2}:\d{2}\s*[✓✔wWvV/]*\s*$")

# 系统分隔行/提示，不是待回复正文。
NOISE = (
    r"^(Today|Yesterday)$",
    r"^(今天|昨天|前天|刚刚)$",
    r"^星期[一二三四五六日天]$",
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)(day)?\b",
    r"^\d{1,2}月\d{1,2}日",
    r"^\d{4}年\d{1,2}月\d{1,2}日",
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}",
    r"\btyping\b",
    r"^\d+\s+(members?|subscribers?|monthly users)\b",
    r"^(online|last seen)\b",
)


@dataclass(frozen=True)
class Layout:
    """聊天区在窗口里的几何（归一化，顶部原点）。"""

    title: str
    top: float
    input_top: float
    pane_left: float
    pane_right: float
    input_rect: tuple[float, float, float, float] | None = None

    def key(self, win) -> tuple:
        return (win["wid"], round(win["w"]), round(win["h"]),
                round(self.top, 3), round(self.pane_left, 3), round(self.input_top, 3))


@dataclass
class _Line:
    text: str
    x: float
    y: float            # 顶部原点
    w: float
    h: float
    conf: float
    lines: list[str]


# ------------------------------------------------------------------ AX

def _attr(el, name):
    try:
        err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    except Exception:
        return None
    return value if err == 0 else None


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


def _walk(el, limit: int = MAX_NODES):
    stack = [el]
    seen = 0
    while stack and seen < limit:
        cur = stack.pop()
        seen += 1
        yield cur
        stack.extend(reversed(_children(cur)))


def _same_rect(a, b, tol: float = 2.0) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _app_pid(bundle_ids) -> int | None:
    for bundle in bundle_ids:
        try:
            apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
        except Exception:
            apps = None
        if apps:
            return int(apps[0].processIdentifier())
    return None


def _window_element(app_el, win: dict):
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


def _text_area(el):
    """最大且可写的 AXTextArea（输入框；搜索框更小）。"""
    best, best_area = None, 0.0
    for node in _walk(el):
        if (_attr(node, AS.kAXRoleAttribute) or "") != "AXTextArea":
            continue
        r = _rect(node)
        if r is None or r[3] <= 1:
            continue
        try:
            result = AS.AXUIElementIsAttributeSettable(node, AS.kAXValueAttribute, None)
            settable = result[1] if isinstance(result, tuple) else bool(result)
        except Exception:
            settable = False
        area = r[2] * r[3]
        if settable and area >= MIN_TEXTAREA and area > best_area:
            best, best_area = node, area
    return best


def layout_from_parts(win: dict, title: str, title_rect, input_rect,
                      has_back: bool) -> Layout | None:
    """把 AX 量到的矩形换算成聊天区几何；不合理就返回 None（交给上层报错）。"""
    if not title or title_rect is None or input_rect is None:
        return None
    wx, wy, ww, wh = (float(win["x"]), float(win["y"]), float(win["w"]), float(win["h"]))
    if ww <= 0 or wh <= 0:
        return None
    top = (title_rect[1] + title_rect[3] - wy) / wh + PAD_TOP
    input_top = (input_rect[1] - wy) / wh - PAD_INPUT
    pane_left = 0.0 if has_back else max(0.0, (title_rect[0] - wx) / ww - 0.03)
    if not (0.02 <= top <= 0.40):
        return None
    if not (0.50 <= input_top <= 0.99):
        return None
    if top + 0.10 >= input_top or pane_left >= 0.50:
        return None
    return Layout(title=title, top=top, input_top=input_top,
                  pane_left=pane_left, pane_right=1.0,
                  input_rect=tuple(float(v) for v in input_rect))


def probe_layout(win: dict) -> tuple[Layout | None, str | None]:
    """从 AX 树推聊天区几何；返回 (layout, error)。"""
    if not AS.AXIsProcessTrusted():
        return None, "需要辅助功能权限"
    pid = _app_pid(TELEGRAM.bundle_ids)
    if pid is None:
        return None, "Telegram 未运行"
    app_el = AS.AXUIElementCreateApplication(pid)
    win_el = _window_element(app_el, win)
    if win_el is None:
        return None, "Telegram 聊天窗口未找到"
    win_rect = _rect(win_el)
    if win_rect is None:
        return None, "读不到 Telegram 窗口尺寸"

    buttons = []
    for node in _walk(win_el):
        if (_attr(node, AS.kAXRoleAttribute) or "") != "AXButton":
            continue
        r = _rect(node)
        if r is None:
            continue
        label = (_attr(node, AS.kAXTitleAttribute)
                 or _attr(node, AS.kAXDescriptionAttribute) or "")
        label = str(label).strip()
        rel_y = (r[1] + r[3] / 2 - win_rect[1]) / max(win_rect[3], 1)
        rel_w = r[2] / max(win_rect[2], 1)
        buttons.append((label, r, rel_y, rel_w))

    title, title_rect = "", None
    for label, r, rel_y, rel_w in buttons:
        if not label or label.lower() in CHROME_BUTTONS:
            continue
        if rel_y > HEADER_BAND or rel_w < TITLE_MIN_W:
            continue
        if title_rect is None or r[2] > title_rect[2]:
            title, title_rect = label, r
    has_back = any(label.lower() == "back" and rel_y <= HEADER_BAND
                   for label, _r, rel_y, _w in buttons)
    text_area = _text_area(win_el)
    input_rect = _rect(text_area) if text_area is not None else None
    layout = layout_from_parts(win, title, title_rect, input_rect, has_back)
    if layout is None:
        return None, "Telegram 界面结构未识别（窗口过小或改版？）"
    return layout, None


# ------------------------------------------------------------------ 指纹 / OCR

def _crop(image, layout: Layout):
    from calibrated_messages import pixels
    rgb = pixels(image)
    h, w = rgb.shape[:2]
    y0 = max(0, int(layout.top * h))
    y1 = min(h, int(layout.input_top * h))
    x0 = max(0, int(layout.pane_left * w))
    x1 = min(w, int(layout.pane_right * w))
    if y1 <= y0 or x1 <= x0:
        return None
    return rgb[y0:y1, x0:x1]


def fingerprint(image, layout: Layout) -> bytes | None:
    crop = _crop(image, layout)
    return hashlib.sha1(crop.tobytes()).digest() if crop is not None else None


def ocr_region(layout: Layout) -> tuple[float, float, float, float]:
    return (layout.pane_left, 1.0 - layout.input_top,
            layout.pane_right - layout.pane_left, layout.input_top - layout.top)


# ------------------------------------------------------------------ 抽消息

def _is_noise(text: str) -> bool:
    if perception.TIMESTAMP_RE.match(text):
        return True
    return any(re.search(pattern, text, flags=re.I) for pattern in NOISE)


def clean_text(text: str) -> str:
    """去掉气泡内嵌的尾部时间/已读标记。"""
    return INLINE_TIME_RE.sub("", text).rstrip()


def side_for(x: float, w: float, layout: Layout) -> str:
    """气泡贴哪边就是哪一方；居中且不贴边的判 unknown（不当作回复目标）。"""
    span = max(layout.pane_right - layout.pane_left, 1e-6)
    left = (x - layout.pane_left) / span
    right = (x + w - layout.pane_left) / span
    if left <= 0.10:
        return "them"
    if right >= 0.90:
        return "me"
    center = (left + right) / 2
    if center <= 0.45:
        return "them"
    if center >= 0.55:
        return "me"
    return "unknown"


def _merge_same_line(blocks) -> list[_Line]:
    lines: list[_Line] = []
    for b in sorted(blocks, key=lambda b: (-b.y, b.x)):
        top = 1.0 - b.y - b.h
        if lines and abs(top - lines[-1].y) < 0.008:
            line = lines[-1]
            line.text = (line.text + " " + b.text).strip()
            right = max(line.x + line.w, b.x + b.w)
            line.x = min(line.x, b.x)
            line.w = right - line.x
            line.h = max(line.h, b.h)
            line.conf = min(line.conf, b.conf)
            line.lines = [line.text]
        else:
            lines.append(_Line(b.text.strip(), b.x, top, b.w, b.h, b.conf, [b.text.strip()]))
    return lines


def _fold(lines: list[_Line], layout: Layout) -> list[_Line]:
    """同一气泡里的折行合并：对齐、紧贴、同归属。"""
    folded: list[_Line] = []
    for line in lines:
        prev = folded[-1] if folded else None
        if prev is not None:
            gap = line.y - (prev.y + prev.h)
            aligned = abs(line.x - prev.x) < 0.015
            same_side = side_for(line.x, line.w, layout) == side_for(prev.x, prev.w, layout)
            if aligned and same_side and -0.01 <= gap <= 0.006:
                prev.lines.append(line.text)
                prev.text = "\n".join(prev.lines)
                prev.conf = min(prev.conf, line.conf)
                bottom = max(prev.y + prev.h, line.y + line.h)
                right = max(prev.x + prev.w, line.x + line.w)
                prev.x = min(prev.x, line.x)
                prev.w = right - prev.x
                prev.h = bottom - prev.y
                continue
        folded.append(line)
    return folded


def extract_messages(blocks, layout: Layout, max_messages: int = 12) -> list[Message]:
    usable = [b for b in blocks
              if b.conf >= perception.MIN_CONF and b.text.strip()
              and not _is_noise(b.text.strip())]
    lines = _fold(_merge_same_line(usable), layout)
    out: list[Message] = []
    for line in lines:
        cleaned = [part for part in (clean_text(part) for part in line.lines) if part]
        text = "\n".join(cleaned)
        if not text or perception.TIMESTAMP_RE.match(text):
            continue
        side = side_for(line.x, line.w, layout)
        # 群聊发送者昵称：短、偏小、贴左，不是待回复正文（正文气泡字号更大）。
        if side == "them" and len(text) <= 16 and line.h < 0.021:
            continue
        out.append(Message(text=text, side=side, y=line.y, conf=line.conf,
                           h=line.h, lines=cleaned, x=line.x, w=line.w,
                           last_y=line.y))
    out.sort(key=lambda m: m.y)
    return out[-max_messages:]


# ------------------------------------------------------------------ 读取

def read_telegram(max_messages: int = 12, previous_wid: int | None = None,
                  prev_fingerprint: bytes | None = None,
                  prev_layout=None) -> dict:
    t0 = time.perf_counter()
    win = perception.find_window(TELEGRAM, previous_wid)
    if win is None:
        return {"ok": False, "error": "Telegram 聊天窗口未找到", "messages": []}
    window = {"wid": win.wid, "title": win.title, "w": win.w, "h": win.h,
              "x": win.x, "y": win.y}
    layout, error = probe_layout(window)
    if layout is None:
        return {"ok": False, "error": error or "Telegram 界面结构未识别", "messages": []}

    image = perception.capture_image(win.wid, nominal=False)
    if image is None:
        return {"ok": False, "error": "capture failed", "messages": []}
    capture_ms = (time.perf_counter() - t0) * 1000

    fp = fingerprint(image, layout)
    key = layout.key(window)
    if key == prev_layout and fp is not None and fp == prev_fingerprint:
        total = (time.perf_counter() - t0) * 1000
        return {"ok": True, "unchanged": True, "messages": [], "window": window,
                "chat_title": layout.title, "n_blocks": 0, "fingerprint": fp,
                "layout": key, "input_rect": layout.input_rect,
                "timing_ms": {"capture": total, "ocr": 0.0, "extract": 0.0,
                              "total": total, "capture_path": "subprocess"}}

    t_ocr = time.perf_counter()
    blocks = perception.ocr_image(image, chat_only=False, region=ocr_region(layout))
    ocr_ms = (time.perf_counter() - t_ocr) * 1000
    t_ext = time.perf_counter()
    msgs = extract_messages(blocks, layout, max_messages)
    extract_ms = (time.perf_counter() - t_ext) * 1000
    total = (time.perf_counter() - t0) * 1000
    return {"ok": True, "unchanged": False, "messages": msgs, "window": window,
            "chat_title": layout.title, "n_blocks": len(blocks), "fingerprint": fp,
            "layout": key, "input_rect": layout.input_rect,
            "timing_ms": {"capture": capture_ms, "ocr": ocr_ms,
                          "extract": extract_ms, "total": total,
                          "capture_path": "subprocess"}}


# ------------------------------------------------------------------ CLI 自测

def main() -> int:  # pragma: no cover - 真机自测入口
    import json
    res = read_telegram()
    if not res["ok"]:
        print("ERROR:", res["error"])
        return 1
    t = res["timing_ms"]
    win = res["window"]
    layout, err = probe_layout(win)
    print(f"window {win['w']:.0f}x{win['h']:.0f} title={res['chat_title']!r} blocks={res['n_blocks']}")
    if layout is not None:
        print(f"layout top={layout.top:.3f} input_top={layout.input_top:.3f} "
              f"pane_left={layout.pane_left:.3f} error={err}")
    else:
        print("layout: 未识别", err)
    print(f"capture={t['capture']:.0f}ms ocr={t['ocr']:.0f}ms extract={t['extract']:.0f}ms "
          f"total={t['total']:.0f}ms unchanged={res['unchanged']}")
    for m in res["messages"]:
        print(f"  [{m.side:7s}] y={m.y:.3f} h={m.h:.3f} x={m.x:.2f} w={m.w:.2f} | {m.text[:40]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
