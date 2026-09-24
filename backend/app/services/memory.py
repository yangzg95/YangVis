"""长期记忆与会话摘要。

两块功能共用这个模块，因为它们的触发时机相同（一轮问答落库之后）、调用
形态相同（后台任务里的一次性非流式模型调用）：

- 会话摘要：回放窗口（chat.HISTORY_TURNS）之外的历史对模型不可见，摘要
  是它们留在上下文里的唯一形式。摘要*全量重算*而不做增量合并：不存
  「摘要到第几条」的 checkpoint，历史被手动改写后下一次重算自然吸收，
  任何 worker 也都能随时重算（见 ChatSummary 的注释）。
- 长期记忆：从最近一轮问答里提取关于用户的持久事实，存入 user_memory，
  回答时注入到开了 ``use_memory`` 的智能体的 system prompt 里。

两者都刻意失败静默：它们是对话的增强而不是主链路，模型调用失败只记
日志，绝不影响下一次回答。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List, Optional

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.errors import BusinessError
from app.models.entities import (
    Agent,
    ChatConversation,
    ChatMessage,
    ChatSummary,
    ModelConfig,
    UserMemory,
)
from app.services.agents import AgentService
from app.services.providers import build_chat_model

logger = logging.getLogger("yangvis.memory")

# ---- 预算（与全系统一致，用字符数而不是 token）--------------------------------

# 注入 system prompt 的记忆条数上限。超出时取最近更新的——最新的一般最相关。
MEMORY_PROMPT_LIMIT = 30

# 一个用户的记忆总量上限。满了之后提取到的 add 被丢弃并记日志：无上限增长
# 的清单最终会把 system prompt 淹没，而人可以在设置页删掉不再重要的。
MEMORY_MAX_ITEMS = 50

# 摘要正文的硬上限。prompt 里要求的是 800 字，这是防模型不听话的兜底。
SUMMARY_MAX_CHARS = 1500

# 喂给模型的「早前历史」渲染文本上限。超出时退化为「旧摘要 + 尾部一段」的
# 合并模式——这是唯一保留 checkpoint（旧摘要本身）的路径，不得已而为之。
SUMMARY_INPUT_LIMIT = 12000

# 记忆提取时单条消息的长度上限。报告讨论的首条消息可能长达 20000 字符，
# 原样喂给提取模型既烧钱又稀释信号。
EXTRACT_MESSAGE_LIMIT = 2000

# 摘要 / 提取这类后台调用的一次性长输出，参照 resume 的 MODEL_TASK_TIMEOUT。
MEMORY_TASK_TIMEOUT = 120.0

# 注入智能体 system prompt 时的行为规则。与 chat._CITATION_RULES 同理放在
# 智能体定义外面，好让用户自建的智能体也有一致的记忆使用方式。
MEMORY_RULES = """
以下是关于这位用户的长期记忆，来自以往对话的积累。请遵守以下要求：
1. 只在与当前问题相关时使用这些记忆；无关时不要提及。
2. 记忆可能过时：用户当下说的与记忆冲突时，以用户当下说的为准。
3. 不要主动向用户复述这份清单，除非他问你「你记得我什么」。
""".strip()

_SUMMARY_SYSTEM_FULL = (
    "你负责把一段对话历史压缩成摘要，供后续对话作为背景上下文。\n"
    "要求：\n"
    "1. 保留事实密度：用户做出的决定、给出的具体信息（数字、名称、配置）、"
    "得出的结论、未完成的待办。\n"
    "2. 砍掉寒暄、客套与追问的过程，只留结果。\n"
    "3. 去掉 [1]、[2] 这类引用编号——编号对应的资料在后续对话里不存在。\n"
    "4. 用简体中文，分要点列出，不超过 800 字。\n"
    "5. 只输出摘要本身，不要任何前缀或解释。"
)

_SUMMARY_SYSTEM_MERGE = (
    "你负责维护一段对话历史的滚动摘要。给你一份已有摘要和这段对话的更新部分，"
    "合并成一份新的完整摘要。\n"
    "要求：\n"
    "1. 保留事实密度：用户做出的决定、给出的具体信息（数字、名称、配置）、"
    "得出的结论、未完成的待办。\n"
    "2. 已被后续对话推翻或修正的内容，以新的为准。\n"
    "3. 去掉 [1]、[2] 这类引用编号——编号对应的资料在后续对话里不存在。\n"
    "4. 用简体中文，分要点列出，不超过 800 字。\n"
    "5. 只输出摘要本身，不要任何前缀或解释。"
)

_EXTRACT_SYSTEM = (
    "你负责维护一份关于用户的长期记忆清单，供以后的对话使用。"
    "从给定的最近一轮对话中，判断是否有值得长期记住的事实，并对清单做增删改。\n"
    "只输出 JSON 对象，不要输出任何其他文字。格式：\n"
    '{"operations": [{"action": "add", "content": "..."}, '
    '{"action": "update", "id": 123, "content": "..."}, '
    '{"action": "delete", "id": 123}]}\n'
    "规则：\n"
    "1. 只记录关于用户本人的持久事实：职业背景、技能栈、偏好、长期目标、"
    "正在进行的项目、明确表达的约束。\n"
    "2. 不记录一次性问题、任务内容、闲聊，以及助手回答里的信息。\n"
    "3. 每条记忆是一句自包含的话，脱离上下文也能看懂。\n"
    "4. 新事实与已有记忆重复或只是细化时，用 update 合并，不要 add 出近义条目。\n"
    "5. 用户推翻了过时的事实时，用 update 或 delete。\n"
    '6. 没有值得记录的内容时输出 {"operations": []}——这是大多数情况，不要硬凑。\n'
    "7. 全部使用简体中文。"
)


def _message_text(text: object) -> str:
    """从模型返回的 content 里取出纯文本（部分 provider 返回内容块列表）。"""
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in text
        )
    return str(text)


async def _ask_text(config: ModelConfig, system: str, user: str, *, temperature: float) -> str:
    """一次性非流式模型调用，返回纯文本。调用方负责兜底异常。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    model = build_chat_model(config, temperature=temperature, timeout=MEMORY_TASK_TIMEOUT)
    result = await model.ainvoke(
        [SystemMessage(content=system), HumanMessage(content=user)]
    )
    return _message_text(getattr(result, "content", result))


