"""对话：ReAct 循环、检索工具与流式回答。

回答走 ReAct：检索被包成一个模型可以调用的工具，由模型自己决定要不要查、
查什么、查几次，结果回灌进对话再继续推理，直到它给出不带工具调用的答复。

代价要说清楚。旧版靠结构防幻觉——检索结果为空时干脆不调模型，它就没得选。
现在这条硬保证没了：模型完全可以一次都不查就开口，约束只剩 prompt 里的
引用规则，那是一句它可以不听的建议。换来的是追问、拆解、多角度检索的能力。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, List, Optional, Sequence, Tuple

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app import database as app_db
from app.background import fire_and_forget
from app.config import get_settings
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import (
    Agent,
    ChatAttachment,
    ChatConversation,
    ChatMessage,
    McpServer,
    ModelConfig,
    OpsPendingAction,
)
from app.models.schemas import (
    ChatAttachmentItem,
    Citation,
    CompactionStateItem,
    ConversationItem,
    MessageItem,
    ModelPurpose,
    OpsActionItem,
)
from app.services.ops import actions as ops_actions
from app.services.agents import AgentService
from app.services.chat_ops import TARGET_SERVER, Auditor, ChatOpsToolbox, OpsTarget
from app.services.knowledge import KnowledgeService
from app.services.mcp.tools import McpToolBundle, open_bundle, resolve_servers
from app.services.model_config import default_model_config
from app.services.tools import (
    ToolContext,
    build_local_tools,
    cap_tool_output,
    ensure_unique_tool_names,
    validate_keys as validate_local_tool_keys,
)
from app.services.chat.attachments import (
    delete_stored,
    image_url,
    read_image_data_url,
)
from app.services.chat.memory import (
    MEMORY_RULES,
    MemoryService,
    split_replay_window,
)
from app.services.ops import database as db_ops
from app.services.ops import server as server_ops
from app.services.providers import build_chat_model, resolve_temperature
from app.timeutil import db_now

logger = logging.getLogger("yangvis.chat")

settings = get_settings()

# 要回放多少轮历史。够撑住追问（「那第二个呢？」），又不至于让一段旧对话
# 把检索到的上下文挤出去。
HISTORY_TURNS = 6

# 附件回放的预算。文档正文按字符数分配、优先最近一轮；图片按张数限制，
# 超出的旧图片只留一行说明，不会把整轮请求撑爆。
REPLAY_ATTACHMENT_TEXT_BUDGET = 60_000
REPLAY_IMAGE_LIMIT = 8

# 每次检索回来的 chunk 数量。
RETRIEVAL_TOP_K = 8

# ReAct 循环的步数上限。模型自己决定检索几轮，得有个上限兜着，免得它原地
# 打转。8 步约等于最多 3 轮「检索 → 再想」。
MAX_REACT_STEPS = 8

# 挂了运维工具箱时的步数上限。运维诊断动辄「列目标 → 逐条命令采集 → 汇总」
# 六七轮起步（每轮模型+工具各占一步），沿用检索的 8 步上限会在排查中途被
# GraphRecursionError 掐断——用户看到的就是 AI 说到一半突然停了。
OPS_REACT_STEPS = 25

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

NO_CONTEXT_REPLY = "知识库中未找到相关内容，无法回答这个问题。"

# 用户中途停止生成时追加的标记。不加这个标记的话，保存下来的文本看起来
# 就像是模型自己说到一半没声了。
ABORTED_SUFFIX = "\n\n（已停止生成）"
ABORTED_EMPTY = "（已停止生成）"

# ReAct 循环撞步数上限时追加的标记。运维排查里模型常常刚说完「现在实际
# 执行」就被掐断；不补这句话，用户看到的就像 AI 说到一半没声了，也不
# 知道发个「继续」就能接着排查。
STEP_LIMIT_SUFFIX = "\n\n（本轮排查步数已达上限，暂停在此；回复「继续」可接着排查）"

# 单轮回答的总时长上限。运维通道单条命令超时就 300s、ReAct 上限 25 步，最坏
# 情况一条回答能占住 SSE 流 + 请求级 session + SSH 连接一个多小时；加一道整体
# 墙钟上限兜底，到点就按「步数上限」同款口径收尾（存半截 + 补标记）。给 900 秒
# 是为了让它仍明显大于单条慢命令（三条的量），否则一条 du 跑完这轮就没预算了。
ANSWER_WALL_CLOCK_LIMIT = 900.0
TIME_LIMIT_SUFFIX = "\n\n（本轮排查时间已达上限，暂停在此；回复「继续」可接着排查）"

# 附加到每个使用了知识库的 agent prompt 尾部。放在智能体定义外面，是为了
# 即使用户自己写了个智能体，引用标注机制也能保持一致。
_CITATION_RULES = """
你可以调用 search_knowledge_base 工具检索知识库。请遵守以下要求：
1. 回答事实性问题前先检索；问题涉及多个方面时，分多次检索，每次只查一个方面。
2. 只使用检索结果中的内容作答，不要引入检索结果之外的信息。
3. 在引用了资料的句子末尾标注来源编号，如 [1]、[2]；可同时引用多条 [1][3]。
   编号直接沿用检索结果里给出的那个，不要自己重新编号。
4. 如果检索不到足以回答的资料，直接说明「知识库中未找到相关内容」，不要推测。
5. 不要复述编号规则，也不要在结尾附加来源列表——界面会自动展示。
""".strip()

# 附加到每个挂了运维工具的 agent prompt 尾部。与引用规则同理放在智能体定义
# 外面，好让「写命令必须经用户确认」这条硬约束不依赖用户自己写的 prompt。
_OPS_RULES = """
你可以通过工具排查用户自己的服务器与数据库。请遵守以下要求：
1. 目标不明确时，先用 list_servers / list_databases 列出候选让用户挑选，不要猜测 id。
2. 只读排查直接执行；会改变系统状态的命令一律用 propose_command 提交给用户确认，
   不要把写命令伪装成只读命令执行，也不要试图绕过分类。
3. propose_command 提交成功后立即结束本轮回答，告知用户到界面上确认——他确认后
   系统会自动执行，并把结果交给你继续分析。用户拒绝时不要换个写法重试。
4. 数据库查询只读是硬约束，没有「确认后可写」的通道：需要写入时把 SQL 写给用户，
   让他到「运维」页面执行。
5. 先采集事实再下结论，回答中引用你实际看到的输出。
6. 工具还没真正返回结果之前，不要输出任何结论、指标或「命令输出」，绝不凭经验
   编造执行结果。中间轮次至多写一句你正在查什么；结论只出现在收集完事实的
   最终回答里。
