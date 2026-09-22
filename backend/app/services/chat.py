"""对话：ReAct 循环、检索工具与流式回答。

回答走 ReAct：检索被包成一个模型可以调用的工具，由模型自己决定要不要查、
查什么、查几次，结果回灌进对话再继续推理，直到它给出不带工具调用的答复。

代价要说清楚。旧版靠结构防幻觉——检索结果为空时干脆不调模型，它就没得选。
现在这条硬保证没了：模型完全可以一次都不查就开口，约束只剩 prompt 里的
引用规则，那是一句它可以不听的建议。换来的是追问、拆解、多角度检索的能力。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable, List, Optional, Sequence, Tuple

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.errors import CODE_CHAT_NOT_READY, BusinessError
from app.models.entities import (
    Agent,
    ChatConversation,
    ChatMessage,
    ModelConfig,
    OpsPendingAction,
)
from app.models.schemas import (
    Citation,
    ConversationItem,
    MessageItem,
    ModelPurpose,
    OpsActionItem,
)
from app.services import ops_actions
from app.services.agents import AgentService
from app.services.chat_ops import TARGET_SERVER, Auditor, ChatOpsToolbox, OpsTarget
from app.services.knowledge import KnowledgeService
from app.services.memory import (
    MEMORY_RULES,
    MemoryService,
    after_answer_task,
    fire_and_forget,
)
from app.services import ops_database as db_ops
from app.services import ops_server as server_ops
from app.services.providers import build_chat_model

logger = logging.getLogger("yangvis.chat")

settings = get_settings()

# 要回放多少轮历史。够撑住追问（「那第二个呢？」），又不至于让一段旧对话
# 把检索到的上下文挤出去。
HISTORY_TURNS = 6

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

# 相对时间（「今天」「本周」）一律按北京时间换算。用固定偏移而不是
# zoneinfo：Windows 没有 IANA 时区库，ZoneInfo("Asia/Shanghai") 在开发机上
# 得额外装 tzdata 才能跑。
_LOCAL_TZ = timezone(timedelta(hours=8))
_WEEKDAYS = "一二三四五六日"

NO_CONTEXT_REPLY = "知识库中未找到相关内容，无法回答这个问题。"

# 用户中途停止生成时追加的标记。不加这个标记的话，保存下来的文本看起来
# 就像是模型自己说到一半没声了。
ABORTED_SUFFIX = "\n\n（已停止生成）"
ABORTED_EMPTY = "（已停止生成）"

# ReAct 循环撞步数上限时追加的标记。运维排查里模型常常刚说完「现在实际
# 执行」就被掐断；不补这句话，用户看到的就像 AI 说到一半没声了，也不
# 知道发个「继续」就能接着排查。
STEP_LIMIT_SUFFIX = "\n\n（本轮排查步数已达上限，暂停在此；回复「继续」可接着排查）"

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

    # -- 作用域 -------------------------------------------------------------

    def _scope(self, stmt: Select, column) -> Select:
        """把查询限定在当前 owner 上。所有读操作都要经过这里。"""
        return stmt.where(column == self._owner_id)

    # -- 模型配置 -----------------------------------------------------------

    def require_chat_config(self, config_id: Optional[int] = None) -> ModelConfig:
        """确定本次回答用哪份对话模型配置。

        传了 ``config_id`` 就用指定的那份（必须是本人的、对话用途、且已通过
        连通性测试）；否则退回「设置」里的默认对话模型。
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
                    "请前往「设置」测试后再使用。",
                )
            return config

        config = self._db.scalar(
            select(ModelConfig).where(
                ModelConfig.owner_id == self._owner_id,
                ModelConfig.purpose == ModelPurpose.CHAT.value,
                ModelConfig.is_default.is_(True),
            )
        )
        if config is None or not config.last_test_ok:
            raise BusinessError(
                CODE_CHAT_NOT_READY,
                "尚未配置可用的对话模型。"
                "请前往「设置」添加对话模型并通过连通性测试。",
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
                message_count=int(counts.get(row.id, 0)),
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

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
    ) -> ChatConversation:
        if agent_id is not None:
            self._agents.get(agent_id)  # 校验可见性，不通过则抛 LookupError
        if project_id is not None:
            self._knowledge.get_project(project_id)  # 已按 owner 限定，抛 LookupError
        if model_config_id is not None:
            self.require_chat_config(model_config_id)

        row = ChatConversation(
            owner_id=self._owner_id,
            title=title[:255] or "新会话",
            agent_id=agent_id,
            project_id=project_id,
            type_ids=list(type_ids) if type_ids else None,
            model_config_id=model_config_id,
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
        self._db.commit()
        self._db.refresh(row)
        return row

    def delete_conversation(self, conversation_id: int) -> None:
        row = self.get_conversation(conversation_id)
        self._db.execute(
            delete(ChatMessage).where(
                ChatMessage.owner_id == self._owner_id,
                ChatMessage.conversation_id == row.id,
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

    # -- 运维问答会话 ----------------------------------------------------------

    def find_or_create_ops_conversation(
        self, target_type: str, target_id: int
    ) -> ChatConversation:
        """找到（或创建）绑定某个运维目标的问答会话，每个目标一个。

        运维页面的嵌入面板每次都走这里：目标是哪台机器/哪个库决定了会话用
        哪个人设（内置的 server-ops / db-ops）、挂哪种形态的工具箱。
        """
        if target_type == TARGET_SERVER:
            target = server_ops.OpsServerService(self._db, self._owner_id).get(target_id)
            slug = "server-ops"
        elif target_type == "database":
            target = db_ops.OpsDatabaseService(self._db, self._owner_id).get(target_id)
            slug = "db-ops"
        else:
            raise LookupError("运维目标类型不支持")

        row = self._db.scalar(
            self._scope(select(ChatConversation), ChatConversation.owner_id)
            .where(
                ChatConversation.ops_target_type == target_type,
                ChatConversation.ops_target_id == target_id,
            )
            .order_by(ChatConversation.id.desc())
        )
        if row is not None:
            return row

        agent = self._agents.require_by_slug(slug)
        row = ChatConversation(
            owner_id=self._owner_id,
            title=f"{target.name} 运维问答",
            agent_id=agent.id,
            ops_target_type=target_type,
            ops_target_id=target_id,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        return row

    def count_messages(self, conversation_id: int) -> int:
        return int(
            self._db.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.owner_id == self._owner_id,
                    ChatMessage.conversation_id == conversation_id,
                )
            )
            or 0
        )

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
        return [self.to_message(row) for row in rows]

    @staticmethod
    def to_message(row: ChatMessage) -> MessageItem:
        return MessageItem(
            id=row.id,
            role=row.role,
            content=row.content,
            citations=[Citation(**item) for item in (row.citations or [])],
            created_at=row.created_at,
        )

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        citations: Optional[List[Citation]] = None,
    ) -> ChatMessage:
        row = ChatMessage(
            owner_id=self._owner_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            citations=[c.model_dump() for c in citations] if citations else None,
        )
        self._db.add(row)

        # 顺手 touch 一下父记录，好让会话列表按最近活跃时间排序。
        conversation = self._db.get(ChatConversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = func.now()

        self._db.commit()
        self._db.refresh(row)
        return row

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

    def pop_dangling_user_message(self, conversation_id: int) -> str:
        """删掉会话末尾那条没得到回答的用户消息，返回它的内容。

        流式回答失败的遗留形态：路由先落库 user 消息再开始生成
        （routers/chat.py 的顺序），失败时 user 在、assistant 不在。
        重试由后端先把它删掉再走正常回答流程（会重新落一条），
        历史里就不会出现两条一模一样的提问。末尾已是 assistant 说明
        上次回答成功了，没有可重试的东西。
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
        content = row.content
        self._db.delete(row)
        self._db.commit()
        return content

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
        Auditor(self._db, self._owner_id, action.target_type).record(
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
        """最近的若干轮对话，按从旧到新排列。"""
        rows = list(
            self._db.scalars(
                self._scope(select(ChatMessage), ChatMessage.owner_id)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id.desc())
                .limit(HISTORY_TURNS * 2)
            ).all()
        )
        return list(reversed(rows))

    # -- 智能体选择 ---------------------------------------------------------

    def resolve_agent(self, agent_id: Optional[int]) -> Agent:
        if agent_id is not None:
            return self._agents.get(agent_id)

        for agent in self._agents.list(enabled_only=True):
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

    @staticmethod
    def _datetime_tool():
        """查询当前日期时间的工具。

        模型自己不知道「今天」是哪天。问题里一旦出现相对时间（今天、本周、
        最近），不论是作答还是组织检索词，都得先把它换算成具体日期。
        """
        from langchain_core.tools import tool

        @tool
        def current_datetime() -> str:
            """获取当前的日期和时间（北京时间）。

            问题涉及「今天」「现在」「本周」「最近」等相对时间时调用，先把
            相对时间换算成具体日期再继续。
            """
            now = datetime.now(_LOCAL_TZ)
            return f"{now:%Y-%m-%d %H:%M:%S} 星期{_WEEKDAYS[now.weekday()]}"

        return current_datetime

    def _replay(
        self, conversation_id: int, question: str, *, question_persisted: bool = True
    ) -> list:
        """把历史和当前问题拼成 ReAct 的初始消息列表。

        ``question_persisted`` 标记调用方是否已把这条问题落库：completions
        路由先落库再问（失败重试需要它），confirm 续答的合成上下文则刻意
        不落——两种情况历史里「末尾那条 user 消息」的含义正好相反。
        """
        from langchain_core.messages import AIMessage, HumanMessage

        history = self._history(conversation_id)
        # 刚存下的那个问题已经在历史里了，先丢掉，免得跟下面单独追加的问题
        # 重复一遍。
        if question_persisted and history and history[-1].role == ROLE_USER:
            history = history[:-1]

        messages: list = []
        for row in history:
            if row.role == ROLE_USER:
                messages.append(HumanMessage(content=row.content))
            elif row.role == ROLE_ASSISTANT:
                messages.append(AIMessage(content=row.content))
        messages.append(HumanMessage(content=question))
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
        tools: list = [self._datetime_tool()]
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

        client = build_chat_model(
            config,
            temperature=agent.temperature / 100,
            streaming=True,
        )
        # tools 里始终带着 current_datetime，所以对话模型必须支持
        # function calling——这是引入时间工具后不再保留的兼容路径。
        executor = create_agent(client, tools, system_prompt=system)

        parts: List[str] = []
        aborted = False
        step_limited = False
        sent_proposals = 0
        sent_execs = 0

        stream = executor.astream_events(
            {
                "messages": self._replay(
                    conversation_id, question, question_persisted=question_persisted
                )
            },
            config={
                "recursion_limit": OPS_REACT_STEPS if toolbox is not None else MAX_REACT_STEPS
            },
            version="v2",
        )
        try:
            async for event in stream:
                if is_disconnected is not None and await is_disconnected():
                    aborted = True
                    break

                kind = event.get("event")
                if kind == "on_tool_start":
                    payload = (event.get("data") or {}).get("input") or {}
                    yield "step", {
                        "tool": event.get("name") or "",
                        "query": str(payload.get("query", "")),
                        # 完整的工具入参：运维工具的展示文案要取 command/statement，
                        # 由前端按工具名挑字段。
                        "input": payload,
                    }
                elif kind == "on_tool_end":
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
                elif kind == "on_chat_model_stream":
                    text = _chunk_text((event.get("data") or {}).get("chunk"))
                    if text:
                        parts.append(text)
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

        # 存的就是流出去的全部文本。模型偶尔会在调用工具前先说一句「我查一下
        # 资料」，那句话也留着：它确实出现在了用户屏幕上，落库内容跟用户看到
        # 的对不上，才是更糟的事。
        answer = "".join(parts).strip()
        citations = registry.all()

        if aborted:
            # 把这段残缺的回答存下来，并打上标记。要是存完整文本，那么落库
            # 的会话内容就和用户当时看到的对不上了。
            answer = f"{answer}{ABORTED_SUFFIX}" if answer else ABORTED_EMPTY
        elif step_limited:
            # 标记同样先补流出去再落库：用户屏幕上看到的和存下来的保持一致，
            # 他也知道发条「继续」就能让模型接着排查。
            yield "token", STEP_LIMIT_SUFFIX
            answer = f"{answer}{STEP_LIMIT_SUFFIX}" if answer else STEP_LIMIT_SUFFIX.strip()
        elif not answer:
            answer = NO_CONTEXT_REPLY

        row = self.add_message(
            conversation_id,
            ROLE_ASSISTANT,
            answer,
            citations=citations or None,
        )

        # 回填提议对应的 assistant 消息：历史回放时确认卡片要挂在触发它的
        # 那条回答下面。
        if toolbox is not None and toolbox.pending_proposals:
            for action in toolbox.pending_proposals:
                action.message_id = row.id
            self._db.commit()

        # 一轮问答落库后的收尾（重算会话摘要、提取长期记忆）放到请求之外跑：
        # SSE 流一结束请求级 session 就关了，后台任务自己开 session。它失败
        # 静默，且摘要全量重算——这一次错过的，下一次回答会补回来。
        fire_and_forget(after_answer_task(self._owner_id, conversation_id, HISTORY_TURNS))

        logger.debug(
            "answer for conversation %s saved as message %s (%d chars, %d citation(s), aborted=%s)",
            conversation_id,
            row.id,
            len(answer),
            len(citations),
            aborted,
        )

        if aborted:
            # 不发 "done"：客户端已经走了。这里的意义就在于把内容存下来。
            return

        yield "done", {"message_id": row.id, "conversation_id": conversation_id}


def derive_title(question: str) -> str:
    """第一个问题顺带充当会话标题。"""
    title = " ".join(question.split())
    return title[:40] if len(title) <= 40 else f"{title[:40]}…"
