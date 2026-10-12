"""智能体的单次调用：一份人设 + 一份模型配置 → 一段文本。

简历分析、幻灯片生成、面试参考答案这些后台调用，现在都是工作流的一个步骤，
统一走这里。之前它们各自抄了一份几乎相同的 ``_ask``（``services/resume.py``、
``services/interview.py``、``services/slides/service.py``），于是「provider 返回内容块
列表要怎么拉平」「该用多长超时」这类问题要在三处分别修。

这里只负责**一次调用**。多步骤的编排、进度与落库见
``services/workflows/exec.py``，那一层的模块清单见 :mod:`app.services.workflows`。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import Agent, ModelConfig
from app.models.schemas import ModelPurpose
from app.services.agents import AgentService
from app.services.model_config import default_model_config
from app.services.providers import build_chat_model

logger = logging.getLogger("yangvis.agent_run")

# 非流式调用要等整份输出（动辄上千 token）生成完才返回，全局
# MODEL_HTTP_TIMEOUT（30s，适合对话与连通性测试）几乎必然超时——慢模型、
# 长 JD 时尤甚——所以这类后台调用给 5 分钟。
MODEL_TASK_TIMEOUT = 300.0

# MCP 工具调用循环的轮次上限：模型 → 工具 → 模型 算一轮。不设限的话，
# 模型与工具互相扯皮会一直烧 token，且把上下文撑爆。
MAX_TOOL_ROUNDS = 8
# 单个工具结果进上下文的字符上限：检索类工具一次吐几万字是常态。
TOOL_OUTPUT_LIMIT = 8000

# 工作流步骤的默认输出形态。
OUTPUT_MARKDOWN = "markdown"
OUTPUT_JSON = "json"


def labeled_section(label: str, text: str) -> str:
    """把一段材料包成 ``标签：\\n三引号\\n内容\\n三引号``。

    简历全文、JD、幻灯片材料都靠它和模型的指令隔开——材料里出现「请忽略上面」
    这类字样时，有没有这层边界决定了模型是照做还是当内容读。
    """
    return f'{label}：\n"""\n{text.strip()}\n"""'


def message_text(content: object) -> str:
    """从模型返回的 ``content`` 里取出纯文本。

    部分 provider 以结构化内容块列表返回，直接当字符串用会得到 ``[{'text': ...}]``
    这样的字面量，报告里就全是引号和字典键。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def require_chat_model(
    db: Session, owner_id: int, config_id: Optional[int] = None
) -> ModelConfig:
    """确定这次调用用哪份对话模型配置。

    传了 ``config_id``（工作流步骤按步骤指定模型）就用那份，必须是本人的、对话
    用途、且已通过连通性测试；否则退回「系统设置 → 模型管理」里的默认对话模型。

    未就绪时抛 :class:`BusinessError`（``CODE_CHAT_NOT_READY``）而不是普通错误：
    这个 code 驱动的是一套「去把模型配上」的引导流程，前端按 code 分支。
    """
    if config_id is not None:
        config = db.scalar(
            select(ModelConfig).where(
                ModelConfig.owner_id == owner_id, ModelConfig.id == config_id
            )
        )
        if config is None:
            # 404 而不是 403：确认别人的配置存在本身就是泄露。
            raise LookupError("模型配置不存在")
        if config.purpose != ModelPurpose.CHAT.value:
            raise BusinessError(CODE_CHAT_NOT_READY, "所选的配置不是对话模型。")
        if not config.last_test_ok:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                f"对话模型「{config.title}」尚未通过连通性测试，"
                "请前往「系统设置 → 模型管理」测试后再使用。",
            )
        return config

    config = default_model_config(db, owner_id, ModelPurpose.CHAT)
    if config is None or not config.last_test_ok:
        raise BusinessError(
            CODE_CHAT_NOT_READY,
            "尚未配置可用的对话模型。"
            "请前往「系统设置 → 模型管理」添加对话模型并通过连通性测试。",
        )
    return config


