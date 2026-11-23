"""渠道配置：截图读取路径需要知道的、按 App 区分的常量。

P1 只放「找哪个进程、窗口多大算聊天窗」这类最小元数据；布局由各渠道的读取器
自己从界面结构里推（Telegram 走 AX 头部/输入框，微信继续用 perception 里的常量）。
"""
from __future__ import annotations

from dataclasses import dataclass

import fill
from perception import WECHAT_APP_NAMES


@dataclass(frozen=True)
class Profile:
    key: str
    display_name: str
    bundle_ids: tuple[str, ...]
    app_names: tuple[str, ...]
    min_window_w: float
    min_window_h: float
    needs_screen_capture: bool = True


WECHAT = Profile(
    key="wechat",
    display_name="聊天应用",
    bundle_ids=(fill.WECHAT_BUNDLE_ID,),
    app_names=WECHAT_APP_NAMES,
    min_window_w=500,
    min_window_h=400,
)

TELEGRAM = Profile(
    key="telegram",
    display_name="Telegram",
    bundle_ids=("ru.keepcoder.Telegram",),
    app_names=("Telegram",),
    # Telegram 默认窗口 480×616 点；微信的 500×400 门槛会把它漏掉。
    min_window_w=380,
    min_window_h=400,
)

PROFILES = {p.key: p for p in (WECHAT, TELEGRAM)}


def by_key(key: str) -> Profile | None:
    return PROFILES.get(key)
