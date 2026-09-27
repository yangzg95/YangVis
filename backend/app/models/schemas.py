"""Pydantic 请求 / 响应模式定义。"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# 有些字段确实以 "model_" 开头（如 model_name、model_mismatch）。
# Pydantic 默认保留了该前缀，遇到时会报警告，因此在需要用到它的
# 模式上解除这个命名空间限制。
_ALLOW_MODEL_PREFIX = ConfigDict(protected_namespaces=())


class APIResponse(BaseModel, Generic[T]):
    """统一的 API 响应外壳。"""

    code: int = 0
    message: str = "success"
    data: Optional[T] = None


# ---- 认证 -------------------------------------------------------------------

class LoginRequest(BaseModel):
    """SPA 提交的用户名 / 密码 / 图形验证码登录 payload。"""

    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=128)
    # Fernet 令牌，几位的 payload 加密后约 100+ 字符。
    captcha_id: str = Field(..., min_length=1, max_length=512)
    captcha_code: str = Field(..., min_length=1, max_length=8)


class CaptchaResult(BaseModel):
    """一张新签发的图形验证码；image 是 data URI，可直接喂给 <img>。"""

    captcha_id: str
    image: str


class UserInfo(BaseModel):
    """当前用户，由上游认证服务返回（camelCase 命名）。

    这里的别名只在校验时生效，这样响应仍然保持 snake_case，与本 API 的
    其余部分一致。
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    user_id: Optional[int] = Field(default=None, validation_alias="userId")
    username: Optional[str] = None
    nickname: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    project_id: Optional[int] = Field(default=None, validation_alias="projectId")
    user_terminal: Optional[str] = Field(default=None, validation_alias="userTerminal")
    roles: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    ops_write: bool = False


class LoginResult(BaseModel):
    """登录成功后返回给 SPA 的结果。"""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    access_token: str = Field(..., validation_alias="accessToken")
    user_info: Optional[UserInfo] = Field(default=None, validation_alias="userInfo")


# ---- 知识库 --------------------------------------------------------------

class KbProjectBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class KbProjectCreate(KbProjectBase):
    pass


class KbProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class KbProject(KbProjectBase):
    id: int
    type_count: int = 0
    document_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class KnowledgeTypeBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class KnowledgeTypeCreate(KnowledgeTypeBase):
    project_id: int = Field(..., ge=1)


class KnowledgeTypeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class KnowledgeType(KnowledgeTypeBase):
    id: int
    project_id: int
    document_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentStatus(str, Enum):
    """一份文档从索引到就绪的生命周期。"""

    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"


class KnowledgeDocument(BaseModel):
    """SPA 中展示的一行文档记录。

    ``name`` 与 ``filename`` 内容一致；前端从 mock 实现时期就一直用这个键名，
    改名并不能带来任何好处。
    """

    id: int
    name: str
    type_id: Optional[int] = None
    size: int = 0
    mime: Optional[str] = None
    status: DocumentStatus = DocumentStatus.PENDING
    error_msg: Optional[str] = None
    chunk_count: int = 0
    created_at: datetime
    updated_at: datetime


class IndexState(BaseModel):
    """调用方的 Qdrant collection 状态。"""

    model_config = _ALLOW_MODEL_PREFIX

    status: str = "uninitialized"
    collection_name: Optional[str] = None
    vector_size: int = 0
    doc_count: int = 0
    chunk_count: int = 0
    error_msg: Optional[str] = None

    # 当前配置的 embedding 模型与建索引时所用的模型不一致时为 True。
    # 这种状态下检索出来的结果看着像模像样，其实是一堆胡言乱语，
    # 所以宁可显式暴露出来，也不能默默容忍。
    model_mismatch: bool = False
    updated_at: Optional[datetime] = None


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)

    # 检索范围限定在一个项目内；``type_ids`` 进一步缩小范围，
    # 不传则搜索该项目下的所有类型。
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None

    score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    chunk_id: int
    doc_id: int
    filename: str
    seq: int
    score: float
    content: str


class SearchResponse(BaseModel):
    hits: List[SearchHit] = Field(default_factory=list)
    total: int = 0


class RebuildResult(BaseModel):
    """触发一次重建索引后的返回结果。"""

    started: bool
    message: str
    doc_count: int = 0


