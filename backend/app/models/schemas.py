"""Pydantic 请求 / 响应模式定义。"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Optional, Tuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # upload（上传文档）| repo（仓库同步的源文件）。老调用方不感知时默认 upload。
    source: str = "upload"
    repo_id: Optional[int] = None
    language: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class DocumentContent(BaseModel):
    """文档/仓库源文件的只读全文预览。仓库文件路径 + 行号预览用的就是它。"""

    model_config = _ALLOW_MODEL_PREFIX

    id: int
    name: str
    source: str = "upload"
    language: Optional[str] = None
    size: int = 0
    # 抽取出的全文（仓库文件即原始源码）；前端自己拆行渲染行号。
    content: str
    # 行数由后端算好，前端不用为了渲染行号再数一遍大字符串。
    line_count: int = 0
    status: str


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
    # 仓库维度的独立过滤：仓库与类型 1:1，服务端把仓库 id 换算成对位 type_id
    # 后仍走 type_id 过滤（不直接过滤 payload，避开老 point 缺字段的坑），
    # 与项目/类型范围取交集；越权或不存在的仓库 id 静默剔除。
    repo_ids: Optional[List[int]] = None

    score_threshold: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    chunk_id: int
    doc_id: int
    filename: str
    seq: int
    score: float
    content: str
    # 代码分片才有：来源、仓库、语言、行号区间、符号。散文分片这些列恒为 None。
    source: str = "upload"
    repo_id: Optional[int] = None
    language: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    symbol: Optional[str] = None


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


# ---- 知识库 · 代码仓库 ----------------------------------------------------

# 与 KbRepo.status 对齐的四态（pending 只在建行到首同步开始之间短暂出现）。
REPO_AUTH_SSH = "ssh_key"
REPO_AUTH_NONE = "none"
REPO_AUTH_HTTP_TOKEN = "http_token"


class RepoKeypair(BaseModel):
    """后端临时生成的一对 deploy key。不入库，私钥只随本次响应出现一次。"""

    public_key: str
    private_key: str


class RepoTestConnectionRequest(BaseModel):
    remote_url: str = Field(..., min_length=4, max_length=512)
    branch: Optional[str] = Field(default=None, max_length=255)
    # 可选私钥：给了就用它探测；匿名 http(s) 仓库不传。
    private_key: Optional[str] = Field(default=None, max_length=8000)
    # http_token 认证时的个人访问令牌（与可选用户名）；只用于本次探测，不入库。
    http_token: Optional[str] = Field(default=None, max_length=512)
    http_username: Optional[str] = Field(default=None, max_length=128)
    # SSH 可选：用户自带的主机指纹（SHA256:...），探测到的主机 key 必须与它匹配，
    # 不匹配直接拒绝——把 TOFU 的「首次信任」换成用户显式钉死。
    expected_fingerprint: Optional[str] = Field(default=None, max_length=255)


class RepoTestConnectionResult(BaseModel):
    default_branch: Optional[str] = None
    head_sha: str
    host: str
    # 首次见到并已写入临时 known_hosts 的主机指纹，接入时随仓库行一起落库。
    host_fingerprint: str
    known_hosts: str


class RepoCreate(BaseModel):
    project_id: int = Field(..., ge=1)
    remote_url: str = Field(..., min_length=4, max_length=512)
    branch: Optional[str] = Field(default=None, max_length=255)
    auth_type: str = REPO_AUTH_SSH
    # ssh_key 时必给（粘贴已有私钥，或 keypair 接口生成的那份）。
    private_key: Optional[str] = Field(default=None, max_length=8000)
    # http_token 时必给：个人访问令牌；http_username 可缺省。
    http_token: Optional[str] = Field(default=None, max_length=512)
    http_username: Optional[str] = Field(default=None, max_length=128)
    # 测试连接返回的 TOFU 记录；不给则首同步时现场 accept-new。
    known_hosts: Optional[str] = None
    host_fingerprint: Optional[str] = Field(default=None, max_length=255)
    name: Optional[str] = Field(default=None, max_length=128)
    exclude_globs: Optional[str] = Field(default=None, max_length=4000)


class RepoUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    branch: Optional[str] = Field(default=None, max_length=255)
    exclude_globs: Optional[str] = Field(default=None, max_length=4000)
    # 缺省=不变；显式空串配合 auth_type=none 清空私钥。
    private_key: Optional[str] = Field(default=None, max_length=8000)
    # 缺省（None）=令牌不变；显式空串=清空（配合切到匿名访问）。
    http_token: Optional[str] = Field(default=None, max_length=512)
    http_username: Optional[str] = Field(default=None, max_length=128)
    auth_type: Optional[str] = None


class RepoSyncResult(BaseModel):
    """一次同步的增删改计数，``last_sync_result`` 落库的就是它。"""

    added: int = 0
    changed: int = 0
    removed: int = 0
    skipped: int = 0
    indexed_chunks: int = 0
    duration_ms: int = 0
    commit: Optional[str] = None
    # 跳过明细：原因码 → 次数 / 样本路径（每码至多 5 条，路径截断 200 字）。
    # 老版本落库的 JSON 没有这两个键，缺省空容器即可向前兼容。
    skip_reasons: Dict[str, int] = Field(default_factory=dict)
    skip_samples: Dict[str, List[str]] = Field(default_factory=dict)
    # 同步后本地工作区的字节体积；超过 KB_REPO_WORKTREE_WARN_BYTES 时置告警位。
    worktree_bytes: int = 0
    worktree_warned: bool = False


class RepoItem(BaseModel):
    id: int
    type_id: int
    project_id: int
    name: str
    auth_type: str
    remote_url: str
    branch: Optional[str] = None
    has_credential: bool = False
    public_key: Optional[str] = None
    host_fingerprint: Optional[str] = None
    exclude_globs: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    head_commit: Optional[str] = None
    last_sync_at: Optional[datetime] = None
    last_sync_result: Optional[RepoSyncResult] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RepoSyncResponse(BaseModel):
    started: bool
    message: str
    # 锁占用（已经在同步）时为 True，前端据此静默而不是报错。
    already_running: bool = False


# ---- 对话 -------------------------------------------------------------------

class ConversationCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    title: str = Field(default="新会话", max_length=255)
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None
    # 会话级记住的可选工具：本地工具 key 与 MCP 服务 id。空数组=清空，不传=不动。
    local_tool_keys: Optional[List[str]] = None
    mcp_server_ids: Optional[List[int]] = None


class ConversationUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None
    local_tool_keys: Optional[List[str]] = None
    mcp_server_ids: Optional[List[int]] = None


class ConversationItem(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    id: int
    title: str
    agent_id: Optional[int] = None
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None
    model_config_id: Optional[int] = None
    local_tool_keys: Optional[List[str]] = None
    mcp_server_ids: Optional[List[int]] = None
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
    # 代码分片才有的定位信息；散文来源缺省。
    source: str = "upload"
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    symbol: Optional[str] = None


class ChatAttachmentItem(BaseModel):
    """一条消息附带的上传文件或图片。

    文件只回展示用的元信息，正文留在服务端（回放时直接拼给模型）；图片额外
    带 ``url`` 供前端缩略图使用。
    """

    id: int
    kind: str
    filename: str
    mime: Optional[str] = None
    size: int
    url: Optional[str] = None


class MessageItem(BaseModel):
    id: int
    role: str
    content: str
    citations: List[Citation] = Field(default_factory=list)
    # 按到达顺序记录的回答流 [{type: text|step|reason, detail}]。历史回放据此重建
    # 交错的命令与解说；老消息为 None 时前端退回只渲染 content。
    timeline: Optional[List[dict]] = None
    attachments: List[ChatAttachmentItem] = Field(default_factory=list)
    created_at: datetime


class MessageUpdate(BaseModel):
    """手动修正一条助手消息的内容。

    目前的唯一用途是「改图」：用户在前端编辑助手生成的 Mermaid 代码后整体
    回写。历史是回放给模型的，所以改写落库之后，下一轮提问看到的就是改后
    的版本——这是「AI 能感知手动修改」的全部机制。
    """

    content: str = Field(..., min_length=1, max_length=20000)


# 手动压缩时允许保留的最近轮数区间。至少留一轮原文在摘要之外，否则模型
# 下一句话就失去了对话的现场感；上限只是防止 UI 提交无意义的大值。
COMPACT_KEEP_TURNS_MIN = 1
COMPACT_KEEP_TURNS_MAX = 20
COMPACT_INSTRUCTION_MAX = 500


class CompactionPreviewRequest(BaseModel):
    """生成压缩预览：不落库任何东西，应用是另一次请求。"""

    keep_turns: int = Field(default=6, ge=1, le=COMPACT_KEEP_TURNS_MAX)
    instruction: Optional[str] = Field(default=None, max_length=COMPACT_INSTRUCTION_MAX)


class CompactionApplyRequest(BaseModel):
    """应用一份已预览的压缩：摘要文本 + 切点 + 本次使用的压缩要求。"""

    cut_message_id: int
    summary: str = Field(..., min_length=1, max_length=2000)
    instruction: Optional[str] = Field(default=None, max_length=COMPACT_INSTRUCTION_MAX)


class CompactionStateItem(BaseModel):
    """会话当前的摘要/压缩状态，供对话页顶部那条分界卡使用。"""

    summary: str
    # 有锚点表示用户手动压缩过：该 id 及之前的消息不再回放。
    cut_message_id: Optional[int] = None
    instruction: Optional[str] = None
    # 实际不进回放窗口的消息条数（手动压缩=锚点之前全部；自动摘要=窗口外溢出）。
    covered_count: int = 0
    updated_at: datetime


class CompletionRequest(BaseModel):
    """提问请求。回答通过 SSE 流式返回，而不是走这个响应外壳。"""

    model_config = _ALLOW_MODEL_PREFIX

    # 上限对齐手动改消息（上面 MessageUpdate.content）的 20000：报告页
    # 「发起对话」会把整份报告（含改进意见）作为首条消息发出；库字段是
    # _LONG_TEXT，没有更紧的约束。
    message: str = Field(default="", max_length=20000)
    # 先上传拿到的附件 id。允许「只发附件不写字」，但两者不能都为空。
    attachment_ids: List[int] = Field(default_factory=list, max_length=8)
    conversation_id: Optional[int] = None
    agent_id: Optional[int] = None

    # 基于知识库的智能体的检索范围。在多轮对话的后续提问中，
    # 不传该值就沿用会话本身已经带着的范围。
    project_id: Optional[int] = None
    type_ids: Optional[List[int]] = None

    # 想用哪份对话模型配置回答。不传则沿用会话记住的那份，会话也没记住
    # 就用「设置」里的默认对话模型。
    model_config_id: Optional[int] = None

    # 本轮生效的工具选择。显式带数组（含空数组）时在追问时更新会话：
    # None=沿用会话已记住的选择，[]=清空。
    local_tool_keys: Optional[List[str]] = None
    mcp_server_ids: Optional[List[int]] = None

    @model_validator(mode="after")
    def _require_question_or_attachment(self) -> "CompletionRequest":
        if not self.message.strip() and not self.attachment_ids:
            raise ValueError("请输入问题或上传附件")
        return self


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


class LocalToolParamItem(BaseModel):
    """本地工具目录里的一个参数说明。"""

    name: str
    description: str = ""
    type: str
    required: bool
    default: Optional[Any] = None


class LocalToolItem(BaseModel):
    """本地工具目录条目：代码注册结果的只读投影，没有任何写接口。"""

    key: str
    title: str
    description: str
    params: List[LocalToolParamItem] = Field(default_factory=list)
    source: str
    is_async: bool


# ---- 设置 / 模型配置 ------------------------------------------------

class ModelPurpose(str, Enum):
    """对话、向量、图片、视频、语音五类模型是分开独立配置的。

    ``image`` / ``video`` 不是 OpenAI 兼容的对话接口，走的是方舟自己的媒体端点，
    但凭证形状一模一样（base_url + model + api_key），所以复用同一张配置表，
    由 ``services/media/ark.py`` 按用途挑客户端。``tts`` 走方舟 OpenAI 兼容的
    ``/audio/speech``，客户端在 ``services/media/tts.py``。
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    IMAGE = "image"
    VIDEO = "video"
    TTS = "tts"


