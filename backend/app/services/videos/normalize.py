"""分镜模型输出的清洗：把 JSON 里那一堆形状不受控的东西变成镜头行认得的字段。

为什么单独成模块而不是塞在 service 里：这一层的规则全是「模型可能怎么不守规矩」
——时长写成 "5秒"、shots 不是数组、提示词是一整段没有切分。这些与项目的存取无关，
改它的理由（模型换了一族、输出契约加一个字段）也不一样。

这里**不做**的是补内容：某一镜缺 image_prompt 就留空，由渲染入队那一步整批拒绝
（见 :meth:`VideoService.render_frames`），而不是替模型编一句。编出来的提示词
会烧掉一次额度，还把「模型没写」这件事藏进了产物里。
"""
from __future__ import annotations

from typing import Any, Dict, List

# 一镜最多这么多条：模型给超了是它没数过镜数，留着后面的镜头既挤掉前面的排序，
# 又让用户以为分镜就是这么多。
MAX_SHOTS_FROM_MODEL = 40


def text_of(value: Any, limit: int) -> str:
    """把任意值收成一段字符串：``None`` 变空串，数字转字符串，最后按列宽裁。

    模型的字段值经常是 ``None``、数字、或者一段带首尾空行的文本；直接进 ORM 的
    String 列会撞上 MySQL 的「Data too long」那类错，而那时报错点在写入侧，
    跟真正写坏的字段已经隔了好几步。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:limit]


def _duration_of(value: Any) -> int:
    """时长取整数秒：``"5秒"``、``5.0``、``"5"`` 都要能落下来。"""
    if isinstance(value, bool):
        return 5
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits else 5


def shots_from_model(raw: Any) -> List[Dict[str, Any]]:
    """把 ``shots`` 数组变成 :class:`VideoShotIn` 收得下的字典列表。

    不认识的非数组形状（模型把分镜写成 markdown 表格、或整个对象套了一层
    ``{"shots": {...}}``）一律返回空列表，让调用方按「一镜都没有」处理——
    那是一次明确的失败，比拿半截结构往下走强。
    """
    if isinstance(raw, dict):
        raw = raw.get("shots")
    if not isinstance(raw, list):
        return []

    shots: List[Dict[str, Any]] = []
    for item in raw[:MAX_SHOTS_FROM_MODEL]:
        if not isinstance(item, dict):
            continue
        # 一镜只有画面没有提示词，等于这一镜无法渲染；这种行留着就是列表里
        # 一个永远点不亮的格子，所以在这里就丢掉。
        image_prompt = text_of(item.get("image_prompt"), 1024)
        if not image_prompt:
            continue
        shots.append(
            {
                "duration_s": _duration_of(item.get("duration_s")),
                "scene": text_of(item.get("scene"), 512),
                "camera": text_of(item.get("camera"), 256),
                "action": text_of(item.get("action"), 256),
                "dialogue": text_of(item.get("dialogue"), 512),
                "on_screen_text": text_of(item.get("on_screen_text"), 256),
                "image_prompt": image_prompt,
                "video_prompt": text_of(item.get("video_prompt"), 1024),
                "gen_mode": text_of(item.get("gen_mode"), 24),
            }
        )
    return shots
