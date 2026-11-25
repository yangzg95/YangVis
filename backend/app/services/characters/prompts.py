"""人物三视图形象的提示词模板。

模板放在后端而不是前端：提示词是效果调优的核心资产，跟工作流内置 prompt 一样集中
管理，迭代措辞不用发一版前端；前端只负责收参考图与描述文本，接口保持最小。

三视图（character turnaround sheet）是一张横排三视角合一的设定图——正面、侧面、
背面同一个人，服装发型配色严格一致。它本来就是行业里角色一致性的标准参考图，存成
形象图后，首帧渲染会原样并进每镜的参考图请求。

两种入口共用同一套硬性要求，区别只在开场：

* 带参考图（图生图）：先钉死「三个视角必须与参考图是同一个人」，五官/发型/服装
  严格对齐参考图，描述文本只是补充要求、可空；
* 纯文字（文生图）：角色设定全部来自描述，因此描述必填（路由层兜底）。
"""
from __future__ import annotations

_HARD_REQUIREMENTS = """硬性要求：
1. 中性自然站姿，正面平视镜头，侧面与背面保持同一姿态，不要摆动作、不要道具；
2. 全身入画，从头到脚完整可见，双脚平稳站立，人物之间留少量间距；
3. 纯色浅灰背景，画面中不要出现文字、标签、水印、边框或装饰元素；
4. 写实风格，光线均匀柔和，五官与服装细节清晰，适合作为后续视频生成的角色参考图。"""

_TEXT_OPENING = """请生成一张人物三视图设定表（character turnaround sheet）：
同一位角色的正面、侧面（左侧面）、背面三个全身视角，从左到右并排站立，三个视角的身高、体型、发型、发色、服装与配色必须完全一致，像同一个人的三次转身。"""

_REFERENCE_OPENING = """请根据参考图生成该角色的人物三视图设定表（character turnaround sheet）：
同一位角色的正面、侧面（左侧面）、背面三个全身视角，从左到右并排站立。三个视角必须与参考图是同一个人——五官、脸型、发型、发色、体型、服装与配色严格一致，像参考图中人物的三次转身，不要更换人物或重新设计造型。"""


def build_turnaround_prompt(description: str, *, has_reference: bool = False) -> str:
    """拼装三视图提示词。

    ``has_reference`` 决定开场与描述段的写法：参考图模式下描述是可空的补充要求，
    纯文字模式下描述是唯一的角色依据（非空由路由层保证）。
    """
    clean = str(description or "").strip()
    opening = _REFERENCE_OPENING if has_reference else _TEXT_OPENING
    parts = [opening, _HARD_REQUIREMENTS]
    if has_reference:
        if clean:
            parts.append(f"补充要求：\n{clean}")
    else:
        parts.append(f"角色设定：\n{clean}")
    return "\n\n".join(parts)
