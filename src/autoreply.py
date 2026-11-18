"""微信自动回复的决策层（纯函数，离线可测）。

只回答一个问题：这条消息、这个会话，现在要不要自动填入/自动发送？真正的写入与
发送在 wechat_auto.py（合成事件，受 JEV_SYNTHETIC_INPUT 总闸约束）。

默认全部关闭，而且白名单为空时什么都不做——自动回复不是「装上就回所有人」，
必须显式把会话加进白名单（菜单栏「把当前会话加入自动回复白名单」）。

配置（用户 env，见 userconfig）：
  JEV_AUTO_FILL=1|0          自动填入候选（默认 0）
  JEV_AUTO_SEND=1|0          自动发送（默认 0；还需 JEV_AUTO_FILL=1）
  JEV_AUTO_DELAY=5           填入后到发送前的倒计时秒数（0–60）
  JEV_AUTO_ALLOWLIST=标题A|标题B   只对这些会话生效；空 = 全部不生效
  JEV_AUTO_MAX_RISK=6        风险超过该值不自动回复（0–9）
  JEV_AUTO_MIN_GAP=30        两条自动回复之间至少间隔的秒数（0–3600）
  JEV_SYNTHETIC_INPUT=1|0    合成事件总闸（默认 0；微信 4.2.0 写入/发送都依赖它）
  JEV_AUTO_ACK=1             已确认高风险（#139 强制登出）提示，不再弹窗
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import userconfig

MAX_DELAY = 60.0
MAX_GAP = 3600.0
MAX_TITLE = 120


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _number(raw: str | None, default: float, lo: float, hi: float) -> float:
    try:
        value = float((raw or "").strip())
    except (TypeError, ValueError):
        return default
    if value != value:                 # NaN
        return default
    return max(lo, min(hi, value))


def title_key(text: str | None) -> str:
    """Whitelist key: OCR pads/splits titles, so collapse whitespace and trim."""
    return " ".join((text or "").split())


def parse_allowlist(raw: str | None) -> tuple[str, ...]:
    """'标题A|标题B' → tuple. 空项与超长项丢弃；标题里的 `|` 无法区分，不进白名单。"""
    out = []
    for part in (raw or "").split("|"):
        title = title_key(part)
        if not title or len(title) > MAX_TITLE:
            continue
        if title not in out:
            out.append(title)
    return tuple(out)


def dump_allowlist(titles: tuple[str, ...]) -> str:
    return "|".join(t for t in titles if t and "|" not in t)


@dataclass(frozen=True)
class Config:
    auto_fill: bool = False
    auto_send: bool = False
    delay: float = 5.0
    allowlist: tuple[str, ...] = ()
    max_risk: int = 6
    min_gap: float = 30.0
    synthetic: bool = False
    acknowledged: bool = False

    @property
    def active(self) -> bool:
        return self.auto_fill


def load(getter=None) -> Config:
    get = getter or userconfig.get
    return Config(
        auto_fill=_truthy(get("JEV_AUTO_FILL")),
        auto_send=_truthy(get("JEV_AUTO_SEND")),
        delay=_number(get("JEV_AUTO_DELAY"), 5.0, 0.0, MAX_DELAY),
        allowlist=parse_allowlist(get("JEV_AUTO_ALLOWLIST")),
        max_risk=int(_number(get("JEV_AUTO_MAX_RISK"), 6, 0, 9)),
        min_gap=_number(get("JEV_AUTO_MIN_GAP"), 30.0, 0.0, MAX_GAP),
        synthetic=_truthy(get("JEV_SYNTHETIC_INPUT")),
        acknowledged=_truthy(get("JEV_AUTO_ACK")),
    )


@dataclass(frozen=True)
class Plan:
    """要不要动手，以及为什么。reason 会直接进状态行/日志，所以读起来是句子。"""

    fill: bool = False
    send: bool = False
    reason: str = ""
    config: Config | None = field(default=None, repr=False)


def plan(config: Config, *, app_key: str, chat_title: str, risk: float | None,
         last_sent_at: float | None, now: float) -> Plan:
    """One decision per incoming message. Order matters: the reasons are user-facing."""
    if not config.auto_fill:
        return Plan(reason="自动填入未开启", config=config)
    if app_key != "wechat":
        return Plan(reason="自动回复目前只覆盖微信", config=config)
    if not config.synthetic:
        return Plan(reason="合成事件总闸未开启（JEV_SYNTHETIC_INPUT）", config=config)
    title = title_key(chat_title)
    if not title:
        return Plan(reason="会话标题为空，无法核对白名单", config=config)
    if title not in config.allowlist:
        return Plan(reason="会话不在自动回复白名单", config=config)
    if risk is not None and risk > config.max_risk:
        return Plan(reason=f"风险 {risk:.0f} 高于阈值 {config.max_risk}", config=config)
    if last_sent_at is not None and now - last_sent_at < config.min_gap:
        wait = int(config.min_gap - (now - last_sent_at)) + 1
        return Plan(reason=f"限频中，{wait}s 后才能再自动回复", config=config)
    return Plan(fill=True, send=bool(config.auto_send), reason="准备自动回复", config=config)


def with_allowlist(config: Config, title: str) -> Config:
    title = title_key(title)
    if not title or title in config.allowlist or "|" in title:
        return config
    return replace(config, allowlist=config.allowlist + (title,))


def without_allowlist(config: Config, title: str) -> Config:
    return replace(config, allowlist=tuple(t for t in config.allowlist if t != title_key(title)))