class ReasoningMode(str, Enum):
    """思维链的展示方式，只对对话模型有意义。

    ``auto`` 上游发了才展示；``on`` 额外把 ``request_extra`` 并进请求体去要求
    上游发；``off`` 一律丢弃。
    """

    AUTO = "auto"
    ON = "on"
    OFF = "off"


class ModelConfigCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    purpose: ModelPurpose
    title: str = Field(..., min_length=1, max_length=128)
    model_name: str = Field(..., min_length=1, max_length=128)
    base_url: str = Field(..., min_length=1, max_length=512)
    api_key: str = Field(default="", max_length=512)
    remark: Optional[str] = Field(default=None, max_length=512)
    reasoning_mode: ReasoningMode = ReasoningMode.AUTO
    request_extra: Optional[dict[str, Any]] = None
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
    # 由配置的人按服务商文档声明；只影响对话页是否允许上传图片。
    supports_vision: bool = False


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
    reasoning_mode: Optional[ReasoningMode] = None
    request_extra: Optional[dict[str, Any]] = None
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
    supports_vision: Optional[bool] = None


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
    reasoning_mode: ReasoningMode = ReasoningMode.AUTO
    request_extra: Optional[dict[str, Any]] = None
    temperature: Optional[int] = None
    supports_vision: bool = False
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
    # 短视频的两类生成模型：前端「办公 · 短视频」的准入门槛按这两个字段决定，
    # 缺哪个就在对应按钮上提示去配置，而不是等提交任务时上游报 401。
    image: PurposeStatus = Field(default_factory=PurposeStatus)
    video: PurposeStatus = Field(default_factory=PurposeStatus)
    # 配音是可选能力：不参与短视频页面的准入，只在工作台的配音开关处给提示。
    tts: PurposeStatus = Field(default_factory=PurposeStatus)
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
    temperature: Optional[int] = Field(default=None, ge=0, le=100)
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
    temperature: Optional[int]
    is_builtin: bool
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class AgentVersionItem(BaseModel):
    """版本列表里的一行：不带快照正文，只给辨认用的名字与时间。"""

    model_config = ConfigDict(from_attributes=True)

    version: int
    name: str
    created_at: datetime


class AgentVersionDetail(BaseModel):
    """某个版本的完整快照：历史运行对人设现场时取的就是这份。"""

    version: int
    created_at: datetime
    snapshot: Dict[str, Any]


# ---- 工作流 ---------------------------------------------------------------

class WorkflowInputField(BaseModel):
    """工作流声明的一个输入变量。"""

    name: str
    label: str
    required: bool = True


