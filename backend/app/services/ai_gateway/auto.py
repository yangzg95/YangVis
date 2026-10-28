"""Auto 模型：按「这次请求要什么」+「每条路由怎么定义自己」现场决定打给谁。

分两步走，因为两件事的确定性差别很大：

1. **规则定硬性要求**。请求里写着 embeddings、消息里带了图片、正文里有
   ``tools`` —— 这些是能一眼看出来的「必须」，用规则判，零成本、可复述、可单测。
2. **任务类型定偏好**。「这是道推理题还是句寒暄」规则判不了，就挑候选池里优先级
   最高的那条映射问它一次。这一步整个是可关的（``AI_GATEWAY_AUTO_LLM``），也是
   允许失败的：分类超时、上游报错、答得不像话，统统退化成「按优先级选」，
   绝不把整次调用拖死——Auto 只是想要更聪明的选择，不是想要多一个故障点。

标签写在 ``ai_model_route.auto_tags`` 上，词表见 :data:`app.models.schemas.AI_AUTO_TAGS`：
能力标签（chat/embedding/vision/tools）参与**筛选**，任务类型标签
（reasoning/code/writing/translation/general）参与**排序**。

本模块不发出站请求：分类请求由 ``proxy`` 去发（它才有 hop 头与超时那一套），这里
只负责「问什么」和「怎么读回答」。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.schemas import AiModelCapability, AiModelTaskKind
from app.services.ai_gateway.service import (
    AUTO_MODEL_NAME,
    CHAT_ENDPOINT,
    EMBEDDINGS_ENDPOINT,
    RouteCandidate,
    auto_routes,
    route_limit_reason,
)
from app.timeutil import db_now

logger = logging.getLogger("yangvis.ai_gateway.auto")

settings = get_settings()

CAPABILITY_CHAT = AiModelCapability.CHAT.value
CAPABILITY_EMBEDDING = AiModelCapability.EMBEDDING.value
CAPABILITY_VISION = AiModelCapability.VISION.value
CAPABILITY_TOOLS = AiModelCapability.TOOLS.value
TASK_GENERAL = AiModelTaskKind.GENERAL.value
TASK_TAGS = frozenset(tag.value for tag in AiModelTaskKind)

# 分类器要回答的就一个词，喂全文既烧钱又没必要——任务类型写在开头。
CLASSIFY_SYSTEM = (
    "你在替网关挑选模型。读用户这句话，只输出一个词，不要解释、不要标点："
    "reasoning=推理/数学/复杂分析，code=编程/技术实现/排查报错，"
    "writing=写作/文案/润色，translation=翻译，general=其余简单问答。"
)

# (候选, 待分类文本) -> 任务类型标签或 None。由 proxy 注入，本模块不碰网络。
Classifier = Callable[[RouteCandidate, str], Awaitable[Optional[str]]]


def is_auto_model(name: Optional[str]) -> bool:
    """调用方点的是不是 Auto（大小写与首尾空格都不计较）。"""
    return bool(name) and name.strip().lower() == AUTO_MODEL_NAME


def _route_tags(route: Any) -> List[str]:
    return [str(tag) for tag in (route.auto_tags or []) if tag]


def _messages(body: Mapping[str, Any]) -> List[Any]:
    messages = body.get("messages")
    return messages if isinstance(messages, list) else []


def has_image(body: Mapping[str, Any]) -> bool:
    """消息里带了图片，只有会看图的模型接得了。

    ``content`` 既可能是字符串也可能是分段数组，各厂商对图片段的写法也不完全
    一致（``image_url`` 是 OpenAI 的，``image`` 是简写形态），两种都认。
    """
    for message in _messages(body):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                return True
    return False


def wants_tools(body: Mapping[str, Any]) -> bool:
    """这次要求函数调用：带了工具定义、带着工具的执行结果、或历史里有工具调用。

    只看 ``tools`` 不够——多轮里把工具结果发回去请求下一跳的，请求体里带的
    是 ``role: "tool"``；这两种都得打给会调工具的模型。
    """
    if isinstance(body.get("tools"), list) and body["tools"]:
        return True
    for message in _messages(body):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            return True
        if isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
            return True
    return False


def required_tags(endpoint: str, body: Mapping[str, Any]) -> List[str]:
    """这次请求的硬性要求；候选必须全部满足才进得了排序。"""
    if endpoint == EMBEDDINGS_ENDPOINT:
        return [CAPABILITY_EMBEDDING]
    tags = [CAPABILITY_CHAT]
    if has_image(body):
        tags.append(CAPABILITY_VISION)
    if wants_tools(body):
        tags.append(CAPABILITY_TOOLS)
    return tags


def classify_messages(text: str) -> List[Dict[str, Any]]:
    """分类请求的 ``messages``：让候选模型自己回一个词。"""
    return [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": text[: settings.AI_GATEWAY_AUTO_CLASSIFY_CHARS]},
    ]


def task_prompt_text(body: Mapping[str, Any]) -> Optional[str]:
    """取最后一条用户消息作为待分类文本。

    取最后一条而不是拼全文：Auto 要判断的是「接下来这活是什么类型」，而多轮对话
    里前面的内容早就被上一轮分类过一回了。
    """
    for message in reversed(_messages(body)):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                return text.strip()
    return None


def parse_task_kind(answer: Optional[str]) -> Optional[str]:
    """从模型的回答里认出一个任务类型标签；认不出就当没分类。

    刻意不做「最接近的那个」这类模糊匹配：判错的任务类型会把请求分到更不相干的
    模型上，而退回按优先级选是零风险的。
    """
    if not answer:
        return None
    lowered = answer.lower()
    for tag in (
        AiModelTaskKind.REASONING.value,
        AiModelTaskKind.CODE.value,
        AiModelTaskKind.TRANSLATION.value,
        AiModelTaskKind.WRITING.value,
        TASK_GENERAL,
    ):
        if tag in lowered:
            return tag
    return None


def _prefers_task(task: Optional[str], tags: List[str]) -> bool:
    return bool(task) and task in tags


def _task_tags_in_pool(candidates: List[RouteCandidate]) -> bool:
    """池子里有没有人声明了任务类型。一个都没有时问模型也排不出先后，别白花一次。"""
    return any(TASK_TAGS.intersection(_route_tags(item.route)) for item in candidates)


def _tag_feasible_rows(db: Session, required: List[str]) -> List[Tuple[Any, Any]]:
    """候选池里标签满足全部硬性要求的（路由, 通道），含被有效期/额度挡住的。"""
    wanted = set(required)
    return [
        (route, channel)
        for route, channel in auto_routes(db, include_limited=True)
        if wanted.issubset(_route_tags(route))
    ]


def _feasible(db: Session, required: List[str]) -> List[RouteCandidate]:
    """候选池里满足全部硬性要求且当前未受限的那些，已按 priority 升序。

    硬性要求一律严格匹配：没有哪条路由可以靠「大概也行」进池子，否则图打给纯文本
    模型这种事会变成偶发的、日志里看不出原因的回答质量问题。
    """
    moment = db_now()
    return [
        RouteCandidate(
            route=route,
            channel=channel,
            upstream_model=(route.upstream_model or route.model_name),
        )
        for route, channel in _tag_feasible_rows(db, required)
        if route_limit_reason(route, moment) is None
    ]


def _blocked_feasible(db: Session, required: List[str]) -> int:
    """标签够格、但被有效期/额度挡掉的条数：候选全灭时给报错留个说法。"""
    moment = db_now()
    return sum(
        1
        for route, _ in _tag_feasible_rows(db, required)
        if route_limit_reason(route, moment) is not None
    )


async def resolve_auto(
    db: Session,
    endpoint: str,
    body: Mapping[str, Any],
    classifier: Optional[Classifier] = None,
) -> Tuple[List[RouteCandidate], Dict[str, Any]]:
    """返回（Auto 的候选列表, 决策快照）。候选为空由调用方决定怎么报错。

    顺序即故障转移顺序：先按任务类型匹配分组（匹配的在前），组内保持 priority
    升序 —— 用稳定排序，所以「更合适」永远压过「更靠前」，而同组里还是原来的
    优先级说话。
    """
    required = required_tags(endpoint, body)
    candidates = _feasible(db, required)
    blocked = _blocked_feasible(db, required)
    decision: Dict[str, Any] = {
        "required": required,
        "task": None,
        "source": "priority",
        "matched": len(candidates),
        "blocked": blocked,
    }
    if not candidates:
        return [], decision

    if (
        endpoint == CHAT_ENDPOINT
        and classifier is not None
        and settings.AI_GATEWAY_AUTO_LLM
        and _task_tags_in_pool(candidates)
    ):
        text = task_prompt_text(body)
        if text:
            # 探针用池子里优先级最高的一条：任务类型还没定，排序无从谈起，
            # 而它反正要能打 chat。
            try:
                task = await classifier(candidates[0], text)
            except Exception as exc:  # noqa: BLE001 - 分类失败只该退回按优先级选
                logger.warning("auto task classification failed: %s", exc)
                decision["source"] = "llm_failed"
                task = None
            else:
                decision["source"] = "llm" if task else "llm_unclear"
                decision["task"] = task
                if task:
                    candidates = sorted(
                        candidates,
                        key=lambda item: not _prefers_task(task, _route_tags(item.route)),
                    )

    return candidates, decision
