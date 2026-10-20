"""Pydantic 请求 / 响应模式定义。"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Optional, Tuple, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    # 按到达顺序记录的回答流 [{type: text|step|reason, detail}]。历史回放据此重建
    # 交错的命令与解说；老消息为 None 时前端退回只渲染 content。
    timeline: Optional[List[dict]] = None
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


class McpServerItem(McpServerBase):
    id: int
    owner_id: int
    last_test_at: Optional[datetime] = None
    last_test_ok: Optional[bool] = None
    last_test_msg: Optional[str] = None
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
    # 参与 Auto 路由：进入 model=auto 的候选池。
    auto_eligible: bool = False
    auto_tags: Optional[List[str]] = None

    _clean_tags = field_validator("auto_tags")(_normalize_auto_tags)


class AiModelRouteUpdate(BaseModel):
    model_config = _ALLOW_MODEL_PREFIX

    model_name: Optional[str] = Field(default=None, max_length=128)
    channel_id: Optional[int] = None
    upstream_model: Optional[str] = Field(default=None, max_length=128)
    priority: Optional[int] = Field(default=None, ge=0, le=9999)
    enabled: Optional[bool] = None
    remark: Optional[str] = Field(default=None, max_length=512)
    auto_eligible: Optional[bool] = None
    auto_tags: Optional[List[str]] = None

    _clean_tags = field_validator("auto_tags")(_normalize_auto_tags)


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
    interview_count: int = 0
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
