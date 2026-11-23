"""Telegram 适配器：截图 OCR 读消息（apps/telegram_reader），AX 写入填入。

读：Telegram 不暴露消息行（AXRows -25200），走窗口截图 + Vision OCR；布局由
AX 头部/输入框自动推出，不需要手动校准。
写：输入框是 AXTextArea，写入后读回校验；不做发送、不合成事件。
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
        return perception.read_conversation(profile=TELEGRAM, **kwargs)

    def locate_input(self, win):
        return ax_input.locate_input(win, self.bundle_ids)

    def fill_text(self, text, target=None):
        return ax_input.fill_text(text, target=target)

    def warm(self):
        return perception.warm_ocr()
