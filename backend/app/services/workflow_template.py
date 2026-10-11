"""模板渲染：把 ``{{name}}`` / ``{{name.字段}}`` 换成上下文里的值。

**变量替换不做条件判断**。模板只有上面两种引用；「有补充描述才加这一段」这类
拼装由调用方的输入构造代码完成（见 ``services/resume.py`` 的
``_analysis_inputs``）。想让模板表达条件，就得为它发明一门小语言，而那段逻辑
本来就是 Python 里更好读、更好测的地方。

取不到的东西直接报错，不会替换成空串：静悄悄少喂一段简历全文，模型照样能编出
一份看着挺完整的报告，而那份报告是废的，代价是一整趟调用的时间和 token。点号
路径只有 JSON 步骤的产物走得通——纯文本步骤进上下文的是整段输出。

``_group_order`` 顺带放在这里，因为定义存取、执行器和重跑三边都要同一套「相邻
同名算一个并行组」的口径。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Sequence, Tuple

from app.models.entities import WorkflowStep

# ---- 模板 ------------------------------------------------------------------

# ``{{name}}`` 与 ``{{name.字段}}`` 的形状。字符集与定义存处的标识符校验对齐
# （见 :mod:`app.services.workflow_defs`），保存时能写的变量名这里才取得到。
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

# 取不到值的哨兵：``None`` 是一种合法的值（JSON 里的 null），不能拿来当缺失。
_MISSING = object()


def render_prompt(tpl: str, context: Dict[str, object]) -> str:
    """把 ``{{name}}`` 与 ``{{name.字段}}`` 替换成上下文里的值。

    引用了取不到的东西直接报错，而不是替换成空串：静悄悄少喂一段简历全文，
    模型照样能编出一份看着挺完整的报告，而那份报告是废的，代价是一整趟调用的
    时间和 token。
    """
    problems: List[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        value = _lookup(context, name)
        if value is _MISSING:
            problems.append(_missing_reason(context, name))
            return match.group(0)
        return _stringify(value)

    rendered = _PLACEHOLDER.sub(replace, tpl)
    if problems:
        raise ValueError(f"模板取值失败：{'、'.join(sorted(set(problems)))}")
    return rendered


def _lookup(context: Dict[str, object], name: str) -> object:
    """按 ``a`` 或 ``a.b.c`` 从上下文取值；取不到返回 :data:`_MISSING`。

    ``{{a}}`` 拿到的永远是这一步的整段产物；点号路径只有 JSON 步骤的产物走得通
    ——执行时进上下文的是它按契约解析出来的对象（见 ``execute_run``），整段文本
    里混着的解释性文字不该被下游当成结构体引用。
    """
    root, _, path = name.partition(".")
    if root not in context:
        return _MISSING
    value = context[root]
    if not path:
        return value
    for segment in path.split("."):
        if isinstance(value, dict):
            if segment not in value:
                return _MISSING
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            # 字符串、数字、越界的下标、``a..b`` 里的空段——都没有再往里走一层的道理。
            return _MISSING
    return value


def _missing_reason(context: Dict[str, object], name: str) -> str:
    """报错时说得出是哪一种取不到：根变量根本没有，还是那条路径走不通。"""
    root, _, path = name.partition(".")
    if root not in context:
        return f"未定义的变量 {root}"
    return f"{name}（{root} 的产物里没有这个字段）"


def _stringify(value: object) -> str:
    """把取到的值变成喂进 prompt 的文本。

    对象与数组按 JSON 序列化，不是 Python 的 repr：单引号和 ``None`` 模型会照抄
    进它的输出里，下游再按 JSON 解析就崩。
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _group_order(steps: Sequence[WorkflowStep]) -> List[Tuple[str, List[WorkflowStep]]]:
    """把步骤切成并行组：**相邻**且分组名相同的算一组，组序即首次出现的顺序。

    保存时已经拦过「同名组被别的组隔开」（见
    :meth:`app.services.workflow_defs.WorkflowService._normalize`），所以这里按
    相邻切与按 group_key 归并结果相同；用相邻这一套是因为界面也是这个口径，
    画出来几列就是跑起来几段，两边不必各自解释一次。
    """
    groups: List[Tuple[str, List[WorkflowStep]]] = []
    for step in steps:
        if groups and groups[-1][0] == step.group_key:
            groups[-1][1].append(step)
        else:
            groups.append((step.group_key, [step]))
    return groups