class DocBatchPayload(BaseModel):
    """文档批量操作（删除/重建索引）的 id 列表。"""

    ids: List[int] = Field(..., min_length=1, max_length=500)


class DocBatchResult(BaseModel):
    """批量操作结果：请求了多少、成了多少（查无此文档的跳过）。"""

    requested: int
    done: int


# ---- 对话 -------------------------------------------------------------------

class ConversationCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    title: str = Field(default="新会话", max_length=255)
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None


class ConversationUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None


class ConversationItem(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    id: int
    title: str
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    """回答中某个编号 [n] 标记所对应的一条来源。"""

    index: int
    doc_id: int
    chunk_id: int
    filename: str
    score: float
    excerpt: str = ""


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    citations: List[Citation] = Field(default_factory=list)
    created_at: datetime


class MessageUpdate(BaseModel):
    """手动修正一条助手消息的内容。

    目前的唯一用途是「改图」：用户在前端编辑助手生成的 Mermaid 代码后整体
    回写。历史是回放给模型的，所以改写落库之后，下一轮提问看到的就是改后
    的版本——这是「AI 能感知手动修改」的全部机制。
    """

    content: str = Field(..., min_length=1, max_length=20000)


class CompletionRequest(BaseModel):
    """提问请求。回答通过 SSE 流式返回，而不是走这个响应外壳。"""

    model_config = _ALLOW_MODEL_PREFIX

    # 上限对齐手动改消息（上面 MessageUpdate.content）的 20000：报告页
    # 「发起对话」会把整份报告（含改进意见）作为首条消息发出；库字段是
    # _LONG_TEXT，没有更紧的约束。
    message: str = Field(..., min_length=1, max_length=20000)
    conversation_id: Optional[int] = None
    agent_id: Optional[int] = None

    # 基于知识库的智能体的检索范围。在多轮对话的后续提问中，
    # 不传该值就沿用会话本身已经带着的范围。
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None

    # 想用哪份对话模型配置回答。不传则沿用会话记住的那份，会话也没记住
    # 就用「设置」里的默认对话模型。
    model_config_id: Optional[int] = None


class OpsConversationRequest(BaseModel):
    """为某个运维目标新开一个问答会话。title 缺省时用「目标名 + 运维问答」兜底。"""

    model_config = _ALLOW_MODEL_PREFIX

    target_type: str = Field(..., pattern="^(server|database)$")
    target_id: int
    title: Optional[str] = None


class OpsActionItem(BaseModel):
    """一条运维写命令待确认项。``status`` 是换算后的展示状态：超时的
    pending 直接报 expired，而不是等后台任务物化。"""

    id: int
    conversation_id: int
    message_id: Optional[int] = None
    target_type: str
    target_id: int
    target_name: Optional[str] = None
    command: str
    reason: Optional[str] = None
    status: str
    result: Optional[str] = None
    exit_status: Optional[int] = None
    timeout_seconds: int
    created_at: datetime
    resolved_at: Optional[datetime] = None


# ---- 设置 / 模型配置 ------------------------------------------------

class ModelPurpose(str, Enum):
    """对话模型和 embedding 模型是分开独立配置的。"""

    CHAT = "chat"
    EMBEDDING = "embedding"


class ModelConfigCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    purpose: ModelPurpose
    title: str = Field(..., min_length=1, max_length=128)
    model_name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(default="", max_length=512)
    remark: Optional[str] = Field(default=None, max_length=512)


class ModelConfigUpdate(BaseModel):
    """部分更新；只写入传进来的那些字段。

    ``api_key`` 未传、为空或者是掩码值时，已存储的凭据保持不变
    —— 参见 ``crypto.is_masked``。
    """

    model_config = _ALLOW_MODEL_PREFIX

    purpose: Optional[ModelPurpose] = None
    title: Optional[str] = Field(default=None, max_length=128)
    model_name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=512)
    remark: Optional[str] = Field(default=None, max_length=512)


class ModelConfigItem(BaseModel):
    """返回给客户端的一条配置，其中凭据已做掩码处理。"""

    model_config = _ALLOW_MODEL_PREFIX

    id: int
    purpose: ModelPurpose
    title: str
    model_name: str
    base_url: str
    api_key: str
    api_key_error: Optional[str] = None
    remark: Optional[str] = None
    is_default: bool = False
    vector_size: Optional[int] = None
    last_tested_at: Optional[datetime] = None
    last_test_ok: bool = False
    last_test_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class TestResult(BaseModel):
    """一次连通性探测的结果。"""

    success: bool
    message: str
    vector_size: Optional[int] = None


