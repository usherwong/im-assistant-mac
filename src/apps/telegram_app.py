"""Telegram 渠道：截图 OCR 读消息 + 无障碍接口写入与发送。

读：Telegram 13.x 的 AX 树不暴露消息列表（`AXList` 的 children 恒为 0），只能窗口
截图 + Apple Vision OCR；读到的正文用气泡表面判「我/对方」。实测见
docs/probe/2026-11-18-telegram.md。

写：输入框是 `AXTextArea`（可写、需读回校验），发送是 `AXButton("Send")` 的
`AXPress` 动作——不合成任何键盘/鼠标事件，也不碰剪贴板。微信那条写入路径
（fill.py）不动。

校准：Telegram 的窗口布局不做自动推断（侧栏可隐藏、气泡贴边方式与微信不同），
首次使用要求手动校准消息区与输入区，按键名按 app 分开存
（`JEV_MESSAGE_REGION_TELEGRAM` / `JEV_INPUT_REGION_TELEGRAM`）。
"""
from __future__ import annotations

import ax_input
import perception
from apps.profiles import TELEGRAM


class TelegramApp:
    key = TELEGRAM.key
    display_name = TELEGRAM.display_name
    bundle_ids = TELEGRAM.bundle_ids
    app_names = TELEGRAM.app_names
    needs_screen_capture = TELEGRAM.needs_screen_capture

    def find_window(self, previous_wid=None):
        return perception.find_window(TELEGRAM, previous_wid)

    def read_conversation(self, **kwargs):
        res = perception.read_conversation(profile=TELEGRAM, **kwargs)
        # 标题以 AX 头部按钮为准：校准区可能把聊天区第一行圈进去，OCR 标题会变成
        # 消息正文（实测 BotFather 读成「/mybots - edit your bots」）。白名单靠它，
        # 必须稳定。
        if res.get("ok") and res.get("window"):
            try:
                title = ax_input.chat_title(res["window"], self.bundle_ids)
            except Exception:
                title = ""
            if title:
                res = dict(res, chat_title=title)
        return res

    def locate_input(self, win):
        return ax_input.locate_input(win, self.bundle_ids)

    def fill_text(self, text, target=None):
        return ax_input.fill_text(text, target=target)

    def warm(self):
        return perception.warm_ocr()