class WorkflowStepItem(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    key: str
    group_key: str
    sort_order: int
    agent_slug: str
    # 人设的展示名，便于界面不用再去查 agent 列表。
    agent_name: Optional[str] = None
    prompt_tpl: str
    output_kind: str
    output_keys: Optional[List[str]] = None
    output_var: Optional[str] = None
    model_config_id: Optional[int] = None
    # 节点级挂载的 MCP 服务：仅本步生效，与工作流级 mcp_server_ids 取并集。
    mcp_server_ids: List[int] = Field(default_factory=list)
    optional: bool


class WorkflowLastRun(BaseModel):
    """一条流程最近一次运行的摘要：清单页卡片上那一行。"""

    id: int
    status: str
    elapsed_ms: Optional[int] = None
    # 结束 / 开始 / 创建依次取的时间，pending 的运行只有创建时间。
    at: Optional[datetime] = None


class WorkflowItem(BaseModel):
    id: int
    slug: str
    name: str
    description: Optional[str] = None
    inputs: List[WorkflowInputField] = Field(default_factory=list)
    steps: List[WorkflowStepItem] = Field(default_factory=list)
    # 挂载的 MCP 服务 id：运行时所有步骤都能调用这些服务的工具。
    mcp_server_ids: List[int] = Field(default_factory=list)
    is_builtin: bool
    enabled: bool
    sort_order: int
    # 从没跑过就是 None：站点没上线，界面上不为它留「从未运行」的占位。
    last_run: Optional[WorkflowLastRun] = None
    created_at: datetime
    updated_at: datetime


class WorkflowRunRequest(BaseModel):
    """发起一次运行：键是 ``WorkflowItem.inputs`` 里的变量名。"""

    inputs: Dict[str, str] = Field(default_factory=dict)


class WorkflowStepInput(BaseModel):
    """编辑器里的一个步骤：整份定义随 ``WorkflowCreate`` / ``WorkflowUpdate`` 提交。

    没有 ``sort_order``——数组顺序就是执行顺序，让客户端再维护一份编号只会和
    界面上的上下移动对不上。
    """

    model_config = _ALLOW_MODEL_PREFIX

    key: str = Field(..., min_length=1, max_length=64)
    group_key: str = Field(..., min_length=1, max_length=64)
    agent_slug: str = Field(..., min_length=1, max_length=64)
    prompt_tpl: str = Field(..., min_length=1)
    output_kind: str = Field(default="markdown", max_length=16)
    output_keys: Optional[List[str]] = None
    output_var: Optional[str] = Field(default=None, max_length=64)
    model_config_id: Optional[int] = None
    # 节点级挂载：仅本步生效；与工作流级挂载取并集，重复只生效一次。
    mcp_server_ids: List[int] = Field(default_factory=list)
    optional: bool = False


class WorkflowDefinition(BaseModel):
    """一条工作流的可编辑内容。

    更新是**整份替换**而不是补丁：步骤列表是相互约束的（模板只能引用前面步骤的
    产物、同组不能互相引用），逐字段打补丁没法判断改完还合不合法，而界面上的
    编辑器本来就是一次提交整份。
    """

    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    inputs: List[WorkflowInputField] = Field(default_factory=list)
    steps: List[WorkflowStepInput] = Field(..., min_length=1)
    mcp_server_ids: List[int] = Field(default_factory=list)


class WorkflowCreate(WorkflowDefinition):
    slug: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")


class WorkflowUpdate(WorkflowDefinition):
    enabled: Optional[bool] = None


class WorkflowRunStepItem(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    id: int
    key: str
    group_key: str
    agent_slug: str
    # 开跑时绑定的人设版本：功能上线前的老运行两列都是 None。
    agent_id: Optional[int] = None
    agent_version: Optional[int] = None
    # 这一步挂的是哪个人设、可不可选、要什么格式：来自流程**当前**的定义。定义可能在
    # 这次运行之后被改过（内置流程每次启动就地刷新），所以这几项对得上才带。
    agent_name: Optional[str] = None
    optional: bool = False
    output_kind: Optional[str] = None
    # 产物以什么名字进的上下文：前端据此还原「这一步收到了哪些输入」。
    output_var: Optional[str] = None
    status: str
    prompt: Optional[str] = None
    output: Optional[str] = None
    parsed: Optional[Dict[str, Any]] = None
    # 步骤内部的统一执行轨迹（模型往返/工具调用节点）；老运行为 None。
    trace: Optional[List[Dict[str, Any]]] = None
    error_msg: Optional[str] = None
    model_config_id: Optional[int] = None
    # 这一步当时用的模型（``model_name``）：按步骤覆盖与默认模型可能混在同一条流程里，
    # 行级的聚合说不清是哪一步用的，现场按步给。
    model_name: Optional[str] = None
    elapsed_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# 运行的状态值，与 services/workflows.py 的 RUN_* 同一套。查询参数用它而不是裸
# ``str``：非法状态由 FastAPI 直接挡在 422，不必在路由里再判一次。
RunStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class WorkflowRunItem(BaseModel):
    # ``model_names`` 撞在 Pydantic 的 ``model_`` 保留前缀上，与本文件里其余
    # 带 model_* 字段的 DTO 同一处理。
    model_config = _ALLOW_MODEL_PREFIX

    id: int
    workflow_id: int
    workflow_slug: str
    # 这次运行为哪个业务对象跑的；手动跑的流水没有归属，三个字段都是 None。
    subject_kind: Optional[str] = None
    subject_ref: Optional[str] = None
    subject_label: Optional[str] = None
    status: str
    error_msg: Optional[str] = None
    elapsed_ms: Optional[int] = None
    # 这次运行各步实际用到的模型（``model_name``），按步骤顺序去重：列表上一眼看出
    # 「这次是 deepseek 还是通义跑的」，不用展开逐步骤看。
    model_names: List[str] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime


class WorkflowRunDetail(WorkflowRunItem):
    """运行详情：带输入快照与每一步的现场。"""

    inputs: Dict[str, Any] = Field(default_factory=dict)
    steps: List[WorkflowRunStepItem] = Field(default_factory=list)


class WorkflowRerunRequest(BaseModel):
    """单步重跑 / 断点续跑的请求体：对这次执行的上下文变量的覆盖值。

    只影响这一次执行：运行行上的输入快照不改，上游步骤行也不改，每个被重跑步骤
    实际收到的 prompt 仍逐行落库可查。JSON 产物的字段校验由服务层按定义完成。
    """

    inputs: Dict[str, str] = Field(default_factory=dict)


class WorkflowRunPruneRequest(BaseModel):
    """清理多少天之前的运行记录。"""

    keep_days: int = Field(..., ge=1, le=3650)


class WorkflowRunPruneResult(BaseModel):
    deleted_runs: int
    deleted_steps: int


# ---- 运行对比评测 -----------------------------------------------------------


class EvalCriterion(BaseModel):
    """评分细则里的一条标准；2–6 条有序标准组成一把可复用的尺子。"""

    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class RubricUpsert(BaseModel):
    """创建 / 更新评分细则共用的内容（整份替换，与工作流定义同一口径）。"""

    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    criteria: List[EvalCriterion] = Field(..., min_length=2, max_length=6)


class RubricItem(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    criteria: List[EvalCriterion] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EvalScore(BaseModel):
    """一条标准对两次运行的打分；``index`` 对齐保存时冻结的细则快照下标。"""

    index: int = Field(..., ge=0)
    score_a: int = Field(..., ge=1, le=5)
    score_b: int = Field(..., ge=1, le=5)


class EvaluationCreate(BaseModel):
    """提交一次 A/B 对比评测。

    两次运行必须属于同一条工作流；同一对运行 + 同一把尺子重复提交按更新处理，
    不会长出两行。
    """

    run_a_id: int
    run_b_id: int
    rubric_id: int
    scores: List[EvalScore] = Field(..., min_length=1)
    note: Optional[str] = Field(default=None, max_length=10000)


class EvalRunBrief(BaseModel):
    """评测行里嵌的运行摘要：列表不用再逐条拉运行详情。"""

    id: int
    status: str
    subject_label: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: datetime


class EvaluationItem(BaseModel):
    id: int
    workflow_id: int
    rubric_id: int
    # 细则可能已被删除：名字取不到为 None，标准仍以快照为准。
    rubric_name: Optional[str] = None
    rubric_snapshot: List[EvalCriterion] = Field(default_factory=list)
    run_a_id: int
    run_b_id: int
    run_a: EvalRunBrief
    run_b: EvalRunBrief
    scores: List[EvalScore] = Field(default_factory=list)
    # 非加权均分与 B−A 差值，后端算好：读侧不各自再算一遍。
    avg_a: float
    avg_b: float
    diff: float
    note: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ---- MCP 服务 ---------------------------------------------------------------

McpTransport = Literal["stdio", "sse", "streamable_http"]


class McpServerBase(BaseModel):
    """MCP 服务的可编辑内容。

    跨传输的必填规则（stdio 必须有 ``command``；sse/streamable_http 必须有
    ``url``）在 service 层校验，不写进 Pydantic——表单中途切换传输类型时
    字段会短暂全空，硬校验会让界面没法编辑。
    """

    name: str = Field(..., min_length=1, max_length=128)
    transport: McpTransport
    url: Optional[str] = Field(default=None, max_length=1024)
    command: Optional[str] = Field(default=None, max_length=1024)
    args: List[str] = Field(default_factory=list)
    # 入参是明文、回参的值逐个打码（见 McpServerService.to_item）。
    env: Dict[str, str] = Field(default_factory=dict)
    headers: Dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    remark: Optional[str] = Field(default=None, max_length=512)


class McpServerCreate(McpServerBase):
    pass


class McpServerUpdate(BaseModel):
    """部分更新：与 ``ModelConfigUpdate`` 同一口径，未传的字段不动。"""

    name: Optional[str] = Field(default=None, max_length=128)
    transport: Optional[McpTransport] = None
    url: Optional[str] = Field(default=None, max_length=1024)
    command: Optional[str] = Field(default=None, max_length=1024)
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    headers: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)


class McpToolSnapshotItem(BaseModel):
    """连通测试时从 MCP 服务发现的一个工具的快照。"""

    name: str
    description: str = ""
    # 原始 JSON Schema（properties/required/type/...），页面自行渲染参数。
    input_schema: Dict[str, Any] = Field(default_factory=dict)


class McpTestResult(BaseModel):
    """MCP 连通性探测结果：成功时带回本次发现的工具快照。"""

    success: bool
    message: str
    tools: List[McpToolSnapshotItem] = Field(default_factory=list)


class McpToolCallRequest(BaseModel):
    """页面上的方法测试：指定工具名与一组入参，现场调用一次。"""

    tool_name: str = Field(..., min_length=1, max_length=128)
    arguments: Dict[str, Any] = Field(default_factory=dict)


class McpToolCallResult(BaseModel):
    """tools/call 结果：参数与结果都不落库，仅回传展示。

    工具自身报错（协议正常、isError=True）时 success 仍为 True，
    由 ``is_error`` 区分；连接/协议失败时 success=False 并带 message。
    """

    success: bool
    message: str = ""
    is_error: bool = False
    # 原始内容块（text/image/audio/resourceLink/embeddedResource），
    # 块类型看 type 字段，页面按类型渲染。
    content: List[Dict[str, Any]] = Field(default_factory=list)
    structured_content: Optional[Dict[str, Any]] = None


class McpServerItem(McpServerBase):
    id: int
    owner_id: int
    last_test_at: Optional[datetime] = None
    last_test_ok: Optional[bool] = None
    last_test_msg: Optional[str] = None
    # 最近一次测试成功时发现的工具；未测/失败/连接信息已变更后为 None。
    tools: Optional[List[McpToolSnapshotItem]] = None
    created_at: datetime
    updated_at: datetime
    # 密钥袋解密失败（密钥轮换或数据损坏）时置 True，界面提示重新填写。
    secret_error: bool = False


# mcpServers 标准格式（Claude Desktop / Cursor 通用）的导入导出。
McpConflictStrategy = Literal["rename", "skip", "overwrite"]


class McpImportRequest(BaseModel):
    # 原始 {"mcpServers": {...}} 文档；结构宽松校验放 service 层，
    # 这样单条配置有问题时可以逐条报错而不是整批被 Pydantic 挡下。
    config: Dict[str, Any]
    conflict: McpConflictStrategy = "rename"


class McpImportReport(BaseModel):
    created: int = 0
    skipped: int = 0
    overwritten: int = 0
    # 形如 "lark-mcp -> lark-mcp-2"。
    renamed: List[str] = []
    # 形如 "lark-mcp: 缺少 command 或 url"。
    errors: List[str] = []


class McpExportResponse(BaseModel):
    mcpServers: Dict[str, Dict[str, Any]]
    # 如某条密钥解密失败：连接字段照出，凭据未导出。
    warnings: List[str] = []


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
    """一段脚本的批量执行结果。单句出错不中断，逐句成败都在这里。

    ``row_limit`` / ``stmt_timeout_seconds`` 回显服务端这次真正生效的两道上限：
    界面上「截断」「停止」这两句话要说得出数字，不能只说「见服务端配置」。
    """

    statements: List[DbBatchStatement]
    total: int
    succeeded: int
    failed: int
    started_at: datetime
    finished_at: datetime
    elapsed_ms: int = 0
    row_limit: int = 0
    stmt_timeout_seconds: int = 0


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
    # 建表顺序（ORDINAL_POSITION），设计表页签算列位置时用；结构页签忽略它。
    position: int = 0


class DbIndexItem(BaseModel):
    """一张表上的一个索引。``columns`` 按 ``SEQ_IN_INDEX`` 排序，
    联合索引的列顺序就是它的可用前缀顺序，不能乱。"""

    name: str
    primary: bool = False
    unique: bool = False
    index_type: str = ""  # BTREE | FULLTEXT | HASH | SPATIAL
    columns: List[str] = Field(default_factory=list)
    cardinality: Optional[int] = None
    comment: str = ""  # STATISTICS.COMMENT，MySQL 在这里写 "generate from page..." 之类


class DbForeignKeyItem(BaseModel):
    """一个外键约束。``columns``/``ref_columns`` 等长且按位置一一对应（联合外键）。"""

    name: str
    columns: List[str] = Field(default_factory=list)
    ref_schema: str
    ref_table: str
    ref_columns: List[str] = Field(default_factory=list)
    on_update: str = ""
    on_delete: str = ""


class DbTableStructure(BaseModel):
    """结构页签一次拉齐的三份元数据：列、索引、外键。

    三份都读同一张表的 information_schema，合并成一个端点复用一条连接。
    """

    columns: List[DbColumnItem] = Field(default_factory=list)
    indexes: List[DbIndexItem] = Field(default_factory=list)
    foreign_keys: List[DbForeignKeyItem] = Field(default_factory=list)


class DbTableDdl(BaseModel):
    """``SHOW CREATE TABLE`` 的原文。"""

    schema_name: str = Field(..., serialization_alias="schema")
    table: str
    ddl: str

    model_config = ConfigDict(populate_by_name=True)


# ---- 一键备份（整库或选表的 DDL + INSERT，存网盘或下载） -------------------------


class DbBackupRequest(BaseModel):
    """备份请求。默认结构 + 数据，只要结构时置 ``structure_only``。

    ``tables`` 是选表备份的名单：给了就只备这几张，空数组 / null 都是整库。
    """

    structure_only: bool = False
    tables: Optional[List[str]] = None


class DbBackupResult(BaseModel):
    """备份存进网盘后的落点与规模，前端toast 直接用。"""

    path: str
    filename: str
    tables: int
    rows: int
    bytes: int


# ---- 设计表（表定义编辑：期望定义 → ALTER 预览 → 确认执行） ----------------------


class DbTableDef(BaseModel):
    """一张表的当前定义，设计表页签的初值。"""

    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(..., serialization_alias="schema")
    table: str
    engine: Optional[str] = None
    collation: Optional[str] = None
    comment: str = ""
    columns: List[DbColumnItem] = Field(default_factory=list)
    primary_key: List[str] = Field(default_factory=list)
    # 不含主键本身（主键在 primary_key 里），与 DbIndexItem 的 primary 字段一致。
    indexes: List[DbIndexItem] = Field(default_factory=list)


class DbColumnDef(BaseModel):
    """设计表里的一列期望定义。

    ``auto_increment`` / ``on_update_current_timestamp`` 是只允许这两种取值的
    Extra：把它们收成布尔位，前端就交不进任意 SQL 片段。生成列
    （``GENERATED ALWAYS AS``）不在可编辑范围内，服务端识别到就拒绝改它。
    """

    name: str = Field(min_length=1, max_length=64)
    # 改名前的列名，没改名时留空。缺了它，改名就只能拆成「删一列 + 加一列」，
    # 那是把整列数据丢掉。
    origin_name: Optional[str] = Field(default=None, max_length=64)
    column_type: str = Field(min_length=1, max_length=128)
    nullable: bool = True
    auto_increment: bool = False
    on_update_current_timestamp: bool = False
    # DEFAULT 有两层含义：没写（走类型默认）与写成 NULL，必须分开表达。
    has_default: bool = False
    default: Optional[str] = Field(default=None, max_length=1024)
    comment: str = Field(default="", max_length=1024)


class DbIndexDef(BaseModel):
    """设计表里的一个期望索引（不含主键）。"""

    name: str = Field(min_length=1, max_length=64)
    unique: bool = False
    columns: List[str] = Field(min_length=1, max_length=32)


class DbTableDefUpdate(BaseModel):
    """整张表的期望定义。顺序即列顺序，服务端按它算 FIRST/AFTER。"""

    columns: List[DbColumnDef] = Field(min_length=1, max_length=512)
    primary_key: List[str] = Field(default_factory=list, max_length=32)
    indexes: List[DbIndexDef] = Field(default_factory=list, max_length=64)
    comment: str = Field(default="", max_length=2048)


class DbAlterAction(BaseModel):
    """一条将要执行的 ALTER 动作。``note`` 说清它为什么在列表里。"""

    kind: Literal[
        "column-add",
        "column-drop",
        "column-modify",
        "column-rename",
        "primary",
        "index-add",
        "index-drop",
        "comment",
    ]
    sql: str
    note: str = ""
    # 会丢数据的动作（删列），前端标红并要求额外确认。
    destructive: bool = False


class DbAlterPreview(BaseModel):
    """设计表预览：将要执行的语句 + 需要提前告知的风险。"""

    actions: List[DbAlterAction] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class DbTableAlterRequest(BaseModel):
    """执行设计表预览产出的语句。

    服务端不重新算 diff：来什么语句就重过一遍可写网关、逐句执行。语句与预览
    之间表结构被别人改过导致的失败，会原样出现在该句的错误里——这与手敲
    控制台的批量执行是同一套语义。
    """

    statements: List[str] = Field(min_length=1, max_length=200)


class DbErdColumn(BaseModel):
    """ER 图里一个实体的一条属性。"""
    name: str
    column_type: str
    key: str = ""


class DbErdTable(BaseModel):
    name: str
    table_type: str = "BASE TABLE"
    columns: List[DbErdColumn] = Field(default_factory=list)


class DbErdRelation(BaseModel):
    """一条外键关系：``from_table.from_columns`` → ``to_table.to_columns``。"""

    name: str
    from_table: str
    from_columns: List[str] = Field(default_factory=list)
    to_table: str
    to_columns: List[str] = Field(default_factory=list)


class DbErd(BaseModel):
    """一个 schema 的实体关系图数据。属性只挑主键/唯一/索引（多为外键）列，
    否则一张宽表就能把画布撑爆；``truncated`` 表示表数量超上限被裁过。"""

    schema_name: str = Field(..., serialization_alias="schema")
    tables: List[DbErdTable] = Field(default_factory=list)
    relations: List[DbErdRelation] = Field(default_factory=list)
    truncated: bool = False

    model_config = ConfigDict(populate_by_name=True)


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


# ---- Redis 结构化写操作 ---------------------------------------------------------
#
# 这些请求体里没有「命令」字段：用户给的是坐标（key / field / member / 下标），
# 命令由服务端用 redis-py 的类型化方法拼出来，值永远进不了命令解析器，
# 因此不存在拼接注入。被拦的只有 WRONGTYPE、越界下标这类真实语义错误。

RedisKeyType = Literal["string", "list", "set", "zset", "hash", "stream"]


class RedisWriteResult(BaseModel):
    """一次结构化写的回执。

    ``command`` 是服务端生成的命令文本（值过长会截断），既给前端提示用，也是
    审计里那条记录——用户能事后核对到底改了什么。
    """

    command: str = ""
    elapsed_ms: int = 0
    deleted: int = 0


class RedisKeyCreateRequest(BaseModel):
    """新建 key。``value`` 的形态按 ``key_type`` 解释：

    string → 文本；list / set → 成员数组；hash / zset → 两列数组
    （hash 为 [field, value]，zset 为 [member, score]）。
    """

    db: int = Field(default=0, ge=0, le=15)
    key: str = Field(min_length=1, max_length=4096)
    key_type: Literal["string", "list", "set", "zset", "hash"]
    value: Any = None
    ttl: Optional[int] = Field(default=None, ge=1, le=3153600000)


class RedisStringUpdateRequest(BaseModel):
    """改 string 的值。TTL 由服务端读取后原样续上（不静默清掉过期时间）。"""

    db: int = Field(default=0, ge=0, le=15)
    key: str = Field(min_length=1, max_length=4096)
    value: str = Field(max_length=1024 * 1024)


class RedisElementAddRequest(BaseModel):
    """给集合加一个元素。``value`` 是成员/列表元素/value，``field`` 只用于 hash，
    ``score`` 只用于 zset，``position`` 只用于 list。"""

    db: int = Field(default=0, ge=0, le=15)
    key: str = Field(min_length=1, max_length=4096)
    key_type: Literal["list", "set", "zset", "hash"]
    value: Optional[str] = Field(default=None, max_length=1024 * 1024)
    field: Optional[str] = Field(default=None, max_length=4096)
    score: Optional[float] = None
    position: Literal["head", "tail"] = "tail"


class RedisElementDeleteRequest(BaseModel):
    """删掉一个元素。``target`` 按类型解释：hash=field，set/zset=member，
    stream=条目 id，list=元素下标（字符串形式的非负整数）。"""

    db: int = Field(default=0, ge=0, le=15)
    key: str = Field(min_length=1, max_length=4096)
    key_type: RedisKeyType
    target: str = Field(min_length=1, max_length=4096)


class RedisTtlRequest(BaseModel):
    """TTL 操作：``expire`` 设过期秒数，``persist`` 取消过期。"""

    db: int = Field(default=0, ge=0, le=15)
    key: str = Field(min_length=1, max_length=4096)
    action: Literal["expire", "persist"]
    seconds: Optional[int] = Field(default=None, ge=1, le=3153600000)


class RedisKeyDeleteRequest(BaseModel):
    """批量删除 key（服务端用 UNLINK 异步回收，不阻塞主线程）。"""

    db: int = Field(default=0, ge=0, le=15)
    keys: List[str] = Field(min_length=1, max_length=200)


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
    # 后端允许的上传上限（字节）。跟着列目录一起下发，面板就能在选文件的当下
    # 拒绝超限，而不是让用户白传一整轮。
    max_upload_bytes: int = 0


class SftpMkdirRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class SftpPasteRequest(BaseModel):
    """服务端复制/剪切：把 ``src`` 落到目录 ``dst_dir`` 里。"""

    src: str = Field(min_length=1, max_length=1024)
    dst_dir: str = Field(min_length=1, max_length=1024)


class SftpPasteResult(BaseModel):
    """粘贴结果。``path`` 是实际落地的绝对路径——同名时会是 ``xxx_副本.ext``。"""

    path: str
    renamed: bool


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


# ---- 智能办公 · 在线简历编辑器 -------------------------------------------------

# 五个可排序模块的键；顺序契约与 services/resume_builder.py、前端 resumeDoc.ts 共用。
ResumeDocModule = Literal["profile", "companies", "projects", "educations", "skills"]


class ResumeDocCreate(BaseModel):
    """新建在线简历；文档结构用默认骨架，不在创建时让前端传。"""

    title: str = Field(..., min_length=1, max_length=128)


class ResumeDocUpdate(BaseModel):
    """标题与文档内容都可单独改：编辑器保存传 doc，重命名只传 title。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    doc: Optional[Dict[str, Any]] = None


class ResumeDocItem(BaseModel):
    """列表行：不带 doc 大字段。"""

    id: int
    title: str
    # 「保存到简历」同步到简历库的那一行；NULL=还没同步过。
    resume_id: Optional[int] = None
    updated_at: datetime
    created_at: datetime


class ResumeDocDetail(ResumeDocItem):
    doc: Dict[str, Any]


class ResumeDocAiRequest(BaseModel):
    """简历编辑器里的流式 AI 请求（帮写 / 润色 / 整份体检）。

    doc_id 提供整份简历上下文（可空：空文档也能先帮写）；module + item_id
    定位「帮写的是哪个模块的哪一条」；selection 是用户在富文本里选中的原文。
    """

    doc_id: Optional[int] = None
    module: ResumeDocModule
    # write=按已有信息生成/扩写描述；polish=润色当前描述；check=整份体检（markdown）。
    action: Literal["write", "polish", "check"]
    item_id: Optional[str] = Field(default=None, max_length=64)
    selection: Optional[str] = Field(default=None, max_length=20000)


# ---- 智能办公 · 幻灯片 ---------------------------------------------------------

# 页面版式。裸字符串存储，与现有状态字段同一约定（不用 Enum）；每个版式对应
# services/slides/html.py 里的一个渲染分支，加版式要同时改这两处。
SlideLayout = Literal[
    "cover",     # 封面：大标题 + 副标题
    "section",   # 章节过渡页
    "bullets",   # 标题 + 要点列表
    "image",     # 标题 + 要点 + 配图
    "quote",     # 金句 / 结论页
    "closing",   # 结尾页
    "table",     # 对比表：表头 + 若干行
    "steps",     # 步骤条：编号 + 标题 + 一句说明
    "flow",      # 链路图：方框串成一条线，箭头上可带条件
    "arch",      # 分层图：自上而下的层，每层若干模块
]
# 外观预设与画布比例，同样由渲染器的白名单把关。amber 是默认。
SlidePreset = Literal["amber", "ink", "teal", "paper", "violet"]
SlideRatio = Literal["16x9", "4x3"]


class SlideTheme(BaseModel):
    """一套外观。``accent`` 是可选的强调色覆盖，只收 ``#rgb`` / ``#rrggbb``，
    其余值在渲染器里回落预设色——样式串是直接拼进 CSS 的，必须验白名单。"""

    preset: SlidePreset = "amber"
    ratio: SlideRatio = "16x9"
    accent: Optional[str] = Field(default=None, max_length=9)


class SlideTable(BaseModel):
    """对比表的内容。列数、行数与格长上限都在渲染器里裁（模型输出不归 Pydantic 管），
    这里只挡明显越界的整包请求。"""

    columns: List[str] = Field(default_factory=list, max_length=4)
    rows: List[List[str]] = Field(default_factory=list, max_length=6)


class SlideStep(BaseModel):
    """步骤条里的一步：``desc`` 可以空。"""

    title: str = Field(default="", max_length=40)
    desc: str = Field(default="", max_length=80)


class SlideFlowNode(BaseModel):
    """链路图里的一格。``via`` 是走到下一格的条件，画在右侧箭头上。

    最后一格的 ``via`` 会被渲染器清掉（右边没有箭头），所以前端也别在它上面留输入框。
    """

    title: str = Field(default="", max_length=16)
    desc: str = Field(default="", max_length=32)
    via: str = Field(default="", max_length=10)


class SlideArchLayer(BaseModel):
    """分层图的一层：``blocks`` 是层内的模块名，数组顺序就是自上而下的层序。

    单个方块字符串的长度由渲染器裁（10 字，见 ``html.MAX_ARCH_BLOCK``），与要点
    列表同一个做法：这里只管条数。
    """

    name: str = Field(default="", max_length=10)
    blocks: List[str] = Field(default_factory=list, max_length=4)


class SlidePageIn(BaseModel):
    """新增 / 修改一页幻灯片。``sid`` 由服务端发放，前端不填（与面试题的 qid 同）。"""

    layout: SlideLayout = "bullets"
    # kicker 是页头的小类别标签，takeaway 是贴在页面底部的那句结论。
    kicker: Optional[str] = Field(default=None, max_length=24)
    title: Optional[str] = Field(default=None, max_length=200)
    subtitle: Optional[str] = Field(default=None, max_length=400)
    # 每条 300 字符的上限在 service 里裁剪：Pydantic 管不了 list 元素内部的长度。
    # 文本里的 ``**关键词**`` 会被渲染成加粗强调，是内容不是样式，原样存。
    bullets: List[str] = Field(default_factory=list, max_length=12)
    table: SlideTable = Field(default_factory=SlideTable)
    steps: List[SlideStep] = Field(default_factory=list, max_length=5)
    flow: List[SlideFlowNode] = Field(default_factory=list, max_length=5)
    layers: List[SlideArchLayer] = Field(default_factory=list, max_length=5)
    takeaway: Optional[str] = Field(default=None, max_length=160)
    asset_id: Optional[str] = Field(default=None, max_length=32)
    notes: Optional[str] = Field(default=None, max_length=4000)


class SlidePage(SlidePageIn):
    """落库后的一页：多了服务端发的 sid。"""

    sid: str


class SlidePageSave(SlidePageIn):
    """整包保存时的一页。``sid`` 可选：新加的页还没有，由服务端发放。

    这里若收成必填的 :class:`SlidePage`，工作台「加一页 → 保存」会直接被 422
    拦下——前端手上不可能有还没发出去的 sid。
    """

    sid: Optional[str] = Field(default=None, max_length=32)


class SlideAsset(BaseModel):
    """一张已落盘的配图。``url`` 浏览器可直接访问，``rel_path`` 只用于删除时定位文件。"""

    id: str
    name: str
    mime: str = "image/png"
    size: int = 0
    rel_path: str = ""
    url: str = ""


class SlideDeckItem(BaseModel):
    """列表里的一行幻灯片。不带 slides / source_text / html 这类大字段。"""

    id: int
    title: str
    description: Optional[str] = None
    status: str = "analyzing"
    error_msg: Optional[str] = None
    page_count: int = 0
    asset_count: int = 0
    created_at: datetime
    updated_at: datetime


class SlideDeckDetail(SlideDeckItem):
    """工作台要的完整数据。``html`` 不在这里——预览走独立接口。"""

    requirement: Optional[str] = None
    source_text: str = ""
    theme: SlideTheme = Field(default_factory=SlideTheme)
    slides: List[SlidePage] = Field(default_factory=list)
    assets: List[SlideAsset] = Field(default_factory=list)


class SlideDeckSave(BaseModel):
    """工作台的整包保存：标题 + 主题 + 整组页面。

    刻意不收 HTML：``html`` 列是 slides 的派生物，每次写入都在服务端重渲染，
    否则「看到的」和「导出的」迟早漂移。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    theme: Optional[SlideTheme] = None
    slides: Optional[List[SlidePageSave]] = Field(default=None, max_length=80)


class SlideDeckRegenerate(BaseModel):
    """重新生成。材料仍用创建时的快照，这里只能补一句要求。"""

    requirement: Optional[str] = Field(default=None, max_length=512)


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


# ---- 智能办公 · 招聘岗位 -------------------------------------------------------


class JobFilter(BaseModel):
    """BOSS 抓取与本地缓存列表共用的一组筛选条件。薪资单位是 K/月。"""

    keyword: str = Field(default="", max_length=100)
    city: str = Field(default="全国", min_length=1, max_length=32)
    posted_days: Optional[int] = None
    salary_min: Optional[float] = Field(default=None, ge=0, le=1000)
    salary_max: Optional[float] = Field(default=None, ge=0, le=1000)

    @field_validator("keyword", "city")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("city")
    @classmethod
    def _require_city(cls, value: str) -> str:
        if not value:
            raise ValueError("城市不能为空")
        return value

    @field_validator("posted_days")
    @classmethod
    def _check_posted_days(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value not in (1, 3, 7, 30):
            raise ValueError("发布时间只支持近 1、3、7、30 天")
        return value

    @model_validator(mode="after")
    def _check_salary_range(self) -> "JobFilter":
        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                raise ValueError("最低薪资不能高于最高薪资")
        return self


class JobItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    company: str
    city: str
    district: Optional[str] = None
    salary_text: str
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    experience: Optional[str] = None
    education: Optional[str] = None
    skills: List[str] = Field(default_factory=list)
    recruiter: Optional[str] = None
    published_at: Optional[datetime] = None
    apply_url: str
    first_seen_at: datetime
    last_seen_at: datetime


class JobFetchResult(BaseModel):
    fetched: int
    matched: int
    created: int
    updated: int
    fetched_at: datetime


# ---- 智能办公 · 求职笔记 -------------------------------------------------------


def _clean_note_title(value: Optional[str]) -> Optional[str]:
    """标题 strip 后必须非空：min_length 只数字符数，挡不住纯空格。"""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("标题不能为空")
    return value


class NoteCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=100_000)

    _clean_title = field_validator("title")(_clean_note_title)


class NoteUpdate(BaseModel):
    """全字段可选、部分更新；显式传 null 会把正文清空（与面试记录同一约定）。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=100_000)

    _clean_title = field_validator("title")(_clean_note_title)


class NoteItem(BaseModel):
    """列表里的一行笔记：不带正文，只给折叠空白后的摘要。"""

    id: int
    title: str
    snippet: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class NoteDetail(NoteItem):
    content: Optional[str] = None


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

class AiModelCapability(str, Enum):
    """Auto 用来判断「这个模型接不接得了这次请求」的硬性能力。

    与 :class:`ModelPurpose` 不是一回事：那边是「用户给自己的助手配了这一类模型」，
    这边是平台网关里一条上游映射的能力声明。
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    VISION = "vision"
    TOOLS = "tools"


class AiModelTaskKind(str, Enum):
    """Auto 用来判断「谁更适合接这次请求」的任务类型；general 是兜底档。"""

    GENERAL = "general"
    REASONING = "reasoning"
    CODE = "code"
    WRITING = "writing"
    TRANSLATION = "translation"


# auto_tags 这一列允许的取值：能力在前、任务类型在后，Auto 按前一组做筛选、
# 按后一组做排序。词表放在这里（而不是 auto 服务模块）是因为请求校验与管理服务
# 两边都要读，而 models 层不接任何服务模块。
AI_AUTO_TAGS: Tuple[str, ...] = tuple(tag.value for tag in AiModelCapability) + tuple(
    tag.value for tag in AiModelTaskKind
)


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


def _naive_local_datetime(value: Optional[datetime]) -> Optional[datetime]:
    """把带时区的有效期折成服务器本地 naive，与 ``db_now()`` 同口径。

    库里的 DATETIME 全是本地墙上时间（见 :mod:`app.timeutil`）；前端控件给的是
    不带偏移的字符串，直接放行，只有显式带时区的值才需要折一道，否则和
    ``db_now()`` 比大小会错开 8 小时。
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value


def _normalize_auto_tags(tags: Optional[List[str]]) -> Optional[List[str]]:
    """去空白、转小写、去重，并拒绝词表之外的标签。

    词表卡死是有原因的：标签写错一个字母，这条路由就永远选不出来，而现象只是
    「Auto 偶尔不打给我配的那个模型」——排查起来完全没有方向。
    """
    if tags is None:
        return None
    cleaned: List[str] = []
    for raw in tags:
        tag = (raw or "").strip().lower()
        if not tag:
            continue
        if tag not in AI_AUTO_TAGS:
            raise ValueError(f"未知的模型标签 {raw!r}，可用的是：{', '.join(AI_AUTO_TAGS)}")
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


class AiModelRouteCreate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    model_name: str = Field(..., min_length=1, max_length=128)
    channel_id: int
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority: int = Field(default=100, ge=0, le=9999)
    enabled: bool = True
    remark: Optional[str] = Field(default=None, max_length=512)
    # 有效期截止时刻与 token 总额度：都可选，留空（null）表示不限制。
    expires_at: Optional[datetime] = None
    token_quota: Optional[int] = Field(default=None, ge=1)
    # 参与 Auto 路由：进入 model=auto 的候选池。
    auto_eligible: bool = False
    auto_tags: Optional[List[str]] = None

    _clean_tags = field_validator("auto_tags")(_normalize_auto_tags)
    _naive_expires = field_validator("expires_at")(_naive_local_datetime)


class AiModelRouteUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    model_name: Optional[str] = Field(default=None, max_length=128)
    channel_id: Optional[int] = None
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority: Optional[int] = Field(default=None, ge=0, le=9999)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)
    # 显式传 null 表示清除限制；字段没出现在请求里才是「不动」。
    expires_at: Optional[datetime] = None
    token_quota: Optional[int] = Field(default=None, ge=1)
    auto_eligible: Optional[bool] = None
    auto_tags: Optional[List[str]] = None

    _clean_tags = field_validator("auto_tags")(_normalize_auto_tags)
    _naive_expires = field_validator("expires_at")(_naive_local_datetime)


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
    expires_at: Optional[datetime] = None
    token_quota: Optional[int] = None
    token_used: int = 0
    # 服务端按当前时刻算出的限制状态：null=未受限，expired / quota_exceeded。
    limit_reason: Optional[str] = None
    auto_eligible: bool = False
    auto_tags: Optional[List[str]] = None
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
    # Auto 那次的决策快照（{required, task, source, resolved_model}）；点名模型时为空。
    auto_route: Optional[Dict[str, Any]] = None
    status_code: int = 0
    success: bool = False
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # 命中缓存的 prompt token；None = 上游没报缓存（区别于报了 0）。命中率由前端算。
    cached_tokens: Optional[int] = None
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


# ---- 首页工作台 ---------------------------------------------------------------


class DashboardSummary(BaseModel):
    """首页工作台的概览：各模块在调用方名下的资源数量。"""

    agent_count: int = 0
    knowledge_project_count: int = 0
    knowledge_document_count: int = 0
    conversation_count: int = 0
    resume_count: int = 0
    slide_count: int = 0
    video_project_count: int = 0
    interview_count: int = 0
    note_count: int = 0
    memory_count: int = 0
    workflow_count: int = 0
    # 调用方今天发起过的运行次数：工作流是「流水线定义」，光数条数说明不了有没有
    # 在用，配上当天的执行次数才看得出这是不是一条活着的链路。
    run_count_today: int = 0
    server_count: int = 0
    database_count: int = 0

    # AI 网关是平台级共享资产，只有管理员看得到。留 None 而不是 0：前端按
    # 「这一格出不出现」判断，0 会被读成「网关还没接通道」而不是「无权查看」。
    ai_channel_count: Optional[int] = None
    ai_call_count_today: Optional[int] = None
    ai_token_count_today: Optional[int] = None


# ---- 开发工具 · YApi 同步 ----------------------------------------------------


class YapiScanRequest(BaseModel):
    """本地 Java 源码根目录；后端所在机器上的路径。"""

    root_path: str = Field(..., min_length=1, max_length=2048)


class YapiEndpointParam(BaseModel):
    name: str
    location: Literal["query", "path", "header", "body", "form"]
    type: str
    required: bool = False
    description: str = ""
    example: str = ""


class YapiScanEndpoint(BaseModel):
    package: str
    category_name: str
    controller: str
    method: str
    title: str
    http_method: str
    path: str
    tag: str = ""
    summary: str = ""
    description: str = ""
    source: str
    line: int = 0
    params: List[YapiEndpointParam] = Field(default_factory=list)
    request_body_type: Literal["json", "form", ""] = ""
    request_body_schema: Optional[Dict[str, Any]] = None
    response_schema: Optional[Dict[str, Any]] = None


class YapiScanCategory(BaseModel):
    """YApi 分类：一个 Controller 一个分类，名字来自 Swagger 注解。"""

    category_name: str
    package: str
    controllers: List[str] = Field(default_factory=list)
    controller_count: int = 0
    endpoint_count: int = 0


class YapiScanResult(BaseModel):
    root_path: str
    java_files: int = 0
    controller_count: int = 0
    endpoint_count: int = 0
    categories: List[YapiScanCategory] = Field(default_factory=list)
    endpoints: List[YapiScanEndpoint] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    duration_ms: int = 0


class YapiSyncRequest(YapiScanRequest):
    base_url: str = Field(..., min_length=1, max_length=512)
    project_id: int = Field(..., ge=1)
    token: str = Field(..., min_length=1, max_length=512)
    # 「METHOD path」形式的接口键；不传表示同步全部（服务层调用），传空列表表示一个都不同步。
    endpoint_keys: Optional[List[str]] = None


class YapiSyncEndpoint(YapiScanEndpoint):
    action: Literal["created", "updated", "failed"]
    error: Optional[str] = None


class YapiSyncResult(BaseModel):
    base_url: str
    project_id: int
    created: int = 0
    updated: int = 0
    failed: int = 0
    categories_created: int = 0
    endpoint_count: int = 0
    endpoints: List[YapiSyncEndpoint] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    duration_ms: int = 0


# ---- 消息通知 ---------------------------------------------------------------


class NotifyKind(str, Enum):
    """通知渠道的 Webhook 协议方言。"""

    FEISHU = "feishu"
    WECOM = "wecom"
    DINGTALK = "dingtalk"
    GENERIC = "generic"


class NotifyEventInfo(BaseModel):
    """一个可订阅的事件：key + 中文说明，供前端渲染勾选组。"""

    key: str
    label: str
    default_on: bool = False


class NotifyChannelCreate(BaseModel):
    """新建一个通知渠道。

    ``secret`` 只有飞书（可选加签）和钉钉（加签机器人）用得上，其他渠道忽略。
    """

    name: str = Field(..., min_length=1, max_length=64)
    kind: NotifyKind
    webhook_url: str = Field(..., min_length=1, max_length=2048)
    secret: Optional[str] = Field(default=None, max_length=256)
    events: List[str] = Field(default_factory=list)
    enabled: bool = True


class NotifyChannelUpdate(BaseModel):
    """修改渠道。

    ``webhook_url`` / ``secret`` 留空或回传掩码都表示「保持原样」——同
    ``ModelConfigUpdate``，把掩码原样写回会在用户只改名时悄悄清掉一个能用的
    机器人地址。
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    kind: Optional[NotifyKind] = None
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    secret: Optional[str] = Field(default=None, max_length=256)
    events: Optional[List[str]] = None
    enabled: Optional[bool] = None


class NotifyChannelItem(BaseModel):
    """渠道列表中的一行。地址与密钥只以掩码出现。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: NotifyKind
    webhook_url: str = ""
    webhook_url_error: Optional[str] = None
    has_secret: bool = False
    secret_error: Optional[str] = None
    events: List[str] = Field(default_factory=list)
    enabled: bool = True
    last_test_at: Optional[datetime] = None
    last_test_ok: Optional[bool] = None
    last_test_error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class NotifyLogItem(BaseModel):
    """最近发送记录里的一行。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    channel_name: str
    event: str
    title: str
    body: Optional[str] = None
    status: str
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: datetime


class AppMessageItem(BaseModel):
    """移动 App 收件箱里的一条业务通知。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    event: str
    title: str
    body: Optional[str] = None
    created_at: datetime


# ---- 智能应用 · 短视频 ------------------------------------------------------

# 这三档白名单直接抄方舟的入参取值（词表在 ``services/media/__init__.py``）：
# 界面上选出来的值要能原样进请求体，中间不翻译。
VideoPlatform = Literal["douyin", "kuaishou", "other"]
VideoGenMode = Literal["t2v", "first_frame", "first_last_frame", "reference"]
VideoRatio = Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
VideoResolution = Literal["480p", "720p", "1080p"]
ImageSize = Literal["1K", "2K", "4K"]
MediaJobKind = Literal["image", "video"]
MediaJobStatus = Literal[
    "queued", "running", "downloading", "succeeded", "failed", "cancelled", "expired"
]


class VideoParams(BaseModel):
    """一个项目的渲染参数。发起作业时抄进 ``media_job.request``，之后改这里不影响在跑的作业。

    ``generate_audio`` 默认**关**：方舟那一侧默认开，留着不动就会拿到一条模型自己
    编的人声或背景乐——它不知道台词、也没有时间戳，与 TTS 配音轨会互相打架。人声
    统一由 ``tts_enabled`` 控制的 TTS 链路在成片合成阶段装配，视频片段始终保持静音。
    写在这里而不是靠上游默认，是为了让「这条片段为什么没有声音」当场有据可查。

    ``tts_enabled`` / ``tts_voice`` 是**成片**参数而不是渲染参数：TTS 在发布作业里
    按各镜台词逐句合成、按镜头时间轴混进成片，不碰任何 ``media_job``。``tts_voice``
    留空时退到环境变量 ``TTS_DEFAULT_VOICE``。

    ``ratio`` 默认**显式 9:16** 而不是 ``adaptive``：出图那一档只带尺寸不带画幅，
    ``adaptive`` 是「跟着首帧走」，首帧出成方图就整条跟着方。竖屏产品不能把方向
    交给运气，横屏选题在渲染参数里切回去就是。

    例外：Seedance 2.5 的首帧/首尾帧生视频不再接受显式 ratio（上游 400
    TaskTypeConstraint，画幅由首帧图决定），入队时这一档会被服务层自动改写成
    ``adaptive``；纯文生与 2.0 及更早型号仍按这里的显式档位发送。
    """

    ratio: VideoRatio = "9:16"
    resolution: VideoResolution = "720p"
    watermark: bool = False
    generate_audio: bool = False
    image_size: ImageSize = "2K"
    # 成片时是否叠 TTS 配音。默认关：不配置语音模型的账号行为与之前完全一致。
    tts_enabled: bool = False
    # 音色 ID（各家模型的音色字表不同，代码不做枚举）；空串 = 用系统默认音色。
    tts_voice: str = Field(default="", max_length=128)
    # 尾帧链式渲染：上一镜片段的尾帧作为下一镜的首帧输入，镜间画面物理连续。
    # 开了它片段就从并发变串行（同项目内一次只跑一条），严格模式下非首帧模式的
    # 镜头入队即拒。默认关，关掉时每条片段仍是互相独立的一次生成。
    chain_clips: bool = False


class VideoStyle(BaseModel):
    """全片风格约束：逐镜提示词都要贴它，改一处比改十几镜更能保持统一。"""

    visual_style: str = Field(default="", max_length=256)
    persona: str = Field(default="", max_length=128)
    palette: str = Field(default="", max_length=128)


class VideoBrief(BaseModel):
    """创作卡：脚本流程第一步的产物，也就是「这条片子凭什么让人看完」。"""

    hook_type: str = Field(default="", max_length=16)
    hook: str = Field(default="", max_length=128)
    audience: str = Field(default="", max_length=256)
    goal: str = Field(default="", max_length=256)
    structure: List[str] = Field(default_factory=list, max_length=8)
    duration_s: int = Field(default=15, ge=5, le=60)
    risk_notes: str = Field(default="", max_length=512)


class VideoPublish(BaseModel):
    """发布用的一包：标题候选、话题标签、封面描述。"""

    title_candidates: List[str] = Field(default_factory=list, max_length=6)
    tags: List[str] = Field(default_factory=list, max_length=8)
    cover_prompt: str = Field(default="", max_length=512)


# 素材的二分：一张图还是一段视频，由文件头魔数判出来，与方舟的入参取值无关。
MaterialKind = Literal["image", "video"]


class MaterialAssetOut(BaseModel):
    """素材库里的一条图片/视频素材。

    ``url`` 由 ``rel_path`` 现拼（不入库）：拼不出空串就说明文件已经不在盘上了，
    这时候界面上给一张裂开的缩略图不如什么都不给。``thumb_url`` 同理由
    ``rel_path`` 现拼，还多一层「没有就当场生成」——生成不了也是空串，界面回退
    到 ``url``。``tag`` 是打标分组，``None`` 表示没打标（按时间分组展示）。
    """

    id: int
    kind: MaterialKind
    name: str
    tag: Optional[str] = None
    mime: str = ""
    size: int = 0
    url: str = ""
    thumb_url: str = ""
    created_at: Optional[datetime] = None


class MaterialTagIn(BaseModel):
    """改打标分组的一次请求正文。空串表示把标签摘掉，归一回「按时间分组」。"""

    tag: str = Field(default="", max_length=128)


class MaterialRenameIn(BaseModel):
    """给素材改名。名字是列表里唯一认得出的标签，空白名字不允许。"""

    name: str = Field(min_length=1, max_length=128)


class MaterialBatchTagIn(BaseModel):
    """一批素材一次性改成同一个分组：整组改名不再发 N 次请求。"""

    ids: List[int] = Field(min_length=1, max_length=200)
    tag: str = Field(default="", max_length=128)


class MaterialIdsIn(BaseModel):
    """按 id 批量查询引用影响（删除前给用户看）。"""

    ids: List[int] = Field(default_factory=list, max_length=200)


class MaterialTagResult(BaseModel):
    """批量分组后的最小回执：前端按 id 原地改标签，不必整页重取。"""

    id: int
    tag: Optional[str] = None


class MaterialBatchTagOut(BaseModel):
    items: List[MaterialTagResult] = Field(default_factory=list)


class MaterialBatchDeleteOut(BaseModel):
    """删除回执：删除先进回收期，``references`` 是仍在引用它的镜头数。

    ``undo_seconds`` 由配置给出，界面照它说「N 分钟内可撤销」，不自己猜时限。
    """

    deleted: int = 0
    references: int = 0
    undo_seconds: int = 0


class MaterialBatchRestoreOut(BaseModel):
    """撤销删除的回执：真正恢复的条数（过期或超出账号上限的不在其中）。"""

    restored: int = 0


class MaterialUsageShot(BaseModel):
    id: int
    sort_order: int


class MaterialUsageProject(BaseModel):
    project_id: int
    title: str
    shots: List[MaterialUsageShot] = Field(default_factory=list)


class MaterialUsageOut(BaseModel):
    """一条素材被哪些短视频的哪些镜头引用：删除前的影响面。"""

    asset_id: int
    projects: List[MaterialUsageProject] = Field(default_factory=list)


class MaterialListOut(BaseModel):
    """一屏素材 + 两个总量。

    ``total`` 跟着筛选条件走，分页才数得对；``bytes_total`` 与 ``max_per_owner`` 是
    全库口径，页面用它说「还能再放多少条」——用户真正会问的是这个，而不是「这次
    筛出来的 8 条一共多大」。
    """

    items: List[MaterialAssetOut] = Field(default_factory=list)
    total: int = 0
    bytes_total: int = 0
    max_per_owner: int = 0
    # 删除后的撤销窗口：界面照它写「N 分钟内可撤销」，与后端配置同一口径。
    trash_ttl_seconds: int = 0


# ---- 人物库 -----------------------------------------------------------------


class CharacterOut(BaseModel):
    """人物库里的一个角色：文字档案 + 一张专属形象图。

    ``url`` / ``thumb_url`` 都由 ``rel_path`` 现拼（不入库）：拼不出给空串，界面
    按「形象图已丢失」处理而不是塞一个会 404 的地址。
    """

    id: int
    name: str
    persona: str = ""
    outfit: str = ""
    features: str = ""
    mime: str = ""
    size: int = 0
    url: str = ""
    thumb_url: str = ""
    created_at: Optional[datetime] = None


class CharacterListOut(BaseModel):
    """一屏角色 + 筛选后的总数与账号条数上限。"""

    items: List[CharacterOut] = Field(default_factory=list)
    total: int = 0
    max_per_owner: int = 0


class CharacterUpdateIn(BaseModel):
    """改角色档案：给了才改，``name`` 给了就不能是空白（服务层再兜一次）。"""

    name: Optional[str] = Field(default=None, max_length=64)
    persona: Optional[str] = Field(default=None, max_length=512)
    outfit: Optional[str] = Field(default=None, max_length=512)
    features: Optional[str] = Field(default=None, max_length=512)


class CharacterIdsIn(BaseModel):
    """删除前查引用影响的一批 id；条数随账号上限走，这里只挡「一次请求塞爆」。"""

    ids: List[int] = Field(default_factory=list, max_length=50)


class CharacterUsageProject(BaseModel):
    project_id: int
    title: str


class CharacterUsageOut(BaseModel):
    """一个角色被哪些短视频项目选进了角色组。"""

    character_id: int
    projects: List[CharacterUsageProject] = Field(default_factory=list)


class CharacterDeleteOut(BaseModel):
    """硬删除回执：``references`` 是仍引用该角色的项目数，仅用于界面提示。"""

    id: int
    references: int = 0


class MediaJobOut(BaseModel):
    """一条媒体作业的对外形状。

    ``url`` 是从 ``rel_path`` 拼出来的站内地址：上游那条 24 小时就失效，把它发给
    浏览器等于给用户一个明天就打不开的作品。
    """

    id: int
    kind: MediaJobKind
    status: MediaJobStatus
    model_name: str = ""
    url: str = ""
    mime: Optional[str] = None
    file_size: Optional[int] = None
    error_msg: Optional[str] = None
    elapsed_ms: Optional[int] = None
    # 链式渲染：非空表示这条片段作业在等同一项目的上一镜尾帧。前端据此把干等的
    # queued 显示成「等待第 N 镜」而不是干巴巴的排队中。
    chain_wait_shot: Optional[int] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class VideoShotOut(BaseModel):
    """工作台里的一镜：两套提示词 + 最近一次首帧与片段作业的现状。

    两个指针各带一条作业，状态与产物路径都从作业行读，镜头行上不抄第二份。
    """

    id: int
    sort_order: int = 0
    start_s: int = 0
    duration_s: int = 5
    scene: str = ""
    camera: str = ""
    action: str = ""
    dialogue: str = ""
    on_screen_text: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    gen_mode: VideoGenMode = "first_frame"
    # 引用的素材库 id（字符串形式，元素最多 4 个）。
    material_ids: List[str] = Field(default_factory=list)
    image_job: Optional[MediaJobOut] = None
    video_job: Optional[MediaJobOut] = None


class VideoShotIn(BaseModel):
    """整批保存时的一行。``id`` 空是新建，不在列表里的现有镜头会被删掉——
    与幻灯片整包保存同一语义，前端的增删改排序只需要发一份完整列表。

    时长区间取各型号窗口的并集：2.0 系是 4-15、2.5 到 30，具体型号能到哪一档由
    作业在上游校验，这里只挡明显荒谬的值。
    """

    id: Optional[int] = None
    duration_s: int = Field(default=5, ge=2, le=30)
    scene: str = Field(default="", max_length=512)
    camera: str = Field(default="", max_length=256)
    action: str = Field(default="", max_length=256)
    dialogue: str = Field(default="", max_length=512)
    on_screen_text: str = Field(default="", max_length=256)
    image_prompt: str = Field(default="", max_length=1024)
    video_prompt: str = Field(default="", max_length=1024)
    gen_mode: VideoGenMode = "first_frame"
    # 本镜引用的素材库 id，最多 4 条：方舟的 reference_image 到 4 张左右开始互相
    # 干扰，再给只是让提示词说不清哪张是主体。
    material_ids: List[str] = Field(default_factory=list, max_length=4)


class VideoScriptStepOut(BaseModel):
    """脚本生成里的一步。只报状态与耗时：prompt、产物、轨迹都在运行详情页，
    工作台每几秒取一次详情，不该把那些大字段跟着来回搬。
    """

    key: str
    status: str
    elapsed_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class VideoScriptRunOut(BaseModel):
    """认领本项目的那条脚本运行。

    这条流程是两步串行（创作卡 → 分镜），AI 网关的日志按**一次调用**分行，只看
    其中一行会以为整个生成就是那几十秒。这里给整条的分解，口径与运行详情页一致。
    """

    id: int
    status: str
    elapsed_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    steps: List[VideoScriptStepOut] = Field(default_factory=list)


class VideoStageProgress(BaseModel):
    """一类镜头作业的当前进度；没有作业指针的镜头数由调用方按总数减去各状态得出。"""

    total: int = 0
    succeeded: int = 0
    active: int = 0
    failed: int = 0
    cancelled: int = 0
    expired: int = 0


class VideoProjectItem(BaseModel):
    """列表里的一行。``status`` 是脚本阶段状态，首帧、片段与成片分别汇总。"""

    id: int
    title: str
    platform: VideoPlatform = "douyin"
    status: str = "analyzing"
    error_msg: Optional[str] = None
    shot_count: int = 0
    duration_s: int = 0
    cover_url: str = ""
    frames: VideoStageProgress = Field(default_factory=VideoStageProgress)
    clips: VideoStageProgress = Field(default_factory=VideoStageProgress)
    # 最近一次发布作业（成片合成 + 手动发布包）；从没合成过就是 None。
    publish_job: Optional[PublishJobOut] = None
    created_at: datetime
    updated_at: datetime


class VideoProjectDetail(VideoProjectItem):
    """工作台要的完整数据：创作卡 + 分镜 + 参数 + 每镜的作业现状。"""

    idea: str = ""
    requirement: str = ""
    brief: VideoBrief = Field(default_factory=VideoBrief)
    script: str = ""
    style: VideoStyle = Field(default_factory=VideoStyle)
    params: VideoParams = Field(default_factory=VideoParams)
    publish: VideoPublish = Field(default_factory=VideoPublish)
    # 项目角色组（video_character.id 的字符串形式）。有效条数上限由服务层按
    # config.CHARACTER_MAX_PER_PROJECT 裁，这里只挡「一次请求塞爆」。
    character_ids: List[str] = Field(default_factory=list, max_length=10)
    shots: List[VideoShotOut] = Field(default_factory=list)
    # 最近一次脚本生成的现场；还没生成过（老项目直接改分镜）时为 None。
    script_run: Optional[VideoScriptRunOut] = None


class VideoProjectCreate(BaseModel):
    """新建项目：一句话想法进来，创作卡与分镜由那条内置流程产出。"""

    title: Optional[str] = Field(default=None, max_length=128)
    platform: VideoPlatform = "douyin"
    idea: str = Field(..., min_length=1, max_length=4000)
    # 想直接指定成片时长就填这里；不填由策划师按选题定。
    duration_s: Optional[int] = Field(default=None, ge=5, le=60)
    requirement: Optional[str] = Field(default=None, max_length=512)
    style: Optional[VideoStyle] = None
    params: Optional[VideoParams] = None
    # 出场角色：建档时就选好，脚本生成会带上角色档案。未知 id 由服务层丢弃。
    character_ids: List[str] = Field(default_factory=list, max_length=10)


class VideoProjectUpdate(BaseModel):
    """工作台的保存：给了才改，``None`` 保持原样。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    platform: Optional[VideoPlatform] = None
    idea: Optional[str] = Field(default=None, max_length=4000)
    requirement: Optional[str] = Field(default=None, max_length=512)
    brief: Optional[VideoBrief] = None
    script: Optional[str] = Field(default=None, max_length=20000)
    style: Optional[VideoStyle] = None
    params: Optional[VideoParams] = None
    publish: Optional[VideoPublish] = None
    character_ids: Optional[List[str]] = Field(default=None, max_length=10)


class VideoShotsSave(BaseModel):
    # 上限比 settings.VIDEO_MAX_SHOTS 宽：那条是「模型能产出多少镜」的闸门，
    # 这里是「别把一次请求塞进几百行」的闸门，服务层再按前者裁。
    shots: List[VideoShotIn] = Field(default_factory=list, max_length=60)


class VideoRenderRequest(BaseModel):
    """一次批量渲染：``shot_ids`` 空表示项目里全部镜头。

    阶段由路径决定，正文里再放一个「阶段」字段就有了两处事实。片段那一档用
    :class:`VideoClipRenderRequest` 往下扩一个开关，基类只留两档都要的部分。
    """

    shot_ids: List[int] = Field(default_factory=list, max_length=60)


class VideoClipRenderRequest(VideoRenderRequest):
    """片段渲染多一个开关：把本镜上一版成片当参考视频一起送上去。

    单独一种形状而不是塞进 ``VideoRenderRequest``：首帧那一档没有「上一版片段」可参考，
    共用一个正文就等于让 ``/frames`` 收下一个它永远不读的字段。
    """

    reference_previous: bool = False


class MediaCapacityItem(BaseModel):
    """一类作业的并发现状：渲染前那屏「现在有几个在跑、还能塞几个」。"""

    kind: MediaJobKind
    inflight: int = 0
    limit: int = 0


class MediaCapacityOut(BaseModel):
    jobs: List[MediaCapacityItem] = Field(default_factory=list)
    # 参考图送进方舟的方式：data_url 时首帧必须落在服务器本地，换公网地址要配
    # PUBLIC_MEDIA_BASE_URL，界面上得说清当前是哪一种。
    image_transport: str = "data_url"
    # 「参考上一版片段」能不能用：方舟的视频参考只收公网 URL，没配 PUBLIC_MEDIA_BASE_URL
    # 就一条也发不出去，界面上那个勾选框据此禁用。
    video_reference_ready: bool = False


# 发布作业的状态与通道。通道目前只开 manual（手动发布包），Literal 先只放它，
# open_api / browser 接入时再扩——前端按字面量联合收窄，比 str 更早发现拼错。
PublishJobStatus = Literal["queued", "running", "succeeded", "failed"]
PublishChannel = Literal["manual"]


class PublishJobOut(BaseModel):
    """一条发布作业的对外形状：合成状态与成片地址都从这一行读。

    详情接口只带项目**最近一次**发布作业（与封面作业同一口径）；历史按项目 id
    在表里都留着，等界面上需要「挑一版成片」时再开口子。
    """

    id: int
    platform: VideoPlatform = "douyin"
    channel: PublishChannel = "manual"
    status: PublishJobStatus = "queued"
    # 成片的站内地址，由 rel_path 现拼；还没合成成功时是空串。
    url: str = ""
    file_size: Optional[int] = None
    error_msg: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ---- 智能应用 · 创作（写小说 / 写文章）-----------------------------------------

# 裸字符串词表，与现有状态字段同一约定（不用 Enum 落库）。
WritingKind = Literal["novel", "article"]
# 大纲节点：小说有卷/章两种，文章只有段落。允许的层级关系由 service 按 kind 校验。
WritingNodeKind = Literal["volume", "chapter", "section"]
WritingExportFormat = Literal["md", "txt"]

# 与 config.WRITING_* 同值的 schema 级闸门：Pydantic 不读 Settings，这里写字面量。
_WRITING_BRIEF_MAX = 20_000
_WRITING_CONTENT_MAX = 30_000
_WRITING_OUTLINE_MAX = 120


def _clean_writing_title(value: Optional[str]) -> Optional[str]:
    """标题 strip 后必须非空：min_length 只数字符数，挡不住纯空格（同笔记标题）。"""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("标题不能为空")
    return value


class WritingWorkCreate(BaseModel):
    """新建作品：只建行不触发生成，参数先落库，生成动作在工作台显式触发。"""

    kind: WritingKind
    title: str = Field(..., min_length=1, max_length=128)
    brief: Optional[str] = Field(default=None, max_length=_WRITING_BRIEF_MAX)
    requirement: Optional[str] = Field(default=None, max_length=512)
    # 白名单在 service 里按 kind 清洗，这里只挡明显越界的整包请求。
    params: Dict[str, Any] = Field(default_factory=dict)

    _clean_title = field_validator("title")(_clean_writing_title)


class WritingWorkUpdate(BaseModel):
    """作品元数据的部分更新；显式传 null 的 requirement 会被清空（同笔记正文约定）。"""

    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    brief: Optional[str] = Field(default=None, max_length=_WRITING_BRIEF_MAX)
    requirement: Optional[str] = Field(default=None, max_length=512)
    params: Optional[Dict[str, Any]] = None

    _clean_title = field_validator("title")(_clean_writing_title)


class OutlineNodeIn(BaseModel):
    """前端提交的大纲节点。``cid`` 可选：新加的节点还没有，由服务端发放。"""

    cid: Optional[str] = Field(default=None, max_length=32)
    # 词表在 service 按作品 kind 校验，这里先收字符串以便回清晰的中文错误。
    kind: str = "chapter"
    title: str = Field(default="", max_length=128)
    synopsis: str = Field(default="", max_length=2000)
    target_words: Optional[int] = Field(default=None, ge=0)
    children: Optional[List["OutlineNodeIn"]] = None


class WritingOutlineSave(BaseModel):
    """整棵大纲的保存请求；service 清洗后用它对账 writing_part 行。"""

    nodes: List[OutlineNodeIn] = Field(default_factory=list, max_length=_WRITING_OUTLINE_MAX)


class WritingPartCreate(BaseModel):
    """手工加章/加段：标题可空，服务端回落「未命名章节/段落」。"""

    title: str = Field(default="", max_length=128)
    # 插在哪个叶子之后；不传 = 追加到末尾。
    after_cid: Optional[str] = Field(default=None, max_length=32)


class WritingPartSave(BaseModel):
    """正文块的自动保存：两字段都可选，没传的保持不动。"""

    title: Optional[str] = Field(default=None, max_length=128)
    content: Optional[str] = Field(default=None, max_length=_WRITING_CONTENT_MAX)


class OutlineNode(BaseModel):
    """落库后的大纲节点：cid 一定有值，children 只在卷节点上出现。"""

    cid: str
    kind: str
    title: str
    synopsis: str = ""
    target_words: Optional[int] = None
    children: List["OutlineNode"] = Field(default_factory=list)


class WritingPartOut(BaseModel):
    """一个正文块的对外形状。"""

    cid: str
    sort_order: int
    title: str
    content: str
    word_count: int
    created_at: datetime
    updated_at: datetime


class WritingWorkItem(BaseModel):
    """列表里的一行作品：不带 brief/outline/正文这类大字段。"""

    id: int
    kind: WritingKind
    title: str
    params: Dict[str, Any] = Field(default_factory=dict)
    part_count: int = 0
    word_count: int = 0
    created_at: datetime
    updated_at: datetime


class WritingWorkDetail(WritingWorkItem):
    """工作台要的完整数据：大纲树 + 全部正文块（按 sort_order 排好）。"""

    brief: str = ""
    requirement: Optional[str] = None
    outline: List[OutlineNode] = Field(default_factory=list)
    meta: Dict[str, Any] = Field(default_factory=dict)
    parts: List[WritingPartOut] = Field(default_factory=list)


OutlineNodeIn.model_rebuild()
OutlineNode.model_rebuild()