class PurposeStatus(BaseModel):
    """某一种用途是否已经配置了可用的默认模型。"""

    model_config = _ALLOW_MODEL_PREFIX

    ready: bool = False
    model: Optional[str] = None
    config_id: Optional[int] = None


class KnowledgeStatus(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    status: str = "uninitialized"
    doc_count: int = 0
    chunk_count: int = 0
    model_mismatch: bool = False


class ReadinessStatus(BaseModel):
    """驱动设计文档里描述的那道前端准入门槛。"""

    chat: PurposeStatus = Field(default_factory=PurposeStatus)
    embedding: PurposeStatus = Field(default_factory=PurposeStatus)
    kb: KnowledgeStatus = Field(default_factory=KnowledgeStatus)


# ---- 智能体 -----------------------------------------------------------------

class AgentCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    system_prompt: str = Field(..., min_length=1, max_length=8000)
    use_knowledge: bool = True
    use_ops: bool = False
    use_memory: bool = False
    chat_visible: bool = True
    temperature: int = Field(default=30, ge=0, le=100)
    sort_order: int = Field(default=100, ge=0, le=9999)


class AgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    system_prompt: Optional[str] = Field(default=None, max_length=8000)
    use_knowledge: Optional[bool] = None
    use_ops: Optional[bool] = None
    use_memory: Optional[bool] = None
    chat_visible: Optional[bool] = None
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
    enabled: Optional[bool] = None
    sort_order: Optional[int] = Field(default=None, ge=0, le=9999)


class AgentItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str] = None
    system_prompt: str
    use_knowledge: bool
    use_ops: bool
    use_memory: bool
    chat_visible: bool
    temperature: int
    is_builtin: bool
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


# ---- 长期记忆 ---------------------------------------------------------------

class MemoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    content: str
    source_conversation_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=512)


class MemoryUpdate(BaseModel):
    content: str = Field(..., min_length=1, max_length=512)

class ListResponse(BaseModel, Generic[T]):
    """分页接口返回的列表包装。"""

    items: List[T]
    total: int

# ---- 自建认证 -------------------------------------------------------------

class CurrentUser(BaseModel):
    """通过校验的调用方，由 ``require_user`` dependency 注入。"""

    user_id: int
    username: str
    nickname: Optional[str] = None
    is_admin: bool = False
    ops_write: bool = False

    @property
    def can_ops_write(self) -> bool:
        """运维写闸门判定：管理员是天然超集，否则看 ops_write 位标记。"""
        return self.is_admin or self.ops_write


class PasswordChangeRequest(BaseModel):
    """用户自助修改密码；仍然必须提供原密码。"""

    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


class UserCreateRequest(BaseModel):
    """仅限管理员的账号创建。不提供公开注册。"""

    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    nickname: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=128)
    is_admin: bool = False
    ops_write: bool = False


class UserUpdateRequest(BaseModel):
    """仅限管理员的资料修改。

    三个字段都是可选的：只有显式传过来的才会被写回，因此改昵称不会顺手把
    邮箱清空。用户名不在其中——它是登录凭据，改名等同于换一个账号。
    """

    nickname: Optional[str] = Field(default=None, max_length=64)
    email: Optional[str] = Field(default=None, max_length=128)
    is_admin: Optional[bool] = None
    ops_write: Optional[bool] = None


class UserStatusRequest(BaseModel):
    """启用 / 禁用一个账号。"""

    status: bool


class PasswordResetRequest(BaseModel):
    """管理员发起的密码重置；无需提供原密码。"""

    new_password: str = Field(..., min_length=8, max_length=256)


