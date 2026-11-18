"""截图路径适配器：薄封装，全部转调 perception / fill，不改任何识别逻辑。

`CaptureApp(profile)` 用 apps/profiles.py 里的渠道配置决定找哪个窗口、读哪一栏、
校准存到哪两个键。微信（默认 profile）仍然走原来的 find_wechat_window /
perception.read_conversation 与 fill.py，行为不变。
"""
from __future__ import annotations

import fill
import perception
from apps.profiles import WECHAT


class CaptureApp:
    def __init__(self, profile=None):
        self.profile = profile or WECHAT
        self.key = self.profile.key
        self.display_name = self.profile.display_name
        self.bundle_ids = self.profile.bundle_ids
        self.app_names = self.profile.app_names
        self.needs_screen_capture = self.profile.needs_screen_capture

    def find_window(self, previous_wid=None):
        return perception.find_window(self.profile, previous_wid)

    def read_conversation(self, **kwargs):
        return perception.read_conversation(profile=self.profile, **kwargs)

    def locate_input(self, win):
        return fill.locate_input(win)

    def fill_text(self, text, target=None):
        return fill.fill_text(text, target=target)

    def warm(self):
        return perception.warm_ocr()