""".strip()


def _format_hits(numbered: Sequence[Tuple[int, object]]) -> str:
    """把检索到的 chunk 渲染成带编号的段落，好让模型能引用。"""
    blocks = []
    for index, hit in numbered:
        blocks.append(f"[{index}] 来源：{hit.filename}\n{hit.content}")
    return "\n\n".join(blocks)


def _chunk_text(chunk: object) -> str:
    """从一个流式 chunk 里取出纯文本。"""
    text = getattr(chunk, "content", "") or ""
    if isinstance(text, str):
        return text
    # 有些 provider 是以结构化内容块的形式流式返回的。
    return "".join(part.get("text", "") for part in text if isinstance(part, dict))


def _chunk_reasoning(chunk: object) -> str:
    """取出一个流式 chunk 里的推理/思考增量（OpenAI 兼容的 reasoning_content）。

    只有会输出思维链的模型才带这个字段，普通模型恒为空。``ChatOpenAI`` 本身会丢掉
    这个非标准字段，是 ``providers.ReasoningChatOpenAI`` 把它抬进
    ``additional_kwargs`` 的，所以从这里读。
    """
    extra = getattr(chunk, "additional_kwargs", None)
    if isinstance(extra, dict):
        reasoning = extra.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
    return ""


# 时间线里工具结果只做「回看过程」用，不回灌模型（那是 tools.cap_tool_output
# 的活，上限 8000）；2000 字够看清结果头尾，SSE 帧与 timeline JSON 也不会被撑大。
TIMELINE_OUTPUT_LIMIT = 2000
_TIMELINE_TRUNCATE_NOTICE = "…（工具输出过长，已截断）"


def _tool_result_text(result: object) -> str:
    """把 on_tool_end 的结果统一成展示用字符串。

    LangChain 给的是 ``ToolMessage``（内容在 ``.content``）；MCP 适配器可能给
    内容块列表（文本块 + 资源块）。非字符串结果 JSON 序列化。
    """
    content = getattr(result, "content", result)
    if isinstance(content, list):
        pieces: List[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    pieces.append(str(block.get("text", "")))
                else:
                    pieces.append(json.dumps(block, ensure_ascii=False, default=str))
            else:
                pieces.append(str(block))
        content = "\n".join(piece for piece in pieces if piece)
    if isinstance(content, str):
        text = content
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, ensure_ascii=False, default=str)
    if len(text) > TIMELINE_OUTPUT_LIMIT:
        text = text[:TIMELINE_OUTPUT_LIMIT] + _TIMELINE_TRUNCATE_NOTICE
    return text


def _attach_step_result(
    timeline: List[dict], event: dict, result: object, started_perf: dict
) -> Optional[dict]:
    """把一次工具返回挂到对应的 timeline 步骤上，返回要推给前端的事件载荷。

    优先按 ``run_id`` 配对（并行工具调用也不会串）；事件里没有 run_id（测试
    打桩、兼容旧行为）时退回「最后一个还没结果的步骤」。找不到归属就不发。
    ``started_perf`` 按 run_id 记工具开跑时刻，顺带把耗时挂回步骤。
    """
    run_id = event.get("run_id")
    target: Optional[dict] = None
    if run_id:
        for entry in reversed(timeline):
            if entry.get("type") == "step" and entry.get("run_id") == run_id:
                target = entry
                break
    if target is None:
        for entry in reversed(timeline):
            if entry.get("type") == "step" and "output" not in entry:
                target = entry
                break
    if target is None:
        return None
    output = _tool_result_text(result)
    # ToolMessage.status：工具抛异常被 LangChain 接住时为 "error"。
    is_error = getattr(result, "status", None) == "error"
    started_at = started_perf.pop(target.get("run_id"), None)
    elapsed_ms = int((time.monotonic() - started_at) * 1000) if started_at else None
    target["output"] = output
    target["is_error"] = is_error
    if elapsed_ms is not None:
        target["elapsed_ms"] = elapsed_ms
    return {
        "run_id": target.get("run_id") or "",
        "output": output,
        "is_error": is_error,
        **({"elapsed_ms": elapsed_ms} if elapsed_ms is not None else {}),
    }


@dataclass
class _AttachmentPlan:
    """一条附件在本轮回放里的形态：展开正文 / 附带图片 / 只留说明。"""

    attachment: ChatAttachment
    text: Optional[str] = None
    truncated: bool = False
    include_image: bool = False
    note: Optional[str] = None


class _CitationRegistry:
    """跨多轮检索给 chunk 分配稳定编号。

    模型可能检索好几次，同一个 chunk 会被重复命中。编号必须全局唯一且此后
    不变——它一旦在回答里写下 [2]，后面再出现的 [2] 就得还是同一份资料。
    """

    def __init__(self) -> None:
        self._by_chunk: dict = {}
        self._order: List[int] = []

    def register(self, hits: Sequence) -> List[Tuple[int, object]]:
        """登记一批命中，返回 ``(编号, hit)``；见过的沿用原编号。"""
        numbered: List[Tuple[int, object]] = []
        for hit in hits:
            known = self._by_chunk.get(hit.chunk_id)
            if known is None:
                known = Citation(
                    index=len(self._order) + 1,
                    doc_id=hit.doc_id,
                    chunk_id=hit.chunk_id,
                    filename=hit.filename,
                    score=hit.score,
                    excerpt=hit.content[:200],
                )
                self._by_chunk[hit.chunk_id] = known
                self._order.append(hit.chunk_id)
            numbered.append((known.index, hit))
        return numbered

    def all(self) -> List[Citation]:
        """按首次出现顺序列出全部引用。"""
        return [self._by_chunk[chunk_id] for chunk_id in self._order]


class ChatService:
    """单个用户的会话。"""

    def __init__(self, db: Session, owner_id: int) -> None:
        self._db = db
        self._owner_id = owner_id
        self._agents = AgentService(db, owner_id)
        self._knowledge = KnowledgeService(db, owner_id)
        self._memory = MemoryService(db, owner_id)

    @property
    def owner_id(self) -> int:
        return self._owner_id

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select, column) -> Select:
        """把查询限定在当前 owner 上。所有读操作都要经过这里。"""
        return stmt.where(column == self._owner_id)

    # -- 模型配置 -----------------------------------------------------------

    def require_chat_config(self, config_id: Optional[int] = None) -> ModelConfig:
        """确定本次回答用哪份对话模型配置。

        传了 ``config_id`` 就用指定的那份（必须是本人的、对话用途、且已通过
        连通性测试）；否则退回「系统设置 → 模型管理」里的默认对话模型。
        """
        if config_id is not None:
            config = self._db.scalar(
                self._scope(select(ModelConfig), ModelConfig.owner_id).where(
                    ModelConfig.id == config_id
                )
            )
            if config is None:
                # 与 get_conversation 同理用 404：确认别人的配置存在也是泄露。
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

        config = default_model_config(self._db, self._owner_id, ModelPurpose.CHAT)
        if config is None or not config.last_test_ok:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                "尚未配置可用的对话模型。"
                "请前往「系统设置 → 模型管理」添加对话模型并通过连通性测试。",
            )
        return config

    # -- 会话 ---------------------------------------------------------------

    def list_conversations(self, *, include_ops: bool = False) -> List[ConversationItem]:
        """会话列表。绑定运维目标的问答会话默认不出现——它属于运维页面的
        嵌入面板，混进主对话侧栏只会是噪音。"""
        stmt = self._scope(select(ChatConversation), ChatConversation.owner_id)
        if not include_ops:
            stmt = stmt.where(ChatConversation.ops_target_type.is_(None))
        rows = list(
            self._db.scalars(
                stmt.order_by(
                    ChatConversation.updated_at.desc(), ChatConversation.id.desc()
                )
            ).all()
        )
        counts = dict(
            self._db.execute(
                select(ChatMessage.conversation_id, func.count(ChatMessage.id))
                .where(ChatMessage.owner_id == self._owner_id)
                .group_by(ChatMessage.conversation_id)
            ).all()
        )
        return [
            ConversationItem(
                id=row.id,
                title=row.title,
                agent_id=row.agent_id,
                project_id=row.project_id,
                type_ids=row.type_ids,
                model_config_id=row.model_config_id,
                local_tool_keys=row.local_tool_keys,
                mcp_server_ids=row.mcp_server_ids,
                message_count=int(counts.get(row.id, 0)),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    def _validate_owned_mcp_servers(self, mcp_ids: Sequence[int]) -> None:
        """工具挂载保存时的归属校验：存在且属于本人即可，停用放行。

        与工作流保存（workflows/defs.py）同一口径：允许先挂载后启用，停用项
        在问答运行时由 resolve_servers 再挡一道。
        """
        owned = self._db.scalars(
            select(McpServer.id).where(
                McpServer.owner_id == self._owner_id,
                McpServer.id.in_(mcp_ids),
            )
        ).all()
        if len(set(owned)) != len(set(mcp_ids)):
            raise ValueError("挂载了不存在或不属于本人的 MCP 服务，请重新选择")

    def get_conversation(self, conversation_id: int) -> ChatConversation:
        row = self._db.scalar(
            self._scope(select(ChatConversation), ChatConversation.owner_id).where(
                ChatConversation.id == conversation_id
            )
        )
        if row is None:
            # 返回 404 而不是 403：确认别人的记录存在，本身就是泄露。
            raise LookupError("会话不存在")
        return row

    def create_conversation(
        self,
        *,
        title: str = "新会话",
        agent_id: Optional[int] = None,
        project_id: Optional[int] = None,
        type_ids: Optional[Sequence[int]] = None,
        model_config_id: Optional[int] = None,
        local_tool_keys: Optional[Sequence[str]] = None,
        mcp_server_ids: Optional[Sequence[int]] = None,
    ) -> ChatConversation:
        if agent_id is not None:
            self._agents.get(agent_id)  # 校验可见性，不通过则抛 LookupError
        if project_id is not None:
            self._knowledge.get_project(project_id)  # 已按 owner 限定，抛 LookupError
        if model_config_id is not None:
            self.require_chat_config(model_config_id)
        local_keys = list(dict.fromkeys(local_tool_keys)) if local_tool_keys else None
        mcp_ids = list(dict.fromkeys(mcp_server_ids)) if mcp_server_ids else None
        if local_keys:
            validate_local_tool_keys(local_keys)
        if mcp_ids:
            self._validate_owned_mcp_servers(mcp_ids)

        row = ChatConversation(
            owner_id=self._owner_id,
            title=title[:255] or "新会话",
            agent_id=agent_id,
            project_id=project_id,
            type_ids=list(type_ids) if type_ids else None,
            model_config_id=model_config_id,
            local_tool_keys=local_keys,
            mcp_server_ids=mcp_ids,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def update_conversation(
        self,
        conversation_id: int,
        *,
        title: Optional[str] = None,
        agent_id: Optional[int] = None,
        project_id: Optional[int] = None,
        type_ids: Optional[Sequence[int]] = None,
        model_config_id: Optional[int] = None,
        local_tool_keys: Optional[Sequence[str]] = None,
        mcp_server_ids: Optional[Sequence[int]] = None,
    ) -> ChatConversation:
        row = self.get_conversation(conversation_id)
        if title is not None:
            row.title = title[:255]
        if agent_id is not None:
            self._agents.get(agent_id)
            row.agent_id = agent_id
        if project_id is not None:
            self._knowledge.get_project(project_id)
            row.project_id = project_id
            if type_ids is None:
                # 这些类型属于旧项目，切换之后会悄无声息地把整个会话限定到
                # 一个空范围里。
                row.type_ids = None
        if type_ids is not None:
            row.type_ids = list(type_ids) or None
        if model_config_id is not None:
            self.require_chat_config(model_config_id)
            row.model_config_id = model_config_id
        # 与 type_ids 同口径：None=保持不动，[]=清空；非空先校验再写。
        if local_tool_keys is not None:
            keys = list(dict.fromkeys(local_tool_keys)) or None
            if keys:
                validate_local_tool_keys(keys)
            row.local_tool_keys = keys
        if mcp_server_ids is not None:
            mcp_ids = list(dict.fromkeys(mcp_server_ids)) or None
            if mcp_ids:
                self._validate_owned_mcp_servers(mcp_ids)
            row.mcp_server_ids = mcp_ids
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete_conversation(self, conversation_id: int) -> None:
        row = self.get_conversation(conversation_id)

        # 附件文件先记下来：行删掉并 commit 之后 ORM 属性已不可读。
        rel_paths = [
            rel_path
            for rel_path in self._db.scalars(
                select(ChatAttachment.rel_path).where(
                    ChatAttachment.owner_id == self._owner_id,
                    ChatAttachment.conversation_id == row.id,
                )
            ).all()
            if rel_path
        ]
        self._db.execute(
            delete(ChatMessage).where(
                ChatMessage.owner_id == self._owner_id,
                ChatMessage.conversation_id == row.id,
            )
        )
        self._db.execute(
            delete(ChatAttachment).where(
                ChatAttachment.owner_id == self._owner_id,
                ChatAttachment.conversation_id == row.id,
            )
        )
        # 待确认项跟着会话走：会话没了，挂起的确认卡片也不该留在任何地方。
        # 审计表不动——ops_audit_log 是留痕，不是会话内容。
        self._db.execute(
            delete(OpsPendingAction).where(
                OpsPendingAction.owner_id == self._owner_id,
                OpsPendingAction.conversation_id == row.id,
            )
        )
        # 摘要属于会话本身，跟着会话走；长期记忆属于用户，不随会话删除。
        self._memory.delete_summary(row.id)
        self._db.delete(row)
        self._db.commit()
        for rel_path in rel_paths:
            delete_stored(rel_path)

    # -- 运维问答会话 ----------------------------------------------------------

    def _ops_persona(self, target_type: str, target_id: int) -> Tuple[str, str]:
        """校验运维目标归属并给出内置人设 slug 与目标名（标题兜底用）。"""
        if target_type == TARGET_SERVER:
            target = server_ops.OpsServerService(self._db, self._owner_id).get(target_id)
            return "server-ops", target.name
        if target_type == "database":
            target = db_ops.OpsDatabaseService(self._db, self._owner_id).get(target_id)
            return "db-ops", target.name
        raise LookupError("运维目标类型不支持")

    def create_ops_conversation(
        self, target_type: str, target_id: int, title: Optional[str] = None
    ) -> ChatConversation:
        """为某个运维目标新开一个问答会话。

        嵌入面板每开一个新窗口就建一个（旧会话从历史列表里翻回去），目标是
        哪台机器/哪个库决定了会话用哪个人设（内置的 server-ops / db-ops）、
        挂哪种形态的工具箱。
        """
        slug, target_name = self._ops_persona(target_type, target_id)
        agent = self._agents.require_by_slug(slug)
        row = ChatConversation(
            owner_id=self._owner_id,
            title=(title or f"{target_name} 运维问答")[:255],
            agent_id=agent.id,
            ops_target_type=target_type,
            ops_target_id=target_id,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def list_ops_conversations(
        self, target_type: str, target_id: int
    ) -> List[Tuple[ChatConversation, int]]:
        """一个运维目标的全部问答会话（最近活跃的在前），连带各自的消息条数。

        面板的历史下拉一趟拿全：标题、时间、条数都在，免得逐行 count。
        会话是「发出第一条提问时才落库」的懒创建，正常不会出现空会话。
        """
        if target_type not in (TARGET_SERVER, "database"):
            raise LookupError("运维目标类型不支持")
        stmt = (
            select(ChatConversation, func.count(ChatMessage.id))
            .outerjoin(
                ChatMessage, ChatMessage.conversation_id == ChatConversation.id
            )
            .where(
                ChatConversation.owner_id == self._owner_id,
                ChatConversation.ops_target_type == target_type,
                ChatConversation.ops_target_id == target_id,
            )
            .group_by(ChatConversation.id)
            .order_by(ChatConversation.updated_at.desc())
        )
        return [(row, int(count)) for row, count in self._db.execute(stmt).all()]

    # -- 消息 ---------------------------------------------------------------

    def list_messages(self, conversation_id: int) -> List[MessageItem]:
        self.get_conversation(conversation_id)
        rows = list(
            self._db.scalars(
                self._scope(select(ChatMessage), ChatMessage.owner_id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id)
            ).all()
        )
        attachments = self._attachments_by_message([row.id for row in rows])
        return [self.to_message(row, attachments.get(row.id)) for row in rows]

    @staticmethod
    def to_attachment(row: ChatAttachment) -> ChatAttachmentItem:
        return ChatAttachmentItem(
            id=row.id,
            kind=row.kind,
            filename=row.filename,
            mime=row.mime,
            size=row.size,
            url=image_url(row.rel_path) if row.kind == "image" and row.rel_path else None,
        )

    @classmethod
    def to_message(
        cls, row: ChatMessage, attachments: Optional[Sequence[ChatAttachment]] = None
    ) -> MessageItem:
        return MessageItem(
            id=row.id,
            role=row.role,
            content=row.content,
            citations=[Citation(**item) for item in (row.citations or [])],
            timeline=row.timeline,
            attachments=[cls.to_attachment(item) for item in (attachments or [])],
            created_at=row.created_at,
        )

    def _attachments_by_message(
        self, message_ids: Sequence[int]
    ) -> dict[int, List[ChatAttachment]]:
        if not message_ids:
            return {}
        rows = list(
            self._db.scalars(
                self._scope(select(ChatAttachment), ChatAttachment.owner_id)
                .where(ChatAttachment.message_id.in_(list(message_ids)))
                .order_by(ChatAttachment.id)
            ).all()
        )
        grouped: dict[int, List[ChatAttachment]] = {}
        for row in rows:
            if row.message_id is not None:
                grouped.setdefault(row.message_id, []).append(row)
        return grouped

    def describe_attachments(self, attachment_ids: Sequence[int]) -> List[ChatAttachment]:
        """按用户传来的顺序取回附件；不存在/已被别的消息占用都会拒绝。

        上传与发送是两次请求，中间可能已经有一轮把同一批附件用掉了。校验放在
        消息落库之前，避免历史里出现一条引用着别人已验证附件的提问。
        """
        ids = list(dict.fromkeys(attachment_ids))
        if not ids:
            return []
        if len(ids) > settings.CHAT_MAX_ATTACHMENTS:
            raise BusinessError(
                400, f"一条消息最多附带 {settings.CHAT_MAX_ATTACHMENTS} 个附件"
            )
        rows = list(
            self._db.scalars(
                self._scope(select(ChatAttachment), ChatAttachment.owner_id)
                .where(ChatAttachment.id.in_(ids))
            ).all()
        )
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(ids):
            # 与 get_conversation 同理：不区分「不存在」与「不是你的」。
            raise LookupError("附件不存在")
        ordered = [by_id[item_id] for item_id in ids]
        if any(row.message_id is not None for row in ordered):
            raise BusinessError(409, "附件已经被发送过了，请重新上传")
        return ordered

    @staticmethod
    def assert_vision_ready(attachments: Sequence[ChatAttachment], config: ModelConfig) -> None:
        if any(row.kind == "image" for row in attachments) and not config.supports_vision:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                f"对话模型「{config.title}」未开启图片理解，请换一个视觉模型，"
                "或到「系统设置 → 模型管理」为该模型勾选「图片理解」。",
            )

    def create_attachment(
        self,
        *,
        kind: str,
        filename: str,
        mime: Optional[str],
        size: int,
        rel_path: Optional[str] = None,
        text: Optional[str] = None,
    ) -> ChatAttachment:
        row = ChatAttachment(
            owner_id=self._owner_id,
            kind=kind,
            filename=filename[:255] or "attachment",
            mime=mime,
            size=size,
            rel_path=rel_path,
            text=text,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete_attachment(self, attachment_id: int) -> None:
        """删掉一个还没发出去的附件；已绑定消息的不许删。"""
        row = self._db.scalar(
            self._scope(select(ChatAttachment), ChatAttachment.owner_id).where(
                ChatAttachment.id == attachment_id
            )
        )
        if row is None:
            raise LookupError("附件不存在")
        if row.message_id is not None:
            raise BusinessError(409, "已发送的附件不能删除")
        rel_path = row.rel_path
        self._db.delete(row)
        self._db.commit()
        delete_stored(rel_path)

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        citations: Optional[List[Citation]] = None,
        attachment_ids: Optional[Sequence[int]] = None,
    ) -> ChatMessage:
        """同步入口：供同步路由（completions、reject）直接调用。"""
        attachments = (
            self.describe_attachments(attachment_ids) if attachment_ids else None
        )
        message_id = self._persist_message(
            self._db,
            self._owner_id,
            conversation_id,
            role,
            content,
            citations=citations,
            attachments=attachments,
            close_session=False,
        )
        return self._db.get(ChatMessage, message_id)

    @staticmethod
    def _persist_message(
        db: Session,
        owner_id: int,
        conversation_id: int,
        role: str,
        content: str,
        *,
        citations: Optional[List[Citation]] = None,
        backfill_actions: Optional[List[OpsPendingAction]] = None,
        timeline: Optional[List[dict]] = None,
        attachments: Optional[List[ChatAttachment]] = None,
        close_session: bool = True,
    ) -> int:
        """在指定 session 里落一条消息并 commit，返回 message id。

        与 ``add_message`` 同一份逻辑，但 session 由调用方传入：async 路径可以
        把它丢进 ``asyncio.to_thread``，用自己的 session，不把同步 commit 留在
        事件循环上；可选的 ``backfill_actions`` 让回填与消息落在同一次 commit
        里，省掉第二次线程切换。``close_session`` 控制是否在返回前关闭 session：
        async 路径传 True（自己开的自己关），sync 路径传 False（调用方的 session
        不该被关掉）。
        """
        row = ChatMessage(
            owner_id=owner_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            citations=[c.model_dump() for c in citations] if citations else None,
            timeline=timeline,
        )
        db.add(row)

        if attachments:
            # 先 flush 拿到消息 id，附件与消息落在同一次 commit 里。
            db.flush()
            for attachment in attachments:
                attachment.message_id = row.id
                attachment.conversation_id = conversation_id

        conversation = db.get(ChatConversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = func.now()

        db.commit()
        db.refresh(row)
        message_id = row.id

        if backfill_actions:
            for action in backfill_actions:
                merged = db.merge(action)
                merged.message_id = message_id
            db.commit()

        if close_session:
            db.close()
        return message_id

    def update_message_content(self, message_id: int, content: str) -> ChatMessage:
        """改写一条助手消息的正文（目前唯一的入口是用户手动改图）。

        只允许动 assistant 角色：user 消息是提问的原貌，改了它，下面的回答
        就成了答非所问。改写直接落在原行上——历史回放给模型的就是这份内容，
        不需要额外的「版本」概念。
        """
        row = self._db.scalar(
            self._scope(select(ChatMessage), ChatMessage.owner_id).where(
                ChatMessage.id == message_id
            )
        )
        if row is None:
            # 与 get_conversation 同理用 404：确认别人的消息存在也是泄露。
            raise LookupError("消息不存在")
        if row.role != ROLE_ASSISTANT:
            raise BusinessError(409, "只有助手消息可以修正")
        row.content = content
        self._db.commit()
        self._db.refresh(row)
        return row

    def pop_dangling_user_message(self, conversation_id: int) -> Tuple[str, List[int]]:
        """删掉会话末尾那条没得到回答的用户消息，返回 ``(内容, 附件 id)``。

        流式回答失败的遗留形态：路由先落库 user 消息再开始生成
        （routers/chat.py 的顺序），失败时 user 在、assistant 不在。
        重试由后端先把它删掉再走正常回答流程（会重新落一条），
        历史里就不会出现两条一模一样的提问。末尾已是 assistant 说明
        上次回答成功了，没有可重试的东西。

        附件不删：先把 message_id 解绑，重试重新落消息时再绑回去，
        否则这条提问重答之后，当初上传的文件就凭空消失了。
        """
        row = (
            self._db.scalars(
                self._scope(select(ChatMessage), ChatMessage.owner_id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                .limit(1)
            ).first()
        )
        if row is None or row.role != ROLE_USER:
            raise BusinessError(409, "当前会话没有可重试的提问")
        attachments = self._attachments_by_message([row.id]).get(row.id, [])
        for attachment in attachments:
            attachment.message_id = None
        content = row.content
        self._db.delete(row)
        self._db.commit()
        return content, [attachment.id for attachment in attachments]

    # -- 运维写命令待确认项 ------------------------------------------------------

    @staticmethod
    def to_action(row: OpsPendingAction) -> OpsActionItem:
        return OpsActionItem(
            id=row.id,
            conversation_id=row.conversation_id,
            message_id=row.message_id,
            target_type=row.target_type,
            target_id=row.target_id,
            target_name=row.target_name,
            command=row.command,
            reason=row.reason,
            status=ops_actions.display_status(row),
            result=row.result,
            exit_status=row.exit_status,
            timeout_seconds=int(settings.OPS_CONFIRM_TIMEOUT),
            created_at=row.created_at,
            resolved_at=row.resolved_at,
        )

    def list_actions(self, conversation_id: int) -> List[OpsActionItem]:
        """一个会话的全部待确认项（含已处理的），历史回放用。"""
        self.get_conversation(conversation_id)
        rows = ops_actions.list_for_conversation(self._db, self._owner_id, conversation_id)
        return [self.to_action(row) for row in rows]

    def reject_action(self, action_id: int) -> OpsActionItem:
        """拒绝一条待确认命令：CAS 置 rejected、留痕、补一条助手消息让历史自洽。"""
        try:
            action = ops_actions.claim_for_reject(self._db, self._owner_id, action_id)
        except LookupError:
            # 与 confirm 同一口径：区分「不存在」和「来晚了」。
            ops_actions.get_owned(self._db, self._owner_id, action_id)
            raise BusinessError(409, "该命令已处理，请刷新查看最新状态")
        Auditor(self._owner_id, action.target_type).record_sync(
            target_id=action.target_id,
            target_name=action.target_name,
            command=action.command,
            verdict="rejected",
            success=False,
            pending_action_id=action.id,
        )
        # 落一条助手消息：历史里「提议 → 没有下文」会让模型以为命令还在等，
        # 明确告诉它（和后来的读者）这件事已经结束。
        self.add_message(
            action.conversation_id,
            ROLE_ASSISTANT,
            f"已拒绝执行命令：`{action.command}`。如需替代方案，请继续告诉我。",
        )
        return self.to_action(action)

    async def confirm_action_stream(
        self,
        *,
        action_id: int,
        is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> AsyncIterator[Tuple[str, object]]:
        """确认并执行一条待确认命令，然后基于结果续答（SSE 事件流）。

        两阶段落库的下半场：propose 早已落库返回，这里是另一个 HTTP 请求、
        很可能是另一个 worker——执行发生在本请求内，所以不需要任何跨进程
        状态。事件序列固定为 ``exec → token* → done|error``。
        """
        try:
            action = ops_actions.claim_for_confirm(self._db, self._owner_id, action_id)
        except LookupError:
            # 区分「不存在」（404）与「已处理/已超时」（409），前端据此给文案。
            ops_actions.get_owned(self._db, self._owner_id, action_id)
            raise BusinessError(409, "该命令已处理或已超时，请让 AI 重新提议")

        conversation = self.get_conversation(action.conversation_id)

        toolbox = ChatOpsToolbox(
            self._db,
            self._owner_id,
            target=OpsTarget(action.target_type, action.target_id),
            conversation_id=conversation.id,
        )
        try:
            result = await toolbox.execute_confirmed(action)
        finally:
            await toolbox.aclose()

        # exec 先于续答涉及的一切解析发出：模型配置这类问题绝不能吞掉一条
        # 已经执行完的命令的结果。
        yield "exec", {
            "action_id": action.id,
            "command": action.command,
            "exit_status": result.exit_status,
            "output": result.output,
            "elapsed_ms": result.elapsed_ms,
            "success": result.success,
        }

        agent = (
            self._agents.get(conversation.agent_id)
            if conversation.agent_id is not None
            else self.resolve_agent(None)
        )
        config = self.require_chat_config(conversation.model_config_id)

        # 续答：执行结果合成一条「系统旁白」作为最新输入喂给模型。它不落
        # user 消息——用户的提问流不该出现一条他没说过的话；但历史里已有
        # 之前的问答，模型看得到来龙去脉。
        context = (
            f"[系统] 用户已批准在 {action.target_name or action.target_type} 上执行命令：\n"
            f"{action.command}\n\n"
            f"退出码：{result.exit_status}（耗时 {result.elapsed_ms}ms）\n"
            f"输出：\n{result.output}\n\n"
            "请基于执行结果继续分析。"
        )
        async for event, data in self.stream_answer(
            conversation_id=conversation.id,
            agent=agent,
            question=context,
            config=config,
            project_id=conversation.project_id,
            type_ids=conversation.type_ids,
            is_disconnected=is_disconnected,
            question_persisted=False,
        ):
            yield event, data

    def _history(self, conversation_id: int) -> List[ChatMessage]:
        """最近的若干轮对话，按从旧到新排列。

        有手动压缩锚点时，锚点及之前的消息永不回放：取回来后先按锚点截断，
        再走自动窗口与「不拆问答对」的切点保护。锚点之后的消息可能不足一窗
        （刚压缩完、保留轮数小于 HISTORY_TURNS），全部回放即可。
        """
        window = HISTORY_TURNS * 2
        anchor = self._memory.anchor_id(conversation_id)
        rows = list(
            self._db.scalars(
                self._scope(select(ChatMessage), ChatMessage.owner_id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                # 多取一条：窗口外紧邻的若是用户提问，split_replay_window
                # 会把整对问答划进回放段（它的回答正是窗口第一条）——与
                # 摘要重算共用同一条边界，提问不会被切成「摘要里挂待办、
                # 窗口里只剩回答」。正常问答序列最多只需这一条。
                .limit(window + 1)
            ).all()
        )
        rows.reverse()
        if anchor is not None:
            rows = [row for row in rows if row.id > anchor]
        return split_replay_window(rows, window)[1]

    # -- 手动压缩 -------------------------------------------------------------

    def _compaction_item(self, row) -> CompactionStateItem:
        """把摘要行换算成给前端的状态：covered_count 按当前消息实况算。"""
        rows = self._memory.list_all_messages(row.conversation_id)
        if row.cut_message_id is not None:
            covered = sum(1 for message in rows if message.id <= row.cut_message_id)
        else:
            overflow, _ = split_replay_window(rows, HISTORY_TURNS * 2)
            covered = len(overflow)
        return CompactionStateItem(
            summary=row.summary,
            cut_message_id=row.cut_message_id,
            instruction=row.instruction,
            covered_count=covered,
            updated_at=row.updated_at,
        )

    def compaction_state(self, conversation_id: int) -> Optional[CompactionStateItem]:
        """会话当前的摘要/压缩状态；连自动摘要都没有时返回 None。"""
        self.get_conversation(conversation_id)
        row = self._memory.summary_row(conversation_id)
        return self._compaction_item(row) if row is not None else None

    async def compaction_preview(
        self,
        *,
        conversation_id: int,
        keep_turns: int,
        instruction: Optional[str] = None,
        is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    ) -> AsyncIterator[Tuple[str, object]]:
        """压缩预览流：``compact_meta`` 先给切点与计数，随后 ``token*``，最后
        ``compact_done``。全程不写库；应用是另一次请求。"""
        conversation = self.get_conversation(conversation_id)
        config = self.require_chat_config(conversation.model_config_id)
        cut_message_id, overflow, replay = self._memory.plan_compaction(
            conversation_id, keep_turns
        )
        yield "compact_meta", {
            "cut_message_id": cut_message_id,
            "covered_count": len(overflow),
            "kept_count": len(replay),
        }
        produced = False
        async for piece in self._memory.stream_compaction(
            config,
            conversation_id,
            overflow,
            instruction,
            is_disconnected=is_disconnected,
        ):
            if piece:
                produced = True
                yield "token", piece
        if not produced:
            raise BusinessError(400, "摘要模型没有返回内容，请重新生成。")
        yield "compact_done", {"cut_message_id": cut_message_id}

    def apply_compaction(
        self,
        conversation_id: int,
        cut_message_id: int,
        summary: str,
        instruction: Optional[str],
    ) -> CompactionStateItem:
        """应用预览过的压缩，返回最新状态。切点合法性由 memory 层兜底校验。"""
        self.get_conversation(conversation_id)
        row = self._memory.apply_compaction(
            conversation_id, cut_message_id, summary, instruction
        )
        return self._compaction_item(row)

    def clear_compaction(self, conversation_id: int) -> None:
        """取消压缩：删掉摘要行，回放恢复成自动窗口口径（下一轮问答会重建
        自动摘要）。消息原文一条不删。"""
        self.get_conversation(conversation_id)
        self._memory.delete_summary(conversation_id)

    # -- 智能体选择 ---------------------------------------------------------

    def resolve_agent(self, agent_id: Optional[int]) -> Agent:
        if agent_id is not None:
            return self._agents.get(agent_id)

        # 默认人选只从「对对话页开放」的智能体里挑：运维、简历这类后台人设
        # （chat_visible=False）即使用户全部禁用前台人设，也不能顶上来答对话。
        for agent in self._agents.list(enabled_only=True):
            if agent.chat_visible:
                return agent
        raise BusinessError(CODE_CHAT_NOT_READY, "没有可用的智能体")

    # -- 主流程 -------------------------------------------------------------

    def _search_tool(
        self,
        registry: _CitationRegistry,
        project_id: Optional[int],
        type_ids: Optional[Sequence[int]],
    ):
        """把知识库检索包成一个模型可以调用的工具。

        ``project_id`` 和 ``type_ids`` 是闭包捕获的，不作为工具参数暴露：
        检索范围由用户在界面上选定，模型只能决定查什么，不能决定查哪儿。
        """
        from langchain_core.tools import tool

        scope = list(type_ids) if type_ids else None

        @tool
        async def search_knowledge_base(query: str) -> str:
            """在知识库中检索资料。

            需要事实依据时调用。query 用一句自然语言描述要找的内容，越具体
            越好。问题涉及多个方面时，分多次调用、每次只查一个方面，比一次
            塞进一个笼统的问题效果好。
            """
            try:
                hits = await self._knowledge.search(
                    query,
                    top_k=RETRIEVAL_TOP_K,
                    project_id=project_id,
                    type_ids=scope,
                )
            except Exception as exc:
                # 不拦：ToolNode 会把异常包成 ToolMessage 喂回模型，让它换个
                # 查法重试。但只留在那里就完全不可见，这里记一笔。
                logger.warning("knowledge search tool failed: %s", exc)
                raise
            if not hits:
                return "没有检索到相关资料。"
            return _format_hits(registry.register(hits))

        return search_knowledge_base

    def _plan_attachment_context(
        self,
        history: Sequence[ChatMessage],
        attachment_map: dict[int, List[ChatAttachment]],
        current: Sequence[ChatAttachment],
        *,
        supports_vision: bool,
    ) -> dict[object, List[_AttachmentPlan]]:
        """给历史与当前附件分配本轮预算，优先保证最近一轮。

        文档正文按字符总预算展开，图片按张数。超出的旧附件不直接丢掉：
        留一行说明，模型才知道「用户当时确实传过东西」。
        """
        groups: List[Tuple[object, Sequence[ChatAttachment]]] = [
            (row.id, attachment_map[row.id])
            for row in history
            if row.role == ROLE_USER and attachment_map.get(row.id)
        ]
        if current:
            groups.append(("current", current))

        text_budget = REPLAY_ATTACHMENT_TEXT_BUDGET
        image_budget = REPLAY_IMAGE_LIMIT
        plan: dict[object, List[_AttachmentPlan]] = {}

        for key, attachments in reversed(groups):
            entries: List[_AttachmentPlan] = []
            for attachment in attachments:
                entry = _AttachmentPlan(attachment=attachment)
                if attachment.kind == "image":
                    if not supports_vision:
                        entry.note = "当前模型不支持图片理解，内容未提供"
                    elif image_budget > 0:
                        entry.include_image = True
                        image_budget -= 1
                    else:
                        entry.note = "历史图片过多，本轮未附带"
                else:
                    text = attachment.text or ""
                    if not text:
                        entry.note = "没有可提取的文本"
                    elif text_budget <= 0:
                        entry.note = "超出本轮附件上下文预算，未展开"
                    else:
                        entry.text = text[:text_budget]
                        entry.truncated = len(entry.text) < len(text)
                        text_budget -= len(entry.text)
                entries.append(entry)
            plan[key] = entries
        return plan

    @staticmethod
    def _human_content(
        text: str,
        entries: Sequence[_AttachmentPlan],
    ) -> object:
        """把一行 user 消息拼成 langchain 能吃的 content。

        没有图片时仍用纯字符串：纯文本模型对 ``content`` 数组的接受度并不一致。
        有图片才切成 ``[{type: text}, {type: image_url}]`` 的多模态形态。
        """
        blocks: List[str] = []
        if text.strip():
            blocks.append(text)
        image_parts: List[dict] = []

        for entry in entries:
            attachment = entry.attachment
            if attachment.kind == "file":
                if entry.text:
                    suffix = "（内容过长，已截断）" if entry.truncated else ""
                    blocks.append(f"[附件：{attachment.filename}]{suffix}\n{entry.text}")
                else:
                    blocks.append(
                        f"[附件：{attachment.filename}]（{entry.note or '内容未随本轮提供'}）"
                    )
                continue

            if entry.include_image:
                data_url = read_image_data_url(attachment.rel_path or "", attachment.mime)
                if data_url:
                    blocks.append(f"[图片：{attachment.filename}]")
                    image_parts.append(
                        {"type": "image_url", "image_url": {"url": data_url}}
                    )
                else:
                    blocks.append(f"[图片：{attachment.filename}]（文件已不可用）")
            else:
                blocks.append(
                    f"[图片：{attachment.filename}]（{entry.note or '未随本轮提供'}）"
                )

        combined = "\n\n".join(blocks)
        if not image_parts:
            return combined
        content: List[dict] = []
        if combined:
            content.append({"type": "text", "text": combined})
        content.extend(image_parts)
        return content

    def _replay(
        self,
        conversation_id: int,
        question: str,
        *,
        question_persisted: bool = True,
        supports_vision: bool = True,
    ) -> list:
        """把历史和当前问题拼成 ReAct 的初始消息列表。

        ``question_persisted`` 标记调用方是否已把这条问题落库：completions
        路由先落库再问（失败重试需要它），confirm 续答的合成上下文则刻意
        不落——两种情况历史里「末尾那条 user 消息」的含义正好相反。

        附件不落进 ``ChatMessage.content``：回放时从附件表取出来再拼。文档
        展开正文，图片转成 data URL；两者都受 :func:`_plan_attachment_context`
        的预算约束。
        """
        from langchain_core.messages import AIMessage, HumanMessage

        history = self._history(conversation_id)
        attachment_map = self._attachments_by_message([row.id for row in history])

        current: List[ChatAttachment] = []
        # 刚存下的那个问题已经在历史里了，先丢掉，免得跟下面单独追加的问题
        # 重复一遍。
        if question_persisted and history and history[-1].role == ROLE_USER:
            current = attachment_map.pop(history[-1].id, [])
            history = history[:-1]

        plan = self._plan_attachment_context(
            history, attachment_map, current, supports_vision=supports_vision
        )

        messages: list = []
        for row in history:
            if row.role == ROLE_USER:
                messages.append(
                    HumanMessage(
                        content=self._human_content(
                            row.content, plan.get(row.id, [])
                        )
                    )
                )
            elif row.role == ROLE_ASSISTANT:
                messages.append(AIMessage(content=row.content))
        messages.append(
            HumanMessage(content=self._human_content(question, plan.get("current", [])))
        )
        return messages

    async def stream_answer(
        self,
        *,
        conversation_id: int,
        agent: Agent,
        question: str,
        config: ModelConfig,
        project_id: Optional[int] = None,
        type_ids: Optional[Sequence[int]] = None,
        is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
        question_persisted: bool = True,
    ) -> AsyncIterator[Tuple[str, object]]:
        """为 SSE 接口产出 ``(event, payload)`` 二元组。

        事件顺序由模型的行为决定，不再是固定的：它每发起一次检索会先来一个
        ``step``，工具返回后跟一次 ``citations``（累积到当前为止的完整列表，
        客户端整体替换即可），正文以若干 ``token`` 增量发出，最后 ``done``
        带上已落库的 message id。模型一次都不检索时，就一个 ``citations``
        都不会有。propose_command 落库后跟随一次 ``confirm`` 事件，把待确认
        项推给前端渲染确认卡片。

        ``is_disconnected`` 让调用方把客户端的连接状态传进来。用户点了停止
        之后，生成随即中断，已经收到的内容会带着标记保存下来，这样重新打开
        会话时看到的，还是用户当时看到的那个截断版本，而不是一段他从没收到
        过的完整回答。

        ``question_persisted`` 见 ``_replay``：confirm 续答传 False。
        """
        from langchain.agents import create_agent
        from langgraph.errors import GraphRecursionError

        if agent.use_knowledge and project_id is None:
            # 搜遍所有项目，就会拿用户根本没圈进来的文档来作答，而项目这个
            # 东西存在的全部理由就在于此。这是用户的操作前提，不该等模型去
            # 发现，所以拦在循环外面。
            raise BusinessError(CODE_CHAT_NOT_READY, "请先选择项目")

        conversation = self.get_conversation(conversation_id)
        logger.info(
            "streaming an answer for conversation %s (model config %s, agent %s)",
            conversation_id,
            config.id,
            agent.slug,
        )

        registry = _CitationRegistry()
        # 没有勾任何工具（也不检索知识库、不挂运维箱）时就是空列表：时间工具
        # 已经改成会话级可选的本地工具，不再强制挂载。
        tools: list = []
        system = agent.system_prompt

        # 长期记忆与会话摘要注入在规则后缀之前：硬约束（引用、写确认）要留在
        # prompt 的最末尾，背景信息放在人设和各套规则之间。
        if agent.use_memory:
            memories = self._memory.prompt_section()
            if memories:
                system = f"{system}\n\n{MEMORY_RULES}\n{memories}"
        summary = self._memory.summary_for(conversation_id)
        if summary:
            system = (
                f"{system}\n\n以下是本次会话更早部分的摘要"
                f"（最近几轮对话在下文完整给出）：\n{summary}"
            )

        # 挂运维工具箱的两个入口：智能体自己开了 use_ops（自由模式），或者
        # 会话本身绑定了一个运维目标（嵌入问答的绑定模式——内置的
        # server-ops/db-ops 人设 use_ops=False，工具箱是靠会话目标挂上的）。
        # SSH 连接随这次回答复用、在下面的 finally 里关掉。
        toolbox: Optional[ChatOpsToolbox] = None
        if agent.use_ops or conversation.ops_target_type is not None:
            target = (
                OpsTarget(conversation.ops_target_type, conversation.ops_target_id)
                if conversation.ops_target_type is not None
                and conversation.ops_target_id is not None
                else None
            )
            toolbox = ChatOpsToolbox(
                self._db,
                self._owner_id,
                target=target,
                conversation_id=conversation_id,
            )
            # 运维嵌入面板没有工具选择器：时间工具随工具箱继续默认挂载，
            # 保持「运维 AI 永远知道现在几点」的旧行为（主对话则改为显式勾选）。
            tools.extend(
                build_local_tools(
                    ["current_datetime"], ToolContext(self._owner_id, conversation_id)
                )
            )
            tools.extend(toolbox.tools())
            system = f"{system}\n\n{_OPS_RULES}{toolbox.prompt_suffix()}"

        if agent.use_knowledge:
            # 索引配置在进循环之前就校验掉。放进工具里就晚了：ToolNode 会把
            # 工具抛出的异常包成一条 ToolMessage 喂回模型，用户只会看到模型
            # 拿它当资料胡诌一通，而不是「请先配置向量模型」。
            self._knowledge.assert_index_usable(
                self._knowledge.require_embedding_config()
            )
            tools.append(self._search_tool(registry, project_id, type_ids))
            system = f"{system}\n\n{_CITATION_RULES}"

        # 会话级勾选的可选工具：本地方法（代码注册）+ 挂载的 MCP 服务。
        # MCP 连接在首事件之前打开，失败直接走 error 帧（同工作流）；连接束在
        # 下面的 finally 里关闭，stdio 子进程随之回收。
        mcp_bundle: Optional[McpToolBundle] = None
        if conversation.local_tool_keys:
            # 运维会话已默认挂过 current_datetime：会话选择里再出现同名 key 时
            # 跳过，而不是让末尾的查重把整轮回答挡掉。
            mounted = {getattr(tool, "name", "") for tool in tools}
            keys = [
                key
                for key in dict.fromkeys(conversation.local_tool_keys)
                if key not in mounted
            ]
            if keys:
                tools.extend(
                    build_local_tools(
                        keys, ToolContext(self._owner_id, conversation_id)
                    )
                )
        if conversation.mcp_server_ids:
            try:
                servers = resolve_servers(
                    self._db, self._owner_id, conversation.mcp_server_ids
                )
            except LookupError as exc:
                raise LookupError(f"选择的 MCP 服务不可用：{exc}，请在工具选择中移除后重试") from exc
            except ValueError as exc:
                raise ValueError(f"{exc}，请重新启用或在工具选择中移除后重试") from exc
            try:
                mcp_bundle = await open_bundle(servers)
            except Exception as exc:  # noqa: BLE001 - 连接失败要落成用户看得懂的 error 帧
                raise ValueError(f"MCP 服务连接失败：{exc}") from exc
            tools.extend(cap_tool_output(tool) for tool in mcp_bundle.tools)
        # 生效集合（内置/本地/MCP）内不许重名：模型面对两个同名工具的行为不可控。
        ensure_unique_tool_names(tools)

        client = build_chat_model(
            config,
            temperature=resolve_temperature(config, agent.temperature),
            streaming=True,
        )
        # tools 为空（没勾选、不检索、不挂运维箱）时 create_agent 不会调用
        # bind_tools，纯文本问答不再强制模型支持 function calling；一旦挂了
        # 任一工具，模型就必须支持。
        executor = create_agent(client, tools, system_prompt=system)

        parts: List[str] = []
        # 按到达顺序记录整条回答流：文字、执行命令、思考过程、写命令确认提议。
        # 历史回放用它重建交错的步骤与解说。只有当它含非纯文字条目（step/reason/
        # confirm）时才落库，纯问答不重复存 content。
        timeline: List[dict] = []

        def _merge_text(kind: str, detail: str) -> None:
            """把连续同类的文字/思考增量并进上一条，与前端交错渲染保持一致。"""
            if timeline and timeline[-1].get("type") == kind:
                timeline[-1]["detail"] += detail
            else:
                timeline.append({"type": kind, "detail": detail})

        # 统一 trace 的计时：工具与模型往返各自按 langchain run_id 记开跑时刻，
        # 结束事件回来时算出耗时，挂回时间线条目。
        tool_started: dict = {}
        model_started: dict = {}

        aborted = False
        step_limited = False
        time_limited = False
        sent_proposals = 0
        sent_execs = 0
        deadline = time.monotonic() + ANSWER_WALL_CLOCK_LIMIT

        stream = executor.astream_events(
            {
                "messages": self._replay(
                    conversation_id,
                    question,
                    question_persisted=question_persisted,
                    supports_vision=config.supports_vision,
                )
            },
            config={
                # 运维排查与 MCP 多工具调用都可能连续多轮，统一放宽到 25；
                # 只挂本地小工具时循环复杂度不变，维持普通上限。
                "recursion_limit": (
                    OPS_REACT_STEPS
                    if toolbox is not None or mcp_bundle is not None
                    else MAX_REACT_STEPS
                )
            },
            version="v2",
        )
        try:
            async for event in stream:
                if is_disconnected is not None and await is_disconnected():
                    aborted = True
                    break
                if time.monotonic() > deadline:
                    time_limited = True
                    logger.warning(
                        "answer for conversation %s hit the wall-clock limit (%.0fs)",
                        conversation_id,
                        ANSWER_WALL_CLOCK_LIMIT,
                    )
                    break

                kind = event.get("event")
                if kind == "on_tool_start":
                    payload = (event.get("data") or {}).get("input") or {}
                    run_id = event.get("run_id") or ""
                    if run_id:
                        tool_started[run_id] = time.monotonic()
                    step_event = {
                        "tool": event.get("name") or "",
                        "query": str(payload.get("query", "")),
                        # 完整的工具入参：运维工具的展示文案要取 command/statement，
                        # 由前端按工具名挑字段。
                        "input": payload,
                        # 与 on_tool_end 配对的凭据：并行工具调用时靠它认门。
                        "run_id": run_id,
                        # trace 节点的起始时刻（本地墙上时间，ISO 字符串）；
                        # 耗时在 on_tool_end 回填。
                        "started_at": db_now().isoformat(),
                    }
                    timeline.append({"type": "step", **step_event})
                    yield "step", step_event
                elif kind == "on_tool_end":
                    # 工具结果挂回步骤并立刻推给前端：时间线能逐次展示「调用 →
                    # 返回」，而不是只有一行转圈圈的工具名。
                    result = (event.get("data") or {}).get("output")
                    result_event = _attach_step_result(
                        timeline, event, result, tool_started
                    )
                    if result_event is not None:
                        yield "step_result", result_event
                    yield "citations", [c.model_dump() for c in registry.all()]
                    if toolbox is not None and len(toolbox.readonly_execs) > sent_execs:
                        # 只读命令也发 exec（action_id 为空表示未经确认的直接
                        # 执行）：运维终端页靠它把命令与输出回显进终端窗口。
                        for executed in toolbox.readonly_execs[sent_execs:]:
                            yield "exec", {
                                "action_id": None,
                                "command": executed["command"],
                                "exit_status": executed["exit_status"],
                                "output": executed["output"],
                                "elapsed_ms": executed["elapsed_ms"],
                                "success": executed["exit_status"] == 0,
                            }
                        sent_execs = len(toolbox.readonly_execs)
                    # propose_command 落库后立刻把待确认项推给前端：确认卡片
                    # 要跟本轮回答一起出现，而不是等 done。
                    if toolbox is not None and len(toolbox.pending_proposals) > sent_proposals:
                        for action in toolbox.pending_proposals[sent_proposals:]:
                            # 位置也要落库：确认卡片按 action_id 就地插回这条
                            # 时间线，历史回放才能和流式时看到同一个顺序。
                            timeline.append({"type": "confirm", "action_id": action.id})
                            yield "confirm", {
                                "action_id": action.id,
                                "command": action.command,
                                "reason": action.reason,
                                "target_type": action.target_type,
                                "target_id": action.target_id,
                                "target_name": action.target_name,
                                "timeout_seconds": int(settings.OPS_CONFIRM_TIMEOUT),
                            }
                        sent_proposals = len(toolbox.pending_proposals)
                elif kind == "on_chat_model_start":
                    # 一次模型往返（工具循环每轮各一次）：记录开跑时刻，
                    # end 时落成一个 llm trace 节点——慢回答归因到「模型慢」
                    # 还是「工具慢」全靠它。
                    model_run_id = event.get("run_id") or ""
                    if model_run_id:
                        model_started[model_run_id] = time.monotonic()
                elif kind == "on_chat_model_end":
                    model_run_id = event.get("run_id") or ""
                    started_perf = model_started.pop(model_run_id, None)
                    if started_perf is not None:
                        llm_event = {
                            "elapsed_ms": int((time.monotonic() - started_perf) * 1000)
                        }
                        timeline.append({"type": "llm", **llm_event})
                        yield "llm", llm_event
                elif kind == "on_chat_model_stream":
                    chunk = (event.get("data") or {}).get("chunk")
                    reasoning = _chunk_reasoning(chunk)
                    if reasoning:
                        _merge_text("reason", reasoning)
                        yield "reason", reasoning
                    text = _chunk_text(chunk)
                    if text:
                        parts.append(text)
                        _merge_text("text", text)
                        yield "token", text
        except GraphRecursionError:
            # 模型一直在调用工具却不肯收尾。已经流出去的内容照常保存下来，不当成
            # 错误抛给用户——他看到的那半截回答总比一条报错有用；但要打上标记，
            # 否则运维排查被掐断时看起来就像 AI 说到一半没声了。
            step_limited = True
            logger.warning(
                "react loop hit the step limit for conversation %s", conversation_id
            )
        finally:
            # 关掉生成器，好让底层那条发往 provider 的 HTTP 请求被释放，
            # 而不是在后台一路跑完。
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
            # 这次回答期间建立的 SSH 连接一并收掉。
            if toolbox is not None:
                await toolbox.aclose()
            # MCP 连接束（含 stdio 子进程）LIFO 关闭。
            if mcp_bundle is not None:
                await mcp_bundle.__aexit__(None, None, None)

        # 存的就是流出去的全部文本。模型偶尔会在调用工具前先说一句「我查一下
        # 资料」，那句话也留着：它确实出现在了用户屏幕上，落库内容跟用户看到
        # 的对不上，才是更糟的事。
        answer = "".join(parts).strip()
        citations = registry.all()

        if aborted:
            # 把这段残缺的回答存下来，并打上标记。要是存完整文本，那么落库
            # 的会话内容就和用户当时看到的对不上了。
            answer = f"{answer}{ABORTED_SUFFIX}" if answer else ABORTED_EMPTY
        elif step_limited or time_limited:
            # 标记同样先补流出去再落库：用户屏幕上看到的和存下来的保持一致，
            # 他也知道发条「继续」就能让模型接着排查。
            suffix = STEP_LIMIT_SUFFIX if step_limited else TIME_LIMIT_SUFFIX
            yield "token", suffix
            _merge_text("text", suffix)
            answer = f"{answer}{suffix}" if answer else suffix.strip()
        elif not answer:
            answer = NO_CONTEXT_REPLY

        # 纯文字问答的 timeline 与 content 等价，不重复存；只有含步骤或思考时才落。
        timeline_to_save = (
            timeline if any(entry["type"] != "text" for entry in timeline) else None
        )

        # 消息落库 + 待确认项回填一起丢进工作线程：两者都需要 commit，合在
        # 同一个 session 里省掉第二次线程切换。self._db 留在事件循环上给后续
        # 的读取（如果有的话）用，不再被同步 commit 阻塞。
        actions_to_backfill = (
            toolbox.pending_proposals if toolbox is not None else None
        )
        message_id = await asyncio.to_thread(
            self._persist_message,
            app_db.SessionLocal(),
            self._owner_id,
            conversation_id,
            ROLE_ASSISTANT,
            answer,
            citations=citations or None,
            backfill_actions=actions_to_backfill,
            timeline=timeline_to_save,
        )

        # 一轮问答落库后的收尾（重算会话摘要、提取长期记忆）放到请求之外跑：
        # SSE 流一结束请求级 session 就关了，后台任务自己开 session。它失败
        # 静默，且摘要全量重算——这一次错过的，下一次回答会补回来。
        fire_and_forget(after_answer_task(self._owner_id, conversation_id, HISTORY_TURNS))

        logger.debug(
            "answer for conversation %s saved as message %s (%d chars, %d citation(s), aborted=%s)",
            conversation_id,
            message_id,
            len(answer),
            len(citations),
            aborted,
        )

        if aborted:
            # 不发 "done"：客户端已经走了。这里的意义就在于把内容存下来。
            return

        yield "done", {"message_id": message_id, "conversation_id": conversation_id}


def derive_title(question: str) -> str:
    """第一个问题顺带充当会话标题。"""
    title = " ".join(question.split())
    return title[:40] if len(title) <= 40 else f"{title[:40]}…"


# ---- 后台任务 -------------------------------------------------------------------

# 收尾任务的失败日志仍走 yangvis.memory 这个通道：搬过来的是模块归属，
# 日志按职责分，不按文件走。
_memory_logger = logging.getLogger("yangvis.memory")


async def after_answer_task(owner_id: int, conversation_id: int, history_turns: int) -> None:
    """一轮问答落库之后的收尾：重算会话摘要、提取长期记忆。

    自己开一个 session：任务真正跑起来的时候，请求级别的 session 早就关了
    （与 knowledge.index_document_task 同一形态）。任何一步失败都只记日志，
    下一步照常——全量重算的设计保证下一次成功调用会把错过的补回来。
    """
    # 延迟导入是有意的：顶层引一次会在进程里存两份 `SessionLocal`，测试 patch
    # `app.database` 那份动不到模块全局。函数体内导入等于把 patch 面收成一处，
    # 与 routers/interview.py、slides.py、workflows/exec.py 的后台任务同形。
    from app.database import SessionLocal

    with SessionLocal() as db:
        chat_service = ChatService(db, owner_id)
        memory = MemoryService(db, owner_id)

        try:
            conversation = chat_service.get_conversation(conversation_id)
        except LookupError:
            return  # 会话在回答落库后又被删了

        try:
            config = chat_service.require_chat_config(conversation.model_config_id)
        except (BusinessError, LookupError):
            # 对话模型配置被改掉/删掉了：记忆与摘要是增强，不是拦路的理由。
            # LookupError 是删掉之前的历史数据留下的悬空 id（delete() 现在会清）。
            return

        try:
            await memory.refresh_summary(
                conversation, config, history_turns=history_turns
            )
        except Exception:  # noqa: BLE001 - 绝不让后台任务无声无息地死掉
            _memory_logger.exception(
                "summary refresh failed for conversation %s", conversation_id
            )

        agent = None
        if conversation.agent_id is not None:
            try:
                agent = AgentService(db, owner_id).get(conversation.agent_id)
            except LookupError:
                pass
        try:
            await memory.extract_memories(conversation, config, agent)
        except Exception:  # noqa: BLE001
            _memory_logger.exception(
                "memory extraction failed for conversation %s", conversation_id
            )