class UserItem(BaseModel):
    """管理员用户列表中的一行。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    nickname: Optional[str] = None
    email: Optional[str] = None
    status: bool
    is_admin: bool
    ops_write: bool = False
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ---- 智能运维 ---------------------------------------------------------------

class ServerAuthType(str, Enum):
    """SSH 认证方式。"""

    PASSWORD = "password"
    KEY = "key"


class DatabaseType(str, Enum):
    """支持的数据库类型。MariaDB 走 MySQL 协议，不单列。"""

    MYSQL = "mysql"
    REDIS = "redis"


class OpsServerCreate(BaseModel):
    """注册一台服务器。"""

    name: str = Field(..., min_length=1, max_length=128)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=64)
    auth_type: ServerAuthType = ServerAuthType.PASSWORD
    password: Optional[str] = Field(default=None, max_length=512)
    private_key: Optional[str] = Field(default=None, max_length=16000)
    passphrase: Optional[str] = Field(default=None, max_length=256)
    remark: Optional[str] = Field(default=None, max_length=512)


class OpsServerUpdate(BaseModel):
    """修改服务器信息。

    凭据字段留空表示「保持原样」——同 ``ModelConfigUpdate``，把掩码原样写回
    会在用户只改备注时悄悄清掉一把能用的密码。
    """

    name: Optional[str] = Field(default=None, max_length=128)
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=64)
    auth_type: Optional[ServerAuthType] = None
    password: Optional[str] = Field(default=None, max_length=512)
    private_key: Optional[str] = Field(default=None, max_length=16000)
    passphrase: Optional[str] = Field(default=None, max_length=256)
    remark: Optional[str] = Field(default=None, max_length=512)


class OpsServerItem(BaseModel):
    """服务器列表中的一行。凭据只以掩码出现。"""

    id: int
    name: str
    host: str
    port: int
    username: str
    auth_type: ServerAuthType
    credential: str = ""
    credential_error: Optional[str] = None
    host_key_pinned: bool = False
    remark: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_check_ok: bool = False
    last_check_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class OpsDatabaseCreate(BaseModel):
    """注册一个数据库实例。"""

    name: str = Field(..., min_length=1, max_length=128)
    db_type: DatabaseType = DatabaseType.MYSQL
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=64)
    password: Optional[str] = Field(default=None, max_length=512)
    db_name: Optional[str] = Field(default=None, max_length=128)
    writable: bool = False
    # 展示色：#rrggbb；None 不染。
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    remark: Optional[str] = Field(default=None, max_length=512)


class OpsDatabaseUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    db_type: Optional[DatabaseType] = None
    host: Optional[str] = Field(default=None, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=64)
    password: Optional[str] = Field(default=None, max_length=512)
    db_name: Optional[str] = Field(default=None, max_length=128)
    writable: Optional[bool] = None
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    remark: Optional[str] = Field(default=None, max_length=512)


class OpsDatabaseItem(BaseModel):
    id: int
    name: str
    db_type: DatabaseType
    host: str
    port: int
    username: Optional[str] = None
    password: str = ""
    password_error: Optional[str] = None
    db_name: Optional[str] = None
    writable: bool = False
    color: Optional[str] = None
    remark: Optional[str] = None
    last_checked_at: Optional[datetime] = None
    last_check_ok: bool = False
    last_check_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SqlFavoriteCreate(BaseModel):
    """收藏一条 SQL / Redis 命令。``database_id`` 为空表示所有连接通用。"""

    title: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=8000)
    database_id: Optional[int] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class SqlFavoriteUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    content: Optional[str] = Field(default=None, min_length=1, max_length=8000)
    database_id: Optional[int] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class SqlFavoriteItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    database_id: Optional[int] = None
    title: str
    content: str
    remark: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DbExecuteRequest(BaseModel):
    """在数据库控制台里手敲的一条命令。

    ``schema`` 是本次执行的默认库（MySQL 的 ``USE``），缺省回落到台账里
    配置的默认库名；它只作为连接参数传给驱动，不参与 SQL 拼接。

    字段名带下划线后缀是因为 ``schema`` 会遮蔽 ``BaseModel.schema``；
    对外契约不变，入参仍叫 ``schema``。
    """

    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(..., min_length=1, max_length=8000)
    schema_: Optional[str] = Field(default=None, max_length=128, validation_alias="schema")


class DbExecuteResult(BaseModel):
    """一条数据库命令的执行结果。

    ``columns`` 为空表示这次返回的不是结果集（例如 Redis 的标量回复），
    此时读 ``text``。
    """

    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    text: Optional[str] = None
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0


class DbBatchRequest(BaseModel):
    """批量执行的一段脚本（Navicat 式：整段 SQL 逐句执行）。

    ``command`` 上限比单语句宽——脚本天然更长；审计字段只存前 8000
    字符，不受影响。``schema`` 语义与 :class:`DbExecuteRequest` 相同。
    """

    model_config = ConfigDict(populate_by_name=True)

    command: str = Field(..., min_length=1, max_length=20000)
    schema_: Optional[str] = Field(default=None, max_length=128, validation_alias="schema")


class DbBatchStatement(BaseModel):
    """批量执行里单条语句的结果。

    ``sql`` 只是预览（截断 200 字符）——消息列表里一眼认出是哪条，
    完整脚本在审计记录里。``columns`` 为空时读 ``text``（与
    :class:`DbExecuteResult` 同约定）。
    """

    index: int  # 1-based，消息列表 / 结果页签 / 摘要按它对齐
    sql: str
    status: Literal["ok", "error"]
    message: str  # 成功："N 行" / "执行完成"；失败：错误原因
    elapsed_ms: int = 0
    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    text: Optional[str] = None


class DbBatchResult(BaseModel):
    """一段脚本的批量执行结果。单句出错不中断，逐句成败都在这里。"""

    statements: List[DbBatchStatement]
    total: int
    succeeded: int
    failed: int
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int = 0


# ---- 数据库浏览（Navicat 式界面的数据源，全部只读） ---------------------------


class DbSchemaItem(BaseModel):
    """连接展开后的一层：MySQL 的 schema，或 Redis 的一个逻辑库。"""

    name: str
    kind: str  # "schema" | "redisdb"
    # MySQL 是表数量，Redis 是 key 数量；拿不到时为 None。
    object_count: Optional[int] = None


class DbTableItem(BaseModel):
    """一张表或一个视图。"""

    name: str
    table_type: str = "BASE TABLE"  # BASE TABLE | VIEW
    engine: Optional[str] = None
    rows_estimate: Optional[int] = None
    comment: Optional[str] = None


class DbColumnItem(BaseModel):
    """一列的定义，对应 Navicat 的「设计表」。"""

    name: str
    column_type: str
    nullable: bool
    column_key: str = ""
    default: Optional[str] = None
    extra: str = ""
    comment: str = ""


class DbCompletionColumn(BaseModel):
    """查询编辑器补全用的一列：只要名字和类型，其余字段补全浮层用不上。"""

    name: str
    column_type: str = ""


class DbCompletionTable(BaseModel):
    """查询编辑器补全用的一张表：表名 + 全部列。"""

    name: str
    columns: List[DbCompletionColumn] = []


class DbRowFilter(BaseModel):
    """数据浏览的一个筛选条件。值一律按字符串传输，交给 MySQL 隐式转换。"""

    column: str = Field(min_length=1, max_length=128)
    op: Literal[
        "eq", "ne", "like", "not_like", "lt", "lte", "gt", "gte", "is_null", "is_not_null"
    ]
    value: Optional[str] = Field(default=None, max_length=1024)


class DbRowSort(BaseModel):
    """数据浏览的一个排序键，列表顺序即 ORDER BY 顺序。"""

    column: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"]


class DbRowsResult(BaseModel):
    """数据浏览的一页。``total`` 是真实 COUNT(*)，用于服务端分页。"""

    columns: List[str] = Field(default_factory=list)
    rows: List[List[Any]] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 100
    elapsed_ms: int = 0
    # 展示用：产生这一页的 SELECT 文本（参数已代回字面量），给底部 SQL 栏用。
    sql: str = ""


class DbRowUpdateRequest(BaseModel):
    """数据浏览页的行内改值。

    ``key`` 是行定位用的整行原值（列名 → 原值，None 表示 NULL），服务端有主键
    只取主键子集、没主键才全列 NULL 安全匹配——空 ``key`` 意味着整表 UPDATE，
    必须拒绝。``sets`` 是要修改的列 → 新值（None 表示设为 NULL）。
    """

    key: Dict[str, Any] = Field(min_length=1, max_length=128)
    sets: Dict[str, Any] = Field(min_length=1, max_length=128)


class DbRowDeleteRequest(BaseModel):
    """数据浏览页的删除记录，``key`` 语义同 DbRowUpdateRequest。"""

    key: Dict[str, Any] = Field(min_length=1, max_length=128)


class DbRowWriteResult(BaseModel):
    """行级写操作的结果。``affected`` 为 0 由服务层直接转成错误，这里只会是 1。"""

    affected: int = 0
    elapsed_ms: int = 0


class RedisKeyItem(BaseModel):
    key: str
    key_type: str = "unknown"


class RedisScanResult(BaseModel):
    """一次 SCAN 的产出。``cursor`` 为 "0" 表示扫完了。"""

    cursor: str = "0"
    keys: List[RedisKeyItem] = Field(default_factory=list)


class RedisKeyDetail(BaseModel):
    """一个 key 的完整只读视图。``value`` 的形态随 ``key_type`` 变：

    string → str；list/set → List[str]；hash/zset → List[List[str]]（两列）；
    stream → List[List[Any]]。
    """

    key: str
    key_type: str
    ttl: int = -1  # -1 永不过期，-2 不存在
    value: Any = None
    truncated: bool = False


class WsTicket(BaseModel):
    """WebSocket 入场票。有效期很短，只够完成一次握手。"""

    ticket: str
    expires_in: int


class SftpEntry(BaseModel):
    """远程文件浏览器里的一行。"""

    name: str
    is_dir: bool
    size: int
    mtime: int  # epoch 秒
    mode: int  # st_mode 权限位


class SftpListResult(BaseModel):
    """列目录结果。``path`` 是实际解析后的路径（请求为空时回填登录用户的 home）。"""

    path: str
    items: List[SftpEntry]
    truncated: bool = False


class SftpMkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class OpsAuditItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    target_type: str
    target_id: int
    target_name: Optional[str] = None
    actor: str
    command: str
    verdict: str
    success: bool
    error: Optional[str] = None
    created_at: datetime


# ---- 智能办公 · 简历 ---------------------------------------------------------

class ResumeUpdate(BaseModel):
    """简历的标题 / 描述修改；内容与分析结果不允许直接改。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class ResumeItem(BaseModel):
    """列表里的一行简历。不带 ``content`` 与 ``report`` 这类大字段。"""

    id: int
    title: str
    description: Optional[str] = None
    filename: str
    mime: Optional[str] = None
    size: int = 0
    status: str = "uploaded"
    error_msg: Optional[str] = None
    has_report: bool = False
    # 原文件是否已同步到用户自己的百度网盘；为 true 时前端才展示「下载原件」。
    has_netdisk: bool = False
    analyzed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ResumeDetail(ResumeItem):
    """简历详情，附带完整的分析结果。"""

    report: Optional[str] = None
    suggestions: Optional[List[str]] = None