async def run_agent(
    db: Session,
    owner_id: int,
    agent_slug: str,
    user_prompt: str,
    *,
    agent: Optional[Agent] = None,
    config_id: Optional[int] = None,
    timeout: float = MODEL_TASK_TIMEOUT,
    tools: Optional[Sequence] = None,
    max_tool_rounds: int = MAX_TOOL_ROUNDS,
) -> str:
    """按人设发起一轮非流式调用，返回模型输出的原始文本。

    ``agent_slug`` 决定语气与输出格式要求（都在 ``agent.system_prompt`` 上），
    温度也跟着智能体走；变量内容（简历全文这类）由调用方拼进 ``user_prompt``。

    ``agent`` 让调用方复用已经取到的人设行——工作流一趟要跑进步骤，每步再查
    一次 agent 表纯属浪费。异常一律外抛，由调用方决定是落状态还是回错误事件。

    传了 ``tools``（工作流挂载的 MCP 工具）时走有界工具调用循环：模型要求
    调工具 → 执行 → 把结果作为 ``ToolMessage`` 喂回，直到模型不再调工具或
    达到 ``max_tool_rounds``。不传时就是原来的一次 ``ainvoke``，行为零变化。
    """
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    agent = agent or AgentService(db, owner_id).require_by_slug(agent_slug)
    config = require_chat_model(db, owner_id, config_id)
    logger.info(
        "calling chat model: agent=%s model=%s base_url=%s prompt_chars=%s owner=%s tools=%s",
        agent.slug,
        config.model_name,
        config.base_url,
        len(user_prompt),
        owner_id,
        len(tools) if tools else 0,
    )
    model = build_chat_model(
        config, temperature=agent.temperature / 100, timeout=timeout
    )
    messages = [
        SystemMessage(content=agent.system_prompt),
        HumanMessage(content=user_prompt),
    ]

    if not tools:
        result = await model.ainvoke(messages)
        return message_text(getattr(result, "content", result))

    by_name = {tool.name: tool for tool in tools}
    bound = model.bind_tools(list(tools))

    for _round in range(max_tool_rounds):
        response = await bound.ainvoke(messages)
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            return message_text(getattr(response, "content", response))

        # AIMessage 要先入列，再补它点名的 ToolMessage——顺序是工具调用
        # 协议的一部分，乱了 provider 会直接拒下一轮请求。
        messages.append(response)
        for call in tool_calls:
            name = call.get("name", "")
            tool = by_name.get(name)
            if tool is None:
                output = f"工具 {name} 不存在，请改用其他可用工具"
            else:
                try:
                    output = await tool.ainvoke(call.get("args") or {})
                except Exception as exc:  # noqa: BLE001 - 错误回灌让模型自纠，不炸步骤
                    output = f"工具调用失败：{str(exc)[:300]}"
            output = message_text(output)
            if len(output) > TOOL_OUTPUT_LIMIT:
                output = output[:TOOL_OUTPUT_LIMIT] + "\n…（工具输出过长，已截断）"
            messages.append(
                ToolMessage(content=output, tool_call_id=call.get("id", ""))
            )

    # 轮次到顶：换回未绑工具的模型强制收敛，避免它继续空转调工具。
    messages.append(
        HumanMessage(
            content="工具调用次数已达上限。请基于已有信息直接给出最终答案，"
            "不要再调用任何工具。"
        )
    )
    final = await model.ainvoke(messages)
    return message_text(getattr(final, "content", final))


def model_error_text(exc: Exception) -> str:
    """把模型调用异常转成用户能看懂的失败原因。

    超时单独翻译：它占后台生成的绝大多数失败，而「换个更快的模型」这个出路
    埋在一条 ``ReadTimeout`` 原始字符串里，用户是看不到的。
    """
    text = str(exc)
    if "timed out" in text.lower() or "timeout" in text.lower():
        return (
            "模型响应超时：本次生成内容较长，可重新发起试试；"
            "若反复出现，建议到「系统设置 → 模型管理」换用响应更快的模型。"
        )
    return text[:512]


_JSON_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def strip_json_fence(raw_text: str) -> str:
    """去掉裹在输出外面的 ```json 代码栅栏，顺带收掉首尾空白。

    栅栏要整对出现在首尾才算。模型在栅栏前后还写了寒暄时这里不动它——
    下面 :func:`json_span` 照样能从散文里截出括号跨度。
    """
    text = raw_text.strip()
    fence = _JSON_FENCE.match(text)
    return fence.group(1) if fence else text


def json_span(text: str, open_brace: str = "{", close_brace: str = "}") -> Optional[str]:
    """第一个 ``open_brace`` 到最后一个 ``close_brace`` 的跨度，截不出来是 None。

    取最外层那对：JSON 前后带散文、数组里套对象都不影响，半截输出会因为
    等不到闭括号而返回 None，而不是交出截断的片段。
    """
    start, end = text.find(open_brace), text.rfind(close_brace)
    return text[start : end + 1] if 0 <= start < end else None


def extract_json_object(raw_text: str) -> dict:
    """从模型输出里剥出一个 JSON 对象。

    模型并不总是守规矩：外面裹一层 ```json 代码栅栏、前后带寒暄都是常态。
    所以先剥栅栏，再取第一个 ``{`` 到最后一个 ``}``。解析不出来时抛
    :class:`ValueError`——调用方要么换个解析策略（简历分析退回整段当报告），
    要么让这一步失败（幻灯片没有合法结构就是没产物）。
    """
    span = json_span(strip_json_fence(raw_text))
    if span is None:
        raise ValueError("模型输出里没有 JSON 对象")
    try:
        data = json.loads(span)
    except json.JSONDecodeError as exc:
        raise ValueError(f"模型输出的 JSON 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("模型输出的顶层不是 JSON 对象")
    return data


def parse_output(
    raw_text: str, kind: str, expected_keys: Optional[list] = None
) -> Tuple[str, Optional[dict]]:
    """按步骤声明的输出契约解析一次调用。

    返回 ``(纯文本, 解析出的对象或 None)``：markdown 步骤没有对象可给。
    JSON 步骤还会核对 ``expected_keys`` 点名的字段在不在——少一个字段就当场
    报错，比让缺字段的结果流到下游、三步之后才以莫名其妙的形式炸出来便宜。
    """
    if kind != OUTPUT_JSON:
        return raw_text.strip(), None

    data = extract_json_object(raw_text)
    missing = [key for key in (expected_keys or []) if key not in data]
    if missing:
        raise ValueError(f"模型输出缺少字段：{'、'.join(missing)}")
    return raw_text.strip(), data
