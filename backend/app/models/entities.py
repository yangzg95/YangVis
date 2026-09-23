"""SQLAlchemy ORM 实体定义。

覆盖自建认证（P0）、模型配置与智能体（P1）以及知识库（P2）。聊天相关的表
随 P3 一起落地。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# SQLite 没有自增的 BIGINT，所以在 SQLite 上退回用 INTEGER；
# MySQL 上仍然是 BIGINT AUTO_INCREMENT。
_PK = BigInteger().with_variant(Integer, "sqlite")

# MySQL 的 TEXT 上限只有 64 KB，单个上传的文档很容易就超过这个大小。
_LONG_TEXT = Text().with_variant(MEDIUMTEXT, "mysql")


class SysUser(Base):
    """本地账号。用于替代上游平台的身份体系（见 §7.1）。"""

    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 运维写通行证：能对共享台账资产产生变更（AI 写确认、PTY、文件写、控制台
    # 写语句）的开关。闸门判定是 is_admin or ops_write（管理员是天然超集）。
    ops_write: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SysUser id={self.id} username={self.username!r}>"


class ModelConfig(Base):
    """某个用户连接某个模型服务商所需的信息。

    按用户隔离（``owner_id``），并按 ``purpose`` 拆分，这样聊天模型和 embedding
    模型可以各自独立配置。
    """

    __tablename__ = "model_config"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)

    # Fernet 加密后的密文；任何 API 响应里都不会暴露。
    api_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary(1024), nullable=True)

    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 通过实际探测服务商测出来的，而不是写死：Qdrant collection 会按这个维度
    # 创建，一旦建好就没法再改。
    vector_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 记录保存的这套凭据是否真的验证通过过。
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_test_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ModelConfig id={self.id} owner={self.owner_id} purpose={self.purpose!r}>"


class Agent(Base):
    """可供选择的助手人设。

    内置智能体以 ``owner_id = 0`` 预置，所有人都能看到但谁都改不了；用户自建的
    智能体则挂在真实的 owner 上。
    """

    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uk_owner_slug"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, default=0)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)

    # 回答是否必须基于从知识库里检索到的 chunk。
    use_knowledge: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # 是否给主对话挂运维只读工具（服务器只读命令 / 数据库只读查询）。
    # 写操作不在此列：写确认要同进程等用户点按钮，SSE 通道做不到，
    # 所以这里只读是硬约束（见 services/chat_ops.py）。
    use_ops: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 是否在回答时注入该用户的长期记忆（user_memory），并在回答后从对话里
    # 提取新记忆。记忆属于用户而不属于智能体，这里只是「这个人设要不要用」。
    use_memory: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 是否出现在主对话的智能体选择器里。功能自带的人设（运维专家、简历分析这类
    # 由对应模块在后台调用的）不在对话里展示，但停用它们不影响后台调用。
    chat_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    temperature: Mapped[int] = mapped_column(Integer, nullable=False, default=30)  # 0-100
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Agent id={self.id} slug={self.slug!r} builtin={self.is_builtin}>"


class KbProject(Base):
    """知识库顶层：项目 → 类型 → 文档。

    项目是聊天时选择知识库的边界：用户先选项目，然后才能选该项目下的知识类型。
    这样两个项目里可以有相同名字的类型，但文档永远不会互相串。
    """

    __tablename__ = "kb_project"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uk_project_owner_name"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KbProject id={self.id} owner={self.owner_id} name={self.name!r}>"


class KnowledgeType(Base):
    """用户定义的知识分类，属于某个项目，用来对文档分组。"""

    __tablename__ = "knowledge_type"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # 默认给 0 是为了让这个列在已有表上新增时不需要重写整张表；
    # bootstrap.migrate_schema() 会把每一行补上真实的 project_id，补完之后
    # 0 不会再出现。
    project_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), default=0
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("idx_type_owner_project", "owner_id", "project_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KnowledgeType id={self.id} owner={self.owner_id} name={self.name!r}>"


class KbDocument(Base):
    """上传的文档及其提取出的文本。

    ``content`` 有意持久保留而不是索引完就丢：切换 embedding 模型时需要全量
    重新索引，如果没有在手的文本数据，唯一的办法就是让用户重新上传所有文档。
    """

    __tablename__ = "kb_document"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # 给未来的全组织知识库预留的；现在不产生任何成本，而且能保证改动只局限在
    # service 层。
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="private")

    type_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content: Mapped[str] = mapped_column(_LONG_TEXT, nullable=False)

    # pending（待处理）| indexing（索引中）| ready（就绪）| error（出错）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("idx_doc_owner_type", "owner_id", "type_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KbDocument id={self.id} owner={self.owner_id} status={self.status!r}>"


class KbChunk(Base):
    """文档的一个切片，在 Qdrant 里对应一个 point。"""

    __tablename__ = "kb_chunk"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    doc_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Qdrant point id（UUID 字符串），存下来是为了能单独从向量库里删除某个
    # chunk，不需要重新扫描整个 collection。
    point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_chunk_owner_doc", "owner_id", "doc_id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KbChunk id={self.id} doc={self.doc_id} seq={self.seq}>"


class KbIndexState(Base):
    """记录用户当前的 Qdrant collection 是用哪个 embedding 模型创建的。

    Qdrant collection 在创建时固定了向量维度。更糟的是，用一个*不一样但维度
    相同*的模型去查，不会报任何错——cosine 分数照算，但结果全是噪声。这一行
    就是用来发现这种不匹配的。
    """

    __tablename__ = "kb_index_state"

    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    collection_name: Mapped[str] = mapped_column(String(64), nullable=False)
    embed_model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # base_url + model_name 的指纹；每次搜索前会拿它做比对。
    embed_model_sig: Mapped[str] = mapped_column(String(256), nullable=False, default="")

    vector_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # uninitialized（未初始化）| indexing（索引中）| ready（就绪）
    # | rebuilding（重建中）| error（出错）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="uninitialized")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<KbIndexState owner={self.owner_id} status={self.status!r}>"


class ChatConversation(Base):
    """用户与某个智能体之间的一轮会话线程。"""

    __tablename__ = "chat_conversation"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="新会话")

    # 当前使用的智能体。保持可空，这样删掉一个智能体不会把会话历史一起带走。
    agent_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 检索范围，持久化下来是为了重新打开会话时能还原出当初回答所依据的那个
    # 项目和那批知识类型。
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 一组 knowledge_type id；为 null 表示「该项目下的全部类型」。
    type_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # 本次会话选用的对话模型配置；为 null 表示跟随「设置」里的默认对话模型。
    model_config_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 绑定的运维目标（server | database + ops_server/ops_database 的 id）。
    # 为 null 表示普通对话；有值表示这是从运维页发起的问答会话，工具箱绑定到
    # 这一个目标上，且不出现在主对话的会话列表里。
    ops_target_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ops_target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChatConversation id={self.id} owner={self.owner_id}>"


class ChatMessage(Base):
    """单条消息。助手消息会带上生成这条回答所引用的来源。"""

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(_LONG_TEXT, nullable=False)

    # [{doc_id, chunk_id, filename, score}] —— 回答里 [1][2] 这些编号标记指向的
    # 内容。存下来是为了重新打开会话时依然能看到每段回答的出处。
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_msg_conv", "conversation_id", "id"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChatMessage id={self.id} conv={self.conversation_id} role={self.role!r}>"


class ChatSummary(Base):
    """一个会话「最近几轮之前」那部分历史的滚动摘要。

    回放窗口（services/chat.py 的 HISTORY_TURNS）之外的消息对模型不可见，
    摘要就是它们留在上下文里的唯一形式。摘要是*全量重算*出来的——不存
    「摘要到第几条消息」的 checkpoint，这样历史消息被手动改写后，下一次
    重算自然吸收，任何 worker 也都能随时重算，无需跨进程状态。
    """

    __tablename__ = "chat_summary"

    # 一个会话至多一行摘要，主键即 conversation_id。
    conversation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChatSummary conv={self.conversation_id} chars={len(self.summary)}>"


class UserMemory(Base):
    """一条关于用户的长期记忆，跨会话生效。

    由后台任务从对话里提取（用户也可在设置页手动增删改），回答时注入到
    开了 ``use_memory`` 的智能体的 system prompt 里。一条记忆就是一句话，
    刻意不做结构化分类：分类体系的维护成本远超它对注入效果的贡献。
    """

    __tablename__ = "user_memory"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    content: Mapped[str] = mapped_column(String(512), nullable=False)

    # 提取出这条记忆的会话，仅用于溯源；会话删除后置空语义靠不删记忆体现——
    # 记忆属于用户，不随产生它的会话一起消失。
    source_conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserMemory id={self.id} owner={self.owner_id}>"


class OpsServer(Base):
    """一台可被运维的服务器及其 SSH 凭据。"""

    __tablename__ = "ops_server"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uk_server_owner_name"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=22)
    username: Mapped[str] = mapped_column(String(64), nullable=False)

    # password（口令）| key（私钥）
    auth_type: Mapped[str] = mapped_column(String(16), nullable=False, default="password")

    # 三个凭据字段都是 Fernet 密文，任何 API 响应里都只出现掩码。
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary(2048), nullable=True)
    private_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary(8192), nullable=True)
    passphrase_enc: Mapped[bytes | None] = mapped_column(LargeBinary(1024), nullable=True)

    # 首次连接时记下的主机公钥指纹（TOFU）。之后每次连接都比对它：
    # 用 known_hosts=None 等于关掉中间人防护，而绝大多数用户并没有现成的
    # known_hosts 可用，TOFU 是这两者之间唯一站得住的折中。
    host_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_check_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpsServer id={self.id} owner={self.owner_id} host={self.host!r}>"


class OpsDatabase(Base):
    """一个可被查询的数据库实例。

    默认只读；``writable`` 打开后控制台（人手敲的那条通道）可以写，
    AI 通道始终是只读的，与这个开关无关。
    """

    __tablename__ = "ops_database"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uk_database_owner_name"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)

    # mysql（含 MariaDB）| redis
    db_type: Mapped[str] = mapped_column(String(16), nullable=False, default="mysql")

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=3306)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary(2048), nullable=True)

    # MySQL 是库名，Redis 是编号（"0"）。类型不同但语义都是「默认落在哪」。
    db_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 控制台写开关：False 时所有通道只读；True 时手敲通道可写（DML/DDL），
    # 元数据浏览和 AI 通道不受影响。
    writable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # 台账共享的展示色（Navicat 式整行底色，树节点和页签头一起染），#rrggbb；None 不染。
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_check_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_check_error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpsDatabase id={self.id} owner={self.owner_id} type={self.db_type!r}>"


class OpsSqlFavorite(Base):
    """用户收藏的一条 SQL / Redis 命令。

    ``database_id`` 为空表示「通用收藏」，在任何连接的查询页里都能用；
    绑定了连接的收藏只在该连接下出现。内容本身不做只读校验——收藏只是
    一段文本，真正的安全边界仍在执行时的只读网关。
    """

    __tablename__ = "ops_sql_favorite"
    __table_args__ = (Index("idx_favorite_owner_db", "owner_id", "database_id"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # 收藏绑定的连接；None 表示所有连接通用的收藏。
    database_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    title: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpsSqlFavorite id={self.id} owner={self.owner_id} title={self.title!r}>"


class OpsAuditLog(Base):
    """运维操作留痕。

    这张表是「AI 可以操作服务器」这件事的凭证：AI 到底跑过什么、哪些是用户
    点头之后才跑的，事后必须查得到。没有它，安全边界就只是一句口头承诺。
    """

    __tablename__ = "ops_audit_log"
    __table_args__ = (Index("idx_audit_owner_time", "owner_id", "created_at"),)

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # server | database
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # user（用户在终端里手敲）| ai（模型通过工具发起）
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default="user")

    command: Mapped[str] = mapped_column(_LONG_TEXT, nullable=False)

    # readonly（白名单内直接执行）| write（连接开了写开关的手敲控制台执行）
    # | confirmed（用户确认后执行）| rejected（用户拒绝）
    # | forbidden（判定为危险，直接挡下）
    # | manual（人在 PTY 终端里手敲的；success 只表示「已送达」，拿不到退出码）
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, default="readonly")

    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 关联的待确认项（ops_pending_action.id）；手动执行与只读执行为 null。
    # 输出不在这里重复存——确认通道的执行结果在 ops_pending_action.result。
    pending_action_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpsAuditLog id={self.id} actor={self.actor!r} verdict={self.verdict!r}>"


class OpsPendingAction(Base):
    """AI 提议的一条待用户确认的运维写命令。

    写确认走两阶段落库而不是在请求里干等：propose 时落一行 pending 并把回答
    正常收尾，用户点确认后由 confirm 请求自己执行并把结果写回本行。这样确认
    与执行永远发生在同一个 worker 上，gunicorn 多进程也就不再是问题；而且
    刷新页面后待确认卡片还能从表里恢复——内存方案在 WS 断开时全丢。
    """

    __tablename__ = "ops_pending_action"
    __table_args__ = (
        Index("idx_action_conv", "conversation_id", "id"),
        Index("idx_action_owner_status", "owner_id", "status"),
    )

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 触发该提议的那条 assistant 消息；propose 发生在流式回答中途，只能等回答
    # 落库后回填，所以可空。历史回放靠它把确认卡片挂到对应气泡下面。
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # server | database（本期只有 server 的写命令走确认）
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    command: Mapped[str] = mapped_column(_LONG_TEXT, nullable=False)
    # AI 给用户看的中文理由：为什么要执行这条命令。
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # pending --确认--> approved --执行完--> executed | failed
    # pending --拒绝--> rejected；pending 超过确认时限未应答，读取时惰性
    # 视为 expired（启动时也会把进程重启遗留的 pending 物化成 expired）。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    # 执行输出（截断后）：续答要把结果喂回模型，历史回放要展示，都从这里取。
    result: Mapped[str | None] = mapped_column(_LONG_TEXT, nullable=True)
    exit_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<OpsPendingAction id={self.id} status={self.status!r}>"


class Resume(Base):
    """一份上传的简历及其 AI 分析结果。

    ``content`` 持久保留提取出的全文：重新分析和简历对比都要用到原文，
    如果只存上传时的临时文件，每次分析都得重新解析一遍 PDF/DOCX。
    """

    __tablename__ = "resume"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content: Mapped[str] = mapped_column(_LONG_TEXT, nullable=False)

    # uploaded（已上传，未分析）| analyzing（分析中）| ready（分析完成）| error（分析失败）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="uploaded")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 原文件在用户百度网盘里的位置。NULL 表示未同步——用户没绑定网盘，
    # 或上传时同步失败（同步失败不阻塞简历保存）。
    netdisk_fs_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    netdisk_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # markdown 分析报告与结构化改进意见，分开存是为了让「改进意见」能单独
    # 用列表样式呈现，而不是埋在报告正文里。
    report: Mapped[str | None] = mapped_column(_LONG_TEXT, nullable=True)
    suggestions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Resume id={self.id} owner={self.owner_id} status={self.status!r}>"


class ResumeComparison(Base):
    """一次多简历对比的结果。

    ``resume_ids`` 只是当时的快照：之后删掉某份简历，这份历史对比报告
    依然成立、可以回看。
    """

    __tablename__ = "resume_comparison"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resume_ids: Mapped[list] = mapped_column(JSON, nullable=False)

    # analyzing（分析中）| ready（完成）| error（失败）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="analyzing")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)
    report: Mapped[str | None] = mapped_column(_LONG_TEXT, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ResumeComparison id={self.id} owner={self.owner_id} status={self.status!r}>"


class ResumeToolkitTask(Base):
    """一次「求职助手」生成任务（简历优化 / 匹配审计 / 面试准备等 7 类）。

    ``inputs`` 是提交时的入参快照：简历来源存的是 id 与标题，原文不进快照——
    原文体量大且生成时已经直接喂给模型了，历史记录只需要能看懂「当时用了哪份」。
    """

    __tablename__ = "resume_toolkit_task"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    # optimize | career-match | jd-match | interview-prep | portfolio-plan |
    # salary-negotiation（合法值见 services/resume.py 的 TOOLKIT_KINDS）
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    inputs: Mapped[dict] = mapped_column(JSON, nullable=False)

    # analyzing（生成中）| ready（完成）| error（失败）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="analyzing")
    error_msg: Mapped[str | None] = mapped_column(String(512), nullable=True)
    report: Mapped[str | None] = mapped_column(_LONG_TEXT, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ResumeToolkitTask id={self.id} owner={self.owner_id} kind={self.kind!r} status={self.status!r}>"


class InterviewRecord(Base):
    """一场面试的记录及其问题清单。

    ``questions`` 是整个 JSON 子列表而不是子表：一场面试的题目量级是几十条，
    没有跨记录检索题目的需求，独立子表只会带来 join 和级联的复杂度。每题带一
    个服务端生成的 ``qid``，题目级的增删改和 AI 参考答案回写都按 qid 寻址，
    不依赖数组下标——下标在「删题的同时另一题正在生成答案」时会发生漂移。

    每题的 ``ref_status``：none（未生成）| analyzing（生成中）| ready（已生成）
    | error（生成失败，原因在 ref_error）。
    """

    __tablename__ = "interview_record"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    company: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[str] = mapped_column(String(128), nullable=False)
    interview_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 一面/二面/HR面 之类，自由文本：各公司的轮次叫法没有标准可枚举。
    round: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # pending（待定）| passed（通过）| failed（未通过）| offer（已 offer）
    result: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 整场面试的复盘备注。
    notes: Mapped[str | None] = mapped_column(_LONG_TEXT, nullable=True)

    # [{qid, question, my_answer, note, ref_answer, ref_status, ref_error}]
    questions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InterviewRecord id={self.id} owner={self.owner_id} company={self.company!r}>"


class NetdiskAccount(Base):
    """一个用户绑定的百度网盘账号（一人一绑）。

    两个 token 都是 Fernet 密文，与 :class:`ModelConfig` 的 api_key 同一约定：
    任何 API 响应里都不会出现 token 本体。
    """

    __tablename__ = "netdisk_account"

    id: Mapped[int] = mapped_column(_PK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)

    baidu_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    baidu_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary(2048), nullable=True)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary(2048), nullable=True)
    # access_token 的到期时刻（naive UTC）；临期时用 refresh_token 自动换新。
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<NetdiskAccount owner={self.owner_id} baidu={self.baidu_name!r}>"