class ResumePreview(BaseModel):
    """预览用的抽取文本。与详情分开返回：详情给报告页用，这个只给预览抽屉，
    两边都不替对方背大字段。"""

    id: int
    title: str
    filename: str
    mime: Optional[str] = None
    size: int = 0
    content: str


class ResumeCompareRequest(BaseModel):
    resume_ids: List[int] = Field(..., min_length=2, max_length=10)
    title: Optional[str] = Field(default=None, max_length=128)


class ResumeComparisonItem(BaseModel):
    id: int
    title: Optional[str] = None
    resume_ids: List[int]
    resume_titles: List[str] = []
    status: str = "analyzing"
    error_msg: Optional[str] = None
    created_at: datetime


class ResumeComparisonDetail(ResumeComparisonItem):
    report: Optional[str] = None


# ---- 智能办公 · 求职助手 -------------------------------------------------------

# 6 类生成任务；各 kind 的必填字段规则在 services/resume.py 的 TOOLKIT_KINDS 里校验。
ToolkitKind = Literal[
    "optimize",          # 简历优化
    "career-match",      # 职业匹配分析
    "jd-match",          # 简历匹配审计
    "interview-prep",    # 面试准备策略
    "portfolio-plan",    # 证明构建计划
    "salary-negotiation",  # 薪资最大化框架
]


