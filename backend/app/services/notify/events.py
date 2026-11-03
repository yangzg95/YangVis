"""可订阅的事件词表。

事件 key 写死在这里而不是靠字符串散落各处：订阅列表（``NotifyChannel.events``）
与触发点（``dispatch`` 调用方）都从这一份定义取值，拼错一个字母的事件会变成
「永远收不到通知」，排查起来完全没有方向——与 ``_normalize_auto_tags`` 卡死
词表是同一个理由。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EventDef:
    """一个可订阅事件的定义。"""

    key: str
    label: str
    # 新建渠道时默认勾选：失败类默认开，成功类默认关（避免轰炸）。
    default_on: bool


EVENTS: tuple[EventDef, ...] = (
    EventDef("workflow.failed", "工作流运行失败", True),
    EventDef("workflow.succeeded", "工作流运行成功", False),
    EventDef("ops.pending", "AI 提议运维命令，待确认", True),
    EventDef("ops.expired", "待确认命令已过期", True),
)

_VALID_KEYS = {event.key for event in EVENTS}


def is_known_event(key: str) -> bool:
    return key in _VALID_KEYS


def normalize_events(raw: list[str] | None) -> list[str]:
    """去重、过滤未知 key，保持传入顺序。

    未知 key 直接丢弃而不是报错：订阅列表来自前端勾选，多一个已下线的事件
    不该让整个更新请求 4xx。
    """
    if not raw:
        return []
    seen: list[str] = []
    for key in raw:
        if key in _VALID_KEYS and key not in seen:
            seen.append(key)
    return seen


def event_infos() -> list[dict]:
    """词表的可序列化视图，给 ``GET /notify/events`` 用。"""
    return [
        {"key": event.key, "label": event.label, "default_on": event.default_on}
        for event in EVENTS
    ]