class MemoryService:
    """单个用户的长期记忆与会话摘要。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id

    @property
    def owner_id(self) -> int:
        return self._owner_id

    def _scope(self, stmt: Select, column) -> Select:
        """把查询限定在当前 owner 上。所有读操作都要经过这里。"""
        return stmt.where(column == self._owner_id)

    # -- 长期记忆 CRUD ---------------------------------------------------------

    def list(self, *, limit: Optional[int] = None) -> List[UserMemory]:
        stmt = self._scope(select(UserMemory), UserMemory.owner_id).order_by(
            UserMemory.updated_at.desc(), UserMemory.id.desc()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._db.scalars(stmt).all())

    def get(self, memory_id: int) -> UserMemory:
        row = self._db.scalar(
            self._scope(select(UserMemory), UserMemory.owner_id).where(
                UserMemory.id == memory_id
            )
        )
        if row is None:
            raise LookupError("记忆不存在")
        return row

    def create(self, content: str, *, source_conversation_id: Optional[int] = None) -> UserMemory:
        row = UserMemory(
            owner_id=self._owner_id,
            content=content.strip()[:512],
            source_conversation_id=source_conversation_id,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def update(self, memory_id: int, content: str) -> UserMemory:
        row = self.get(memory_id)
        row.content = content.strip()[:512]
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete(self, memory_id: int) -> None:
        row = self.get(memory_id)
        self._db.delete(row)
        self._db.commit()

    def clear(self) -> int:
        """清空当前用户的全部记忆。返回删除条数。"""
        count = len(self.list())
        self._db.execute(
            delete(UserMemory).where(UserMemory.owner_id == self._owner_id)
        )
        self._db.commit()
        return count

    # -- 注入上下文 -------------------------------------------------------------

    def prompt_section(self) -> str:
        """注入 system prompt 的记忆清单；没有记忆时返回空串。"""
        rows = self.list(limit=MEMORY_PROMPT_LIMIT)
        if not rows:
            return ""
        return "\n".join(f"- {row.content}" for row in rows)

    def summary_for(self, conversation_id: int) -> str:
        """一个会话当前的摘要；没有时返回空串。"""
        row = self._db.scalar(
            self._scope(select(ChatSummary), ChatSummary.owner_id).where(
                ChatSummary.conversation_id == conversation_id
            )
        )
        return row.summary if row is not None else ""

    def delete_summary(self, conversation_id: int) -> None:
        self._db.execute(
            delete(ChatSummary).where(
                ChatSummary.owner_id == self._owner_id,
                ChatSummary.conversation_id == conversation_id,
            )
        )
        self._db.commit()

    # -- 摘要重算 -----------------------------------------------------------------

    @staticmethod
    def _render(rows: List[ChatMessage]) -> str:
        lines = []
        for row in rows:
            speaker = "用户" if row.role == "user" else "助手"
            lines.append(f"{speaker}：{row.content}")
        return "\n\n".join(lines)

    async def refresh_summary(
        self, conversation: ChatConversation, config: ModelConfig, *, history_turns: int
    ) -> None:
        """把回放窗口之外的历史（重新）压缩成摘要。

        全量重算：窗口之前的消息全部参与，不依赖上次算到哪。只有当这段历史
        长到喂不下时才退化为「旧摘要 + 尾部一段」的合并模式。
        """
        rows = list(
            self._db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.owner_id == self._owner_id,
                    ChatMessage.conversation_id == conversation.id,
                )
                .order_by(ChatMessage.id)
            ).all()
        )
        window = history_turns * 2
        overflow = rows[:-window] if len(rows) > window else []

        if not overflow:
            # 历史还在窗口内，摘要没有存在的必要（比如曾经长过、后来被删短了）。
            self.delete_summary(conversation.id)
            return

        rendered = self._render(overflow)
        if len(rendered) <= SUMMARY_INPUT_LIMIT:
            system = _SUMMARY_SYSTEM_FULL
            user_prompt = f"请总结以下对话：\n\n{rendered}"
        else:
            prior = self.summary_for(conversation.id)
            system = _SUMMARY_SYSTEM_MERGE
            user_prompt = (
                f"已有摘要：\n{prior or '（无）'}\n\n"
                f"对话的更新部分：\n\n{rendered[-SUMMARY_INPUT_LIMIT:]}"
            )

        text = (await _ask_text(config, system, user_prompt, temperature=0.2)).strip()
        if not text:
            logger.warning("summary for conversation %s came back empty", conversation.id)
            return
        text = text[:SUMMARY_MAX_CHARS]

        row = self._db.get(ChatSummary, conversation.id)
        if row is None:
            row = ChatSummary(
                conversation_id=conversation.id, owner_id=self._owner_id, summary=text
            )
            self._db.add(row)
        else:
            row.summary = text
        self._db.commit()
        logger.info(
            "refreshed summary for conversation %s (%d overflow message(s), %d chars)",
            conversation.id,
            len(overflow),
            len(text),
        )

    # -- 记忆提取 -----------------------------------------------------------------

    async def extract_memories(
        self, conversation: ChatConversation, config: ModelConfig, agent: Optional[Agent]
    ) -> None:
        """从最近一轮问答里提取关于用户的持久事实。

        只在智能体开了 ``use_memory`` 时运行：没开的人设既不消费记忆，也不
        该为它花提取的 token。
        """
        if agent is None or not agent.use_memory:
            return

        rows = list(
            self._db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.owner_id == self._owner_id,
                    ChatMessage.conversation_id == conversation.id,
                )
                .order_by(ChatMessage.id.desc())
                .limit(2)
            ).all()
        )
        if len(rows) < 2:
            return
        rows.reverse()
        exchange = "\n\n".join(
            f"{'用户' if row.role == 'user' else '助手'}："
            f"{row.content[:EXTRACT_MESSAGE_LIMIT]}"
            for row in rows
        )

        existing = self.list()
        listing = (
            "\n".join(f"- id={row.id} {row.content}" for row in existing) or "（空）"
        )
        user_prompt = f"已有记忆清单：\n{listing}\n\n最近一轮对话：\n\n{exchange}"

        text = await _ask_text(config, _EXTRACT_SYSTEM, user_prompt, temperature=0.0)
        operations = self._parse_operations(text, conversation.id)
        if operations is None:
            return

        known = {row.id: row for row in existing}
        # 规范化后的内容集合：去重既针对已有记忆，也针对本批刚加进去的——
        # 模型在同一批里给出两条一样的 add 是常有的事。
        contents = {row.content.casefold() for row in known.values()}
        for op in operations:
            action = op.get("action")
            content = str(op.get("content") or "").strip()
            try:
                op_id = int(op.get("id")) if op.get("id") is not None else None
            except (TypeError, ValueError):
                op_id = None

            if action == "add" and content:
                normalized = content.casefold()
                if normalized in contents:
                    continue
                # 新项已 append 进 known，len(known) 本身就含本批新增。
                if len(known) >= MEMORY_MAX_ITEMS:
                    logger.info(
                        "memory cap (%d) reached for owner %s, dropping extracted item",
                        MEMORY_MAX_ITEMS,
                        self._owner_id,
                    )
                    continue
                row = self.create(content, source_conversation_id=conversation.id)
                known[row.id] = row
                contents.add(normalized)
            elif action == "update" and op_id in known and content:
                self.update(op_id, content)
            elif action == "delete" and op_id in known:
                self.delete(op_id)

    def _parse_operations(self, text: str, conversation_id: int) -> Optional[List[dict]]:
        """从模型输出里解析操作列表；解析失败返回 None（本轮跳过，不算错误）。"""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            logger.warning(
                "memory extraction for conversation %s returned no JSON: %.200s",
                conversation_id,
                text,
            )
            return None
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning(
                "memory extraction for conversation %s returned bad JSON: %.200s",
                conversation_id,
                text,
            )
            return None
        operations = payload.get("operations")
        if not isinstance(operations, list):
            return []
        return [op for op in operations if isinstance(op, dict)]


# ---- 后台任务 -------------------------------------------------------------------

# asyncio.create_task 创建的任务只被事件循环弱引用，不放一份强引用，任务可能
# 在跑完之前就被 GC 回收。
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def fire_and_forget(coro) -> None:
    """在请求结束后继续跑一个协程（SSE 流没法用 FastAPI 的 BackgroundTasks）。

    任务体内的异常必须自己兜住——这里的回调只负责回收引用。
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def after_answer_task(owner_id: int, conversation_id: int, history_turns: int) -> None:
    """一轮问答落库之后的收尾：重算会话摘要、提取长期记忆。

    自己开一个 session：任务真正跑起来的时候，请求级别的 session 早就关了
    （与 knowledge.index_document_task 同一形态）。任何一步失败都只记日志，
    下一步照常——全量重算的设计保证下一次成功调用会把错过的补回来。
    """
    # 延迟导入：chat 模块在模块级依赖本模块（MemoryService）。
    from app.services.chat import ChatService

    with SessionLocal() as db:
        chat_service = ChatService(db, owner_id)
        memory = MemoryService(db, owner_id)

        try:
            conversation = chat_service.get_conversation(conversation_id)
        except LookupError:
            return  # 会话在回答落库后又被删了

        try:
            config = chat_service.require_chat_config(conversation.model_config_id)
        except BusinessError:
            # 对话模型配置被改掉/删掉了：记忆与摘要是增强，不是拦路的理由。
            return

        try:
            await memory.refresh_summary(
                conversation, config, history_turns=history_turns
            )
        except Exception:  # noqa: BLE001 - 绝不让后台任务无声无息地死掉
            logger.exception("summary refresh failed for conversation %s", conversation_id)

        agent = None
        if conversation.agent_id is not None:
            try:
                agent = AgentService(db, owner_id).get(conversation.agent_id)
            except LookupError:
                pass
        try:
            await memory.extract_memories(conversation, config, agent)
        except Exception:  # noqa: BLE001
            logger.exception(
                "memory extraction failed for conversation %s", conversation_id
            )