class ResumeToolkitCreate(BaseModel):
    """发起一次求职助手生成。字段是全集，各 kind 只用其中一部分。"""

    kind: ToolkitKind
    # 简历来源二选一：引用已上传简历，或直接粘贴文本。
    resume_id: Optional[int] = None
    resume_text: Optional[str] = Field(default=None, max_length=20000)
    job_description: Optional[str] = Field(default=None, max_length=20000)
    position: Optional[str] = Field(default=None, max_length=128)
    background: Optional[str] = Field(default=None, max_length=20000)
    offer_amount: Optional[str] = Field(default=None, max_length=64)
    notes: Optional[str] = Field(default=None, max_length=2000)


class ResumeToolkitItem(BaseModel):
    """生成记录列表的一行；不带 inputs 与 report 这类大字段。"""

    id: int
    kind: str
    kind_label: str = ""
    title: str
    status: str = "analyzing"
    error_msg: Optional[str] = None
    created_at: datetime


class ResumeToolkitDetail(ResumeToolkitItem):
    inputs: Dict[str, Any] = {}
    report: Optional[str] = None


# ---- 智能办公 · 面试记录 -------------------------------------------------------

# 面试结果；裸字符串存储，与现有状态字段同一约定（不用 Enum）。
InterviewResult = Literal[
    "pending",  # 待定
    "passed",   # 通过
    "failed",   # 未通过
    "offer",    # 已 offer
]


