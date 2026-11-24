"""人物三视图形象的提示词模板。

模板放在后端而不是前端：提示词是效果调优的核心资产，跟工作流内置 prompt 一样集中
管理，迭代措辞不用发一版前端；前端只负责收角色描述文本，接口保持最小。

三视图（character turnaround sheet）是一张横排三视角合一的设定图——正面、侧面、
背面同一个人，服装发型配色严格一致。它本来就是行业里角色一致性的标准参考图，存成
形象图后，首帧渲染会原样并进每镜的参考图请求。
"""
from __future__ import annotations

# 描述上限与路由 schema 的字段上限同档，模板本身不再截，由 pydantic 挡超长输入。
TURNAROUND_PROMPT_TEMPLATE = """请生成一张人物三视图设定表（character turnaround sheet）：
同一位角色的正面、侧面（左侧面）、背面三个全身视角，从左到右并排站立，三个视角的身高、体型、发型、发色、服装与配色必须完全一致，像同一个人的三次转身。

硬性要求：
1. 中性自然站姿，正面平视镜头，侧面与背面保持同一姿态，不要摆动作、不要道具；
2. 全身入画，从头到脚完整可见，双脚平稳站立，人物之间留少量间距；
3. 纯色浅灰背景，画面中不要出现文字、标签、水印、边框或装饰元素；
4. 写实风格，光线均匀柔和，五官与服装细节清晰，适合作为后续视频生成的角色参考图。

角色设定：
{description}"""


def build_turnaround_prompt(description: str) -> str:
    """把用户的角色描述套进三视图模板；入口处已保证非空白。"""
    return TURNAROUND_PROMPT_TEMPLATE.format(description=str(description or "").strip())