class InterviewQuestionIn(BaseModel):
    """新增 / 修改一条面试问题。ref_*（AI 参考答案）只允许走生成接口，
    不在这个入口里出现——三条写路径各自管各自的字段，才不会互相踩掉。"""

    question: str = Field(..., min_length=1, max_length=4000)
    my_answer: Optional[str] = Field(default=None, max_length=20000)
    note: Optional[str] = Field(default=None, max_length=20000)


class InterviewQuestion(InterviewQuestionIn):
    """落库后的一条问题：多了服务端发的 qid 和 AI 参考答案三件套。"""

    qid: str
    ref_answer: Optional[str] = None
    # none | analyzing | ready | error
    ref_status: str = "none"
    ref_error: Optional[str] = None


class InterviewCreate(BaseModel):
    company: str = Field(..., min_length=1, max_length=128)
    position: str = Field(..., min_length=1, max_length=128)
    interview_date: Optional[date] = None
    round: Optional[str] = Field(default=None, max_length=32)
    result: InterviewResult = "pending"
    notes: Optional[str] = Field(default=None, max_length=20000)
    # 允许建场次时顺手带上第一批问题。
    questions: List[InterviewQuestionIn] = []


class InterviewUpdate(BaseModel):
    """面试场次的元数据修改；questions 有专门的题目级端点，不走这里——
    整体替换问题列表会和后台生成任务回写的 ref_* 互相覆盖。"""

    company: Optional[str] = Field(default=None, min_length=1, max_length=128)
    position: Optional[str] = Field(default=None, min_length=1, max_length=128)
    interview_date: Optional[date] = None
    round: Optional[str] = Field(default=None, max_length=32)
    result: Optional[InterviewResult] = None
    notes: Optional[str] = Field(default=None, max_length=20000)


class InterviewItem(BaseModel):
    """列表里的一行面试记录。不带 questions，只给计数。"""

    id: int
    company: str
    position: str
    interview_date: Optional[date] = None
    round: Optional[str] = None
    result: str = "pending"
    notes: Optional[str] = None
    question_count: int = 0
    created_at: datetime
    updated_at: datetime


class InterviewDetail(InterviewItem):
    questions: List[InterviewQuestion] = []


class NetdiskStatus(BaseModel):
    """当前用户的百度网盘绑定状态。configured=false 时前端隐藏网盘入口。"""

    configured: bool = False
    bound: bool = False
    baidu_name: Optional[str] = None
    expires_at: Optional[datetime] = None


class NetdiskAuthUrl(BaseModel):
    url: str


class NetdiskBindRequest(BaseModel):
    """OAuth 授权码（redirect_uri=oob 模式下用户从授权页手动复制回来）。"""

    code: str = Field(..., min_length=1, max_length=128)


class NetdiskSaveTextRequest(BaseModel):
    """把一段文本（分析报告等）存成网盘里的一个文件。

    落在应用目录下的 ``reports/`` 子目录；1MB 上限对一份报告绰绰有余，
    也挡住了拿这个端点当免费文件存储的用法。
    """

    filename: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=1024 * 1024)


class NetdiskSaveTextResult(BaseModel):
    """保存成功后网盘里的完整路径，给前端展示用。"""

    path: str


# ---- AI 网关 -----------------------------------------------------------------

class AiChannelCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field(..., min_length=1, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=512)
    models: Optional[List[str]] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class AiChannelUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    # 留空或回传掩码都表示「保持原样」，同 ModelConfigUpdate。
    api_key: Optional[str] = Field(default=None, max_length=512)
    models: Optional[List[str]] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class AiChannelItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    api_key: str = ""
    api_key_error: Optional[str] = None
    protocol: str = "openai"
    models: Optional[List[str]] = None
    enabled: bool = True
    remark: Optional[str] = None
    last_tested_at: Optional[datetime] = None
    last_test_ok: bool = False
    last_test_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AiChannelTestRequest(BaseModel):
    """连通性测试用的模型名；留空则取通道 models 列表的第一个。"""

    model: Optional[str] = Field(default=None, max_length=128)


class AiApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    remark: Optional[str] = Field(default=None, max_length=512)


class AiApiKeyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class AiApiKeyItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    # 明文只在创建那一次返回；列表里永远是这个前缀 + 掩码。
    key_prefix: str = ""
    enabled: bool = True
    remark: Optional[str] = None
    call_count: int = 0
    last_used_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AiApiKeyCreated(BaseModel):
    """创建密钥的返回：多带一次性的明文，之后再也取不到。"""

    item: AiApiKeyItem
    api_key: str


class AiModelRouteCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    model_name: str = Field(..., min_length=1, max_length=128)
    channel_id: int
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority: int = Field(default=100, ge=0, le=9999)
    enabled: bool = True
    remark: Optional[str] = Field(default=None, max_length=512)


class AiModelRouteUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    model_name: Optional[str] = Field(default=None, max_length=128)
    channel_id: Optional[int] = None
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority: Optional[int] = Field(default=None, ge=0, le=9999)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class AiModelRouteItem(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    model_name: str
    channel_id: int
    channel_name: Optional[str] = None
    upstream_model: Optional[str] = None
    priority: int = 100
    enabled: bool = True
    remark: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class AiCallLogItem(BaseModel):
    """调用日志列表里的一行，不带正文大字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    request_id: str
    key_id: Optional[int] = None
    key_name: Optional[str] = None
    endpoint: str
    model: Optional[str] = None
    stream: bool = False
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    upstream_model: Optional[str] = None
    status_code: int = 0
    success: bool = False
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    first_token_ms: Optional[int] = None
    client_ip: Optional[str] = None
    created_at: datetime


class AiCallLogDetail(AiCallLogItem):
    """单条日志的完整视图，含截断后的请求 / 响应正文与故障转移轨迹。"""

    attempts: Optional[List[Dict[str, Any]]] = None
    request_body: Optional[str] = None
    response_body: Optional[str] = None


class AiStatsTotals(BaseModel):
    calls: int = 0
    success: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    avg_latency_ms: int = 0
    max_latency_ms: int = 0


class AiStatsBucket(BaseModel):
    """按天 / 按通道 / 按模型 / 按密钥聚合出的一行。"""

    name: str
    calls: int = 0
    failed: int = 0
    total_tokens: int = 0
    avg_latency_ms: int = 0


class AiStats(BaseModel):
    totals: AiStatsTotals = AiStatsTotals()
    daily: List[AiStatsBucket] = Field(default_factory=list)
    by_channel: List[AiStatsBucket] = Field(default_factory=list)
    by_model: List[AiStatsBucket] = Field(default_factory=list)
    by_key: List[AiStatsBucket] = Field(default_factory=list)


class AiGatewayOverview(BaseModel):
    """网关首页的概览：接入信息 + 各类资源的数量。"""

    # 完整接入地址（含 /v1），管理员直接复制去填 SDK 的 base_url。
    base_url: str = ""
    channel_count: int = 0
    channel_enabled: int = 0
    route_count: int = 0
    route_enabled: int = 0
    key_count: int = 0
    key_enabled: int = 0
    log_count: int = 0
    payload_logging: bool = True


class AiLogPurgeRequest(BaseModel):
    """清理多少天之前的日志。"""

    before_days: int = Field(..., ge=1, le=3650)


class AiLogPurgeResult(BaseModel):
    deleted: int = 0
