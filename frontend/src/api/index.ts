import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios'
import { message } from 'ant-design-vue'
import { clearAccessToken, getAccessToken } from '@/utils/auth'

export interface ApiEnvelope<T = unknown> {
  code: number
  message: string
  data: T
}

/** 在 axios 自身配置之上追加的单请求级开关。 */
export interface RequestConfig extends AxiosRequestConfig {
  /** 屏蔽全局错误提示，让调用方自己决定如何呈现这个错误。 */
  skipErrorToast?: boolean
}

const LOGIN_PATH = '/login'

const http: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

/** token 已失效，清除后跳转回登录页。fetch 直连（流式下载/SSE）绕过 axios
 * 拦截器，也要用它，所以导出。 */
export function handleUnauthorized() {
  clearAccessToken()
  if (window.location.pathname !== LOGIN_PATH) {
    window.location.replace(LOGIN_PATH)
  }
}

http.interceptors.request.use((config) => {
  const accessToken = getAccessToken()
  if (accessToken) {
    config.headers.Authorization = accessToken
  }
  return config
})

http.interceptors.response.use(
  // 这里返回的是拆包后的 envelope payload，而不是 AxiosResponse，所以返回类型
  // 只能写成 `any`：真正的类型由下面的 `request()` 重新补上。
  (response): any => {
    const payload = response.data as ApiEnvelope | Blob | ArrayBuffer
    if (response.config.responseType === 'blob' || payload instanceof Blob) {
      return response
    }
    const envelope = payload as ApiEnvelope
    if (envelope && typeof envelope === 'object' && 'code' in envelope) {
      if (envelope.code !== 0) {
        if (envelope.code === 401) {
          handleUnauthorized()
        }
        if (!(response.config as RequestConfig).skipErrorToast) {
          message.error(envelope.message || '请求失败')
        }
        return Promise.reject(envelope)
      }
      return envelope.data
    }
    return payload
  },
  (error) => {
    if (error?.response?.status === 401) {
      handleUnauthorized()
    }
    const detail = error?.response?.data?.message || error.message || '网络错误'
    if (!(error?.config as RequestConfig | undefined)?.skipErrorToast) {
      message.error(detail)
    }
    return Promise.reject(error)
  },
)

export async function request<T = unknown>(config: RequestConfig): Promise<T> {
  const res = await http.request<unknown, T>(config)
  return res
}

// ---- 认证 ------------------------------------------------------------------

export interface UserInfo {
  user_id: number | null
  username: string | null
  nickname: string | null
  email: string | null
  phone: string | null
  project_id: number | null
  user_terminal: string | null
  roles: string[]
  permissions: string[]
  ops_write: boolean
}

export interface LoginResult {
  access_token: string
  user_info: UserInfo | null
}

export interface CaptchaResult {
  captcha_id: string
  /** data URI，可直接喂给 <img>。 */
  image: string
}

export const authApi = {
  captcha: () =>
    request<CaptchaResult>({
      url: '/auth/captcha',
      // 登录页自己处理加载失败（点击图片重试），跳过全局提示。
      skipErrorToast: true,
    }),
  login: (payload: {
    username: string
    password: string
    captcha_id: string
    captcha_code: string
  }) =>
    request<LoginResult>({
      url: '/auth/login',
      method: 'POST',
      data: payload,
      // 登录页会把失败原因内联显示出来，所以这里跳过全局提示。
      skipErrorToast: true,
    }),
  me: () => request<UserInfo>({ url: '/auth/me', skipErrorToast: true }),
  logout: () => request<null>({ url: '/auth/logout', method: 'POST', skipErrorToast: true }),
  changePassword: (payload: { old_password: string; new_password: string }) =>
    request<null>({ url: '/auth/password', method: 'POST', data: payload }),
}

// ---- 用户（仅管理员） ------------------------------------------------------

export interface UserItem {
  id: number
  username: string
  nickname: string | null
  email: string | null
  status: boolean
  is_admin: boolean
  ops_write: boolean
  last_login_at: string | null
  created_at: string
  updated_at: string
}

export const usersApi = {
  list: () => request<{ items: UserItem[]; total: number }>({ url: '/users' }),
  create: (payload: {
    username: string
    password: string
    nickname?: string
    email?: string
    is_admin?: boolean
    ops_write?: boolean
  }) => request<UserItem>({ url: '/users', method: 'POST', data: payload }),
  update: (
    id: number,
    payload: {
      nickname?: string | null
      email?: string | null
      is_admin?: boolean
      ops_write?: boolean
    },
  ) => request<UserItem>({ url: `/users/${id}`, method: 'PUT', data: payload }),
  setStatus: (id: number, status: boolean) =>
    request<UserItem>({ url: `/users/${id}/status`, method: 'PUT', data: { status } }),
  resetPassword: (id: number, newPassword: string) =>
    request<null>({
      url: `/users/${id}/password`,
      method: 'POST',
      data: { new_password: newPassword },
    }),
}

// ---- 知识库 ----------------------------------------------------------------

/** SPA 会专门处理而不是直接弹提示的业务错误码。 */
export const CODE_EMBEDDING_NOT_READY = 4001
export const CODE_INDEX_MODEL_MISMATCH = 4002
export const CODE_CHAT_NOT_READY = 4004

export interface KbProject {
  id: number
  name: string
  description?: string | null
  type_count: number
  document_count: number
  created_at: string
  updated_at: string
}

export interface KnowledgeType {
  id: number
  name: string
  description?: string | null
  project_id: number
  document_count: number
  created_at: string
  updated_at: string
}

export type DocumentStatus = 'pending' | 'indexing' | 'ready' | 'error'

export interface KnowledgeDocument {
  id: number
  name: string
  type_id: number | null
  size: number
  mime?: string | null
  status: DocumentStatus
  error_msg?: string | null
  chunk_count: number
  created_at: string
  updated_at: string
}

export interface IndexState {
  status: 'uninitialized' | 'indexing' | 'ready' | 'rebuilding' | 'error'
  collection_name: string | null
  vector_size: number
  doc_count: number
  chunk_count: number
  error_msg?: string | null
  /** 当前配置的向量模型，已经和建立索引时用的那个对不上了。 */
  model_mismatch: boolean
  updated_at: string | null
}

export interface SearchHit {
  chunk_id: number
  doc_id: number
  filename: string
  seq: number
  score: number
  content: string
}

export interface RebuildResult {
  started: boolean
  message: string
  doc_count: number
}

/** 保持可读性而不用引入解析依赖；和 chunking.py 一致。 */
export const SUPPORTED_UPLOAD_EXTENSIONS = [
  '.txt',
  '.md',
  '.markdown',
  '.csv',
  '.json',
  '.log',
  '.yaml',
  '.yml',
]

export const knowledgeApi = {
  /** ---- 项目 ---- */
  listProjects: () => request<{ items: KbProject[]; total: number }>({ url: '/knowledge/projects' }),
  createProject: (payload: { name: string; description?: string }) =>
    request<KbProject>({ url: '/knowledge/projects', method: 'POST', data: payload }),
  updateProject: (id: number, payload: { name?: string; description?: string }) =>
    request<KbProject>({ url: `/knowledge/projects/${id}`, method: 'PUT', data: payload }),
  deleteProject: (id: number) =>
    request<null>({ url: `/knowledge/projects/${id}`, method: 'DELETE' }),

  /** ---- 知识类型 ---- */
  listTypes: (projectId: number) =>
    request<{ items: KnowledgeType[]; total: number }>({
      url: '/knowledge/types',
      params: { project_id: projectId },
    }),
  createType: (payload: { name: string; project_id: number; description?: string }) =>
    request<KnowledgeType>({ url: '/knowledge/types', method: 'POST', data: payload }),
  updateType: (id: number, payload: { name?: string; description?: string }) =>
    request<KnowledgeType>({ url: `/knowledge/types/${id}`, method: 'PUT', data: payload }),
  deleteType: (id: number) =>
    request<null>({ url: `/knowledge/types/${id}`, method: 'DELETE' }),

  /** ---- 文档 ---- */
  listDocuments: (typeId: number, page?: number, pageSize?: number) =>
    request<{ items: KnowledgeDocument[]; total: number }>({
      url: `/knowledge/types/${typeId}/documents`,
      // 不传分页参数时后端全量返回（向后兼容）。
      params: page !== undefined && pageSize !== undefined ? { page, page_size: pageSize } : undefined,
    }),
  uploadDocument: (typeId: number, file: File, name?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (name) fd.append('name', name)
    return request<KnowledgeDocument>({
      url: `/knowledge/types/${typeId}/documents`,
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      // 这里只负责存下文本；切片与向量化都放在后台跑。
      timeout: 60000,
      skipErrorToast: true,
    })
  },
  deleteDocument: (id: number) =>
    request<null>({ url: `/knowledge/documents/${id}`, method: 'DELETE' }),
  reindexDocument: (id: number) =>
    request<KnowledgeDocument>({
      url: `/knowledge/documents/${id}/reindex`,
      method: 'POST',
      skipErrorToast: true,
    }),
  batchDeleteDocuments: (ids: number[]) =>
    request<{ requested: number; done: number }>({
      url: '/knowledge/documents/batch-delete',
      method: 'POST',
      data: { ids },
    }),
  batchReindexDocuments: (ids: number[]) =>
    request<{ requested: number; done: number }>({
      url: '/knowledge/documents/batch-reindex',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),

  /** ---- 索引 ---- */
  indexState: () => request<IndexState>({ url: '/knowledge/index/state' }),
  rebuildIndex: () =>
    request<RebuildResult>({
      url: '/knowledge/index/rebuild',
      method: 'POST',
      skipErrorToast: true,
    }),

  /** ---- 检索 ---- */
  search: (payload: {
    query: string
    top_k?: number
    project_id?: number
    type_ids?: number[]
    score_threshold?: number
  }) =>
    request<{ hits: SearchHit[]; total: number }>({
      url: '/knowledge/search',
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
}

// ---- 设置 ------------------------------------------------------------------

export type ModelPurpose = 'chat' | 'embedding'

export interface ModelConfig {
  id: number
  purpose: ModelPurpose
  title: string
  model_name: string
  base_url: string
  /** 掩码形式（sk-****abcd）；原样提交回来不会改动已保存的密钥。 */
  api_key: string
  api_key_error?: string | null
  remark?: string | null
  is_default: boolean
  vector_size?: number | null
  last_tested_at?: string | null
  last_test_ok: boolean
  last_test_error?: string | null
  created_at: string
  updated_at: string
}

export interface ModelConfigPayload {
  purpose: ModelPurpose
  title: string
  model_name: string
  base_url: string
  api_key?: string
  remark?: string | null
}

export interface TestResult {
  success: boolean
  message: string
  vector_size?: number | null
}

export interface PurposeStatus {
  ready: boolean
  model: string | null
  config_id: number | null
}

export interface ReadinessStatus {
  chat: PurposeStatus
  embedding: PurposeStatus
  kb: {
    status: string
    doc_count: number
    chunk_count: number
    model_mismatch: boolean
  }
}

export const settingsApi = {
  listModels: (purpose?: ModelPurpose) =>
    request<{ items: ModelConfig[]; total: number }>({
      url: '/settings/models',
      params: purpose ? { purpose } : undefined,
    }),
  createModel: (payload: ModelConfigPayload) =>
    request<ModelConfig>({ url: '/settings/models', method: 'POST', data: payload }),
  updateModel: (id: number, payload: Partial<ModelConfigPayload>) =>
    request<ModelConfig>({ url: `/settings/models/${id}`, method: 'PUT', data: payload }),
  deleteModel: (id: number) =>
    request<null>({ url: `/settings/models/${id}`, method: 'DELETE' }),
  setDefault: (id: number) =>
    request<ModelConfig>({ url: `/settings/models/${id}/default`, method: 'POST' }),
  testModel: (id: number) =>
    request<TestResult>({
      url: `/settings/models/${id}/test`,
      method: 'POST',
      // 探测远端服务商可能很慢，全局的 30 秒太紧了。
      timeout: 60000,
      skipErrorToast: true,
    }),
  status: () => request<ReadinessStatus>({ url: '/settings/models/status' }),
}

// ---- 智能体 ----------------------------------------------------------------

export interface Agent {
  id: number
  slug: string
  name: string
  description?: string | null
  system_prompt: string
  use_knowledge: boolean
  use_ops: boolean
  use_memory: boolean
  chat_visible: boolean
  temperature: number
  is_builtin: boolean
  enabled: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

export interface AgentPayload {
  slug: string
  name: string
  description?: string | null
  system_prompt: string
  use_knowledge?: boolean
  use_ops?: boolean
  use_memory?: boolean
  chat_visible?: boolean
  temperature?: number
  sort_order?: number
}

export const agentsApi = {
  list: (enabledOnly = false) =>
    request<{ items: Agent[]; total: number }>({
      url: '/agents',
      params: enabledOnly ? { enabled_only: true } : undefined,
    }),
  create: (payload: AgentPayload) =>
    request<Agent>({ url: '/agents', method: 'POST', data: payload }),
  update: (id: number, payload: Partial<AgentPayload> & { enabled?: boolean }) =>
    request<Agent>({ url: `/agents/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/agents/${id}`, method: 'DELETE' }),
  duplicate: (id: number) =>
    request<Agent>({ url: `/agents/${id}/duplicate`, method: 'POST' }),
}

// ---- 长期记忆 ----------------------------------------------------------------

export interface MemoryItem {
  id: number
  content: string
  source_conversation_id: number | null
  created_at: string
  updated_at: string
}

export const memoryApi = {
  list: () => request<{ items: MemoryItem[]; total: number }>({ url: '/memory' }),
  create: (content: string) =>
    request<MemoryItem>({ url: '/memory', method: 'POST', data: { content } }),
  update: (id: number, content: string) =>
    request<MemoryItem>({ url: `/memory/${id}`, method: 'PUT', data: { content } }),
  remove: (id: number) => request<null>({ url: `/memory/${id}`, method: 'DELETE' }),
  clear: () => request<null>({ url: '/memory', method: 'DELETE' }),
}

// ---- 对话 ------------------------------------------------------------------

export interface Conversation {
  id: number
  title: string
  agent_id: number | null
  /** 该会话所依据的检索范围；早于项目功能的老会话为 null。 */
  project_id: number | null
  /** 为空或 null 表示「项目下的所有类型」。 */
  type_ids: number[] | null
  /** 本会话选用的对话模型配置；null 表示跟随设置里的默认对话模型。 */
  model_config_id: number | null
  message_count: number
  created_at: string
  updated_at: string
}

export interface Citation {
  index: number
  doc_id: number
  chunk_id: number
  filename: string
  score: number
  excerpt: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  created_at: string
}

/** 运维写命令待确认项。status 是后端换算后的展示状态（超时的 pending 报 expired）。 */
export type OpsActionStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'executed'
  | 'failed'

export interface OpsAction {
  id: number
  conversation_id: number
  /** 触发该提议的 assistant 消息；确认卡片靠它挂到对应气泡下面。 */
  message_id: number | null
  target_type: string
  target_id: number
  target_name: string | null
  command: string
  reason: string | null
  status: OpsActionStatus
  result: string | null
  exit_status: number | null
  timeout_seconds: number
  created_at: string
  resolved_at: string | null
}

/** completions 流内的 confirm 事件：一条新的待确认写命令。 */
export interface ConfirmEvent {
  action_id: number
  command: string
  reason: string | null
  target_type: string
  target_id: number
  target_name: string | null
  timeout_seconds: number
}

/** exec 事件：一条 AI 命令的执行结果。confirm 流里带被确认 action 的 id；
 *  completions 流里是只读命令的直接执行，action_id 为 null。 */
export interface ExecEvent {
  action_id: number | null
  command: string
  exit_status: number | null
  output: string
  elapsed_ms: number
  success: boolean
}

/** 由 SSE 事件流驱动的一组回调。 */
export interface StreamHandlers {
  onMeta?: (data: {
    conversation_id: number
    agent_id: number
    project_id: number | null
    type_ids: number[] | null
    model_config_id: number | null
  }) => void
  onCitations?: (citations: Citation[]) => void
  onStep?: (data: { tool: string; query: string; input?: Record<string, unknown> }) => void
  onToken?: (token: string) => void
  onConfirm?: (data: ConfirmEvent) => void
  onExec?: (data: ExecEvent) => void
  onDone?: (data: { message_id: number; conversation_id: number }) => void
  onError?: (data: { code: number; message: string }) => void
}

export const chatApi = {
  listConversations: () =>
    request<{ items: Conversation[]; total: number }>({ url: '/chat/conversations' }),
  createConversation: (
    payload: {
      title?: string
      agent_id?: number
      project_id?: number
      type_ids?: number[]
      model_config_id?: number
    } = {},
  ) => request<Conversation>({ url: '/chat/conversations', method: 'POST', data: payload }),
  updateConversation: (
    id: number,
    payload: {
      title?: string
      agent_id?: number
      project_id?: number
      type_ids?: number[]
      model_config_id?: number
    },
  ) =>
    request<Conversation>({
      url: `/chat/conversations/${id}`,
      method: 'PUT',
      data: payload,
    }),
  deleteConversation: (id: number) =>
    request<null>({ url: `/chat/conversations/${id}`, method: 'DELETE' }),
  listMessages: (id: number) =>
    request<{ items: ChatMessage[]; total: number }>({
      url: `/chat/conversations/${id}/messages`,
    }),
  /** 改写一条助手消息的正文（手动改图的落库通道；改后历史即随之变化）。 */
  updateMessage: (id: number, content: string) =>
    request<ChatMessage>({ url: `/chat/messages/${id}`, method: 'PUT', data: { content } }),

  /**
   * 以 stream 方式获取回答。
   *
   * 这里刻意绕开上面那个 axios 实例：它的拦截器会去拆一层 JSON envelope，
   * 而 token stream 根本没有这层结构。改用 fetch 加 ReadableStream reader。
   * 返回一个用于中断的函数。
   */
  streamCompletion(
    payload: {
      message: string
      conversation_id?: number
      agent_id?: number
      /** 对于需要检索知识库的智能体，后端要求必须带上这个字段。 */
      project_id?: number
      /** 不传表示「项目下的所有类型」。 */
      type_ids?: number[]
      /** 点名用哪份对话模型配置；不传则沿用会话记住的或默认配置。 */
      model_config_id?: number
    },
    handlers: StreamHandlers,
  ): () => void {
    return streamSse('/api/chat/completions', payload, handlers)
  },

  /**
   * 重试会话里最后一问：后端先删掉那条没得到回答的用户消息再重新回答，
   * 历史里不会出现重复提问。事件流格式与 streamCompletion 相同。
   */
  streamRetry(conversationId: number, handlers: StreamHandlers): () => void {
    return streamSse(`/api/chat/conversations/${conversationId}/retry`, undefined, handlers)
  },

  // ---- 运维问答会话与写命令确认 ------------------------------------------------

  /** 某个运维目标的全部历史问答会话（最近活跃的在前）：面板的历史下拉用。 */
  listOpsConversations: (targetType: 'server' | 'database', targetId: number) =>
    request<{ items: Conversation[]; total: number }>({
      url: '/chat/ops-conversations',
      params: { target_type: targetType, target_id: targetId },
    }),
  /**
   * 为运维目标新开一个问答会话。面板是懒创建：打开就是新窗口，第一条提问
   * 发出去时才调这里落库，免得历史列表堆满一句话都没说的空会话。
   */
  createOpsConversation: (targetType: 'server' | 'database', targetId: number, title?: string) =>
    request<Conversation>({
      url: '/chat/ops-conversations',
      method: 'POST',
      data: { target_type: targetType, target_id: targetId, title },
    }),
  /** 一个会话的全部待确认项（含已处理的）：历史回放时确认卡片靠它恢复。 */
  listActions: (conversationId: number) =>
    request<{ items: OpsAction[]; total: number }>({
      url: `/chat/conversations/${conversationId}/actions`,
    }),
  /**
   * 确认一条待执行命令：认领 → 执行 → 基于结果续答，全程 SSE。
   * 事件序列固定为 exec → token* → done|error。
   */
  confirmAction(actionId: number, handlers: StreamHandlers): () => void {
    return streamSse(`/api/chat/actions/${actionId}/confirm`, undefined, handlers)
  },
  rejectAction: (actionId: number) =>
    request<OpsAction>({ url: `/chat/actions/${actionId}/reject`, method: 'POST' }),
}

/**
 * SSE 拉流的公共实现。
 *
 * 这里刻意绕开上面那个 axios 实例：它的拦截器会去拆一层 JSON envelope，
 * 而 token stream 根本没有这层结构。改用 fetch 加 ReadableStream reader。
 * 返回一个用于中断的函数。
 */
function streamSse(url: string, body: unknown, handlers: StreamHandlers): () => void {
  const controller = new AbortController()

  void (async () => {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(getAccessToken() ? { Authorization: getAccessToken() as string } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      })

      if (response.status === 401) {
        handleUnauthorized()
        return
      }
      if (!response.ok || !response.body) {
        handlers.onError?.({ code: response.status, message: '连接失败，请稍后重试' })
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        // 事件之间用空行分隔；最后一个空行之后的内容属于还没收完的半个
        // 事件，先留在 buffer 里等下一批数据。
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''

        for (const block of blocks) {
          const event = parseSseBlock(block)
          if (event) dispatchSse(event, handlers)
        }
      }
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') return
      handlers.onError?.({ code: -1, message: (err as Error)?.message || '网络错误' })
    }
  })()

  return () => controller.abort()
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = ''
  let raw = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) raw = line.slice(5).trim()
  }
  if (!event) return null
  try {
    return { event, data: raw ? JSON.parse(raw) : null }
  } catch {
    return null
  }
}

function dispatchSse(
  { event, data }: { event: string; data: any },
  handlers: StreamHandlers,
) {
  switch (event) {
    case 'meta':
      handlers.onMeta?.(data)
      break
    case 'citations':
      handlers.onCitations?.(data ?? [])
      break
    case 'step':
      handlers.onStep?.(data ?? { tool: '', query: '' })
      break
    case 'token':
      handlers.onToken?.(String(data ?? ''))
      break
    case 'confirm':
      handlers.onConfirm?.(data)
      break
    case 'exec':
      handlers.onExec?.(data)
      break
    case 'done':
      handlers.onDone?.(data)
      break
    case 'error':
      handlers.onError?.(data)
      break
  }
}

// ---- 智能运维 --------------------------------------------------------------

export const CODE_OPS_FORBIDDEN = 4101
export const CODE_OPS_CONNECT_FAILED = 4102
export const CODE_OPS_EXEC_FAILED = 4103

export type ServerAuthType = 'password' | 'key'
export type DatabaseType = 'mysql' | 'redis'

export interface OpsServer {
  id: number
  name: string
  host: string
  port: number
  username: string
  auth_type: ServerAuthType
  /** 掩码形式；原样提交回来不会改动已保存的凭据。 */
  credential: string
  credential_error?: string | null
  host_key_pinned: boolean
  remark?: string | null
  last_checked_at?: string | null
  last_check_ok: boolean
  last_check_error?: string | null
  created_at: string
  updated_at: string
}

export interface OpsServerPayload {
  name: string
  host: string
  port?: number
  username: string
  auth_type?: ServerAuthType
  password?: string
  private_key?: string
  passphrase?: string
  remark?: string | null
}

export interface OpsDatabase {
  id: number
  name: string
  db_type: DatabaseType
  host: string
  port: number
  username?: string | null
  password: string
  password_error?: string | null
  db_name?: string | null
  /** 控制台写开关：开了之后手敲通道可执行 DML/DDL（AI 通道始终只读）。 */
  writable: boolean
  /** 展示色（#rrggbb）：树节点整行与页签头的底色；null 不染。 */
  color?: string | null
  remark?: string | null
  last_checked_at?: string | null
  last_check_ok: boolean
  last_check_error?: string | null
  created_at: string
  updated_at: string
}

export interface OpsDatabasePayload {
  name: string
  db_type?: DatabaseType
  host: string
  port?: number
  username?: string | null
  password?: string
  db_name?: string | null
  writable?: boolean
  color?: string | null
  remark?: string | null
}

export interface DbExecuteResult {
  columns: string[]
  rows: unknown[][]
  /** columns 为空时读这里：不是结果集的回复（Redis 标量等）。 */
  text?: string | null
  row_count: number
  truncated: boolean
  elapsed_ms: number
}

/** 批量执行里单条语句的结果（Navicat 式逐句执行）。 */
export interface DbBatchStatement {
  /** 1-based，消息列表 / 结果页签 / 摘要按它对齐。 */
  index: number
  /** 单行预览（截断 200 字符），完整脚本在审计记录里。 */
  sql: string
  status: 'ok' | 'error'
  /** 成功："N 行" / "执行完成"；失败：错误原因。 */
  message: string
  elapsed_ms: number
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  text?: string | null
}

/** 一段脚本的批量执行结果。单句出错不中断，逐句成败都在这里。 */
export interface DbBatchResult {
  statements: DbBatchStatement[]
  total: number
  succeeded: number
  failed: number
  started_at: string
  finished_at: string
  elapsed_ms: number
}

export interface WsTicket {
  ticket: string
  expires_in: number
}

export interface SqlFavorite {
  id: number
  /** null 表示所有连接通用的收藏。 */
  database_id: number | null
  title: string
  content: string
  remark?: string | null
  created_at: string
  updated_at: string
}

export interface SqlFavoritePayload {
  title: string
  content: string
  database_id?: number | null
  remark?: string | null
}

// ---- 数据库浏览（Navicat 式界面的数据源） --------------------------------------

export interface DbSchemaItem {
  name: string
  kind: 'schema' | 'redisdb'
  object_count?: number | null
}

export interface DbTableItem {
  name: string
  table_type: string
  engine?: string | null
  rows_estimate?: number | null
  comment?: string | null
}

export interface DbColumnItem {
  name: string
  column_type: string
  nullable: boolean
  column_key: string
  default?: string | null
  extra: string
  comment: string
}

export interface DbCompletionColumn {
  name: string
  column_type: string
}

/** 查询编辑器补全用的「表 → 列」映射，一个库一次拉齐。 */
export interface DbCompletionTable {
  name: string
  columns: DbCompletionColumn[]
}

export interface DbRowsResult {
  columns: string[]
  rows: unknown[][]
  total: number
  page: number
  page_size: number
  elapsed_ms: number
  /** 展示用：产生这一页的 SELECT 文本（参数已代回字面量），给底部 SQL 栏用。 */
  sql: string
}

/** 行级写的行定位：整行原值（列名 → 原值，null 表示 NULL），服务端自取主键子集。 */
export type DbRowKey = Record<string, unknown>

export interface DbRowWriteResult {
  affected: number
  elapsed_ms: number
}

export type DbRowFilterOp =
  | 'eq'
  | 'ne'
  | 'like'
  | 'not_like'
  | 'lt'
  | 'lte'
  | 'gt'
  | 'gte'
  | 'is_null'
  | 'is_not_null'

export interface DbRowFilter {
  column: string
  op: DbRowFilterOp
  value?: string | null
}

export interface DbRowSort {
  column: string
  direction: 'asc' | 'desc'
}

export interface RedisKeyItem {
  key: string
  key_type: string
}

export interface RedisScanResult {
  /** "0" 表示扫完了，否则把它带回下一页。 */
  cursor: string
  keys: RedisKeyItem[]
}

export interface RedisKeyDetail {
  key: string
  key_type: string
  /** -1 永不过期，-2 不存在。 */
  ttl: number
  /** string→字符串；list/set→string[]；hash/zset/stream→两列数组。 */
  value: unknown
  truncated: boolean
}

export interface OpsAuditItem {
  id: number
  target_type: string
  target_id: number
  target_name?: string | null
  actor: string
  command: string
  verdict: string
  success: boolean
  error?: string | null
  created_at: string
}

/** 远程文件浏览器里的一行。 */
export interface SftpEntry {
  name: string
  is_dir: boolean
  size: number
  mtime: number
  mode: number
}

export interface SftpListResult {
  path: string
  items: SftpEntry[]
  truncated: boolean
}

/** SFTP 下载地址。流式落盘走 fetch（不经 axios），URL 在这里集中拼。 */
export function sftpDownloadUrl(id: number, path: string): string {
  return `/api/ops/servers/${id}/files/download?path=${encodeURIComponent(path)}`
}

export const opsApi = {
  ticket: () => request<WsTicket>({ url: '/ops/ws-ticket', method: 'POST' }),

  listServers: (keyword?: string) =>
    request<{ items: OpsServer[]; total: number }>({
      url: '/ops/servers',
      params: keyword ? { keyword } : undefined,
    }),
  createServer: (payload: OpsServerPayload) =>
    request<OpsServer>({ url: '/ops/servers', method: 'POST', data: payload }),
  updateServer: (id: number, payload: Partial<OpsServerPayload>) =>
    request<OpsServer>({ url: `/ops/servers/${id}`, method: 'PUT', data: payload }),
  removeServer: (id: number) => request<null>({ url: `/ops/servers/${id}`, method: 'DELETE' }),
  testServer: (id: number) =>
    request<TestResult>({
      url: `/ops/servers/${id}/test`,
      method: 'POST',
      // SSH 握手 + 一次 uname，慢起来比 30 秒的全局超时还长。
      timeout: 60000,
      skipErrorToast: true,
    }),

  // ---- 远程文件（SFTP 文件浏览器） ----------------------------------------------
  // 全部 skipErrorToast：错误由文件面板在自己的错误区里呈现，不再全局弹。

  sftpList: (id: number, path?: string) =>
    request<SftpListResult>({
      url: `/ops/servers/${id}/files/list`,
      params: path ? { path } : undefined,
      timeout: 60000,
      skipErrorToast: true,
    }),
  sftpDownload: (
    id: number,
    path: string,
    opts?: { onProgress?: (loaded: number, total?: number) => void; signal?: AbortSignal },
  ) =>
    // responseType:'blob' 时响应拦截器原样返回整个 AxiosResponse（含 headers），
    // 出错时后端给的是 JSON 信封，由 saveBlobResponse 识别。
    request<AxiosResponse<Blob>>({
      url: `/ops/servers/${id}/files/download`,
      params: { path },
      responseType: 'blob',
      // 大文件下载不该被全局 30 秒超时杀掉。
      timeout: 0,
      skipErrorToast: true,
      onDownloadProgress: (event) => opts?.onProgress?.(event.loaded, event.total),
      signal: opts?.signal,
    }),
  sftpUpload: (
    id: number,
    dir: string,
    file: File,
    opts?: { onProgress?: (loaded: number, total?: number) => void; signal?: AbortSignal },
  ) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<null>({
      url: `/ops/servers/${id}/files/upload`,
      method: 'POST',
      params: { path: dir },
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 0,
      skipErrorToast: true,
      onUploadProgress: (event) => opts?.onProgress?.(event.loaded, event.total ?? file.size),
      signal: opts?.signal,
    })
  },
  sftpMkdir: (id: number, path: string) =>
    request<null>({
      url: `/ops/servers/${id}/files/mkdir`,
      method: 'POST',
      data: { path },
      timeout: 60000,
      skipErrorToast: true,
    }),
  sftpRemove: (id: number, path: string) =>
    request<null>({
      url: `/ops/servers/${id}/files`,
      method: 'DELETE',
      params: { path },
      // 递归删除一棵大树可能远超 30 秒。
      timeout: 0,
      skipErrorToast: true,
    }),

  listDatabases: (params?: { keyword?: string; db_type?: DatabaseType }) =>
    request<{ items: OpsDatabase[]; total: number }>({ url: '/ops/databases', params }),
  createDatabase: (payload: OpsDatabasePayload) =>
    request<OpsDatabase>({ url: '/ops/databases', method: 'POST', data: payload }),
  updateDatabase: (id: number, payload: Partial<OpsDatabasePayload>) =>
    request<OpsDatabase>({ url: `/ops/databases/${id}`, method: 'PUT', data: payload }),
  removeDatabase: (id: number) => request<null>({ url: `/ops/databases/${id}`, method: 'DELETE' }),
  testDatabase: (id: number) =>
    request<TestResult>({
      url: `/ops/databases/${id}/test`,
      method: 'POST',
      timeout: 60000,
      skipErrorToast: true,
    }),
  execute: (id: number, command: string, schema?: string, opts?: { signal?: AbortSignal }) =>
    request<DbExecuteResult>({
      url: `/ops/databases/${id}/execute`,
      method: 'POST',
      data: { command, schema: schema || undefined },
      timeout: 60000,
      // 被安全网关挡下来也是一种正常结果，控制台自己把它打印出来。
      skipErrorToast: true,
      signal: opts?.signal,
    }),
  executeBatch: (id: number, command: string, schema?: string, opts?: { signal?: AbortSignal }) =>
    request<DbBatchResult>({
      url: `/ops/databases/${id}/execute/batch`,
      method: 'POST',
      data: { command, schema: schema || undefined },
      // 批量是逐句串行，一批可能远超单句的 60 秒。
      timeout: 0,
      skipErrorToast: true,
      signal: opts?.signal,
    }),

  // ---- 浏览（Navicat 式界面） --------------------------------------------------

  dbSchemas: (id: number) =>
    request<{ items: DbSchemaItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbTables: (id: number, schema: string) =>
    request<{ items: DbTableItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbColumns: (id: number, schema: string, table: string) =>
    request<{ items: DbColumnItem[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/columns`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbCompletion: (id: number, schema: string) =>
    request<{ items: DbCompletionTable[]; total: number }>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/completion`,
      timeout: 60000,
      skipErrorToast: true,
    }),
  dbRows: (
    id: number,
    schema: string,
    table: string,
    page: number,
    pageSize: number,
    filters?: DbRowFilter[],
    sorts?: DbRowSort[],
    where?: string,
  ) =>
    request<DbRowsResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      params: {
        page,
        page_size: pageSize,
        ...(filters?.length ? { filters: JSON.stringify(filters) } : {}),
        ...(sorts?.length ? { order_by: JSON.stringify(sorts) } : {}),
        ...(where?.trim() ? { where: where.trim() } : {}),
      },
      timeout: 60000,
      skipErrorToast: true,
    }),
  // 行级写：改值 / 删除记录。SQL 由服务端按主键生成，前端只回传原值与新值。
  updateRow: (id: number, schema: string, table: string, key: DbRowKey, sets: DbRowKey) =>
    request<DbRowWriteResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      method: 'PUT',
      data: { key, sets },
      timeout: 60000,
      skipErrorToast: true,
    }),
  deleteRow: (id: number, schema: string, table: string, key: DbRowKey) =>
    request<DbRowWriteResult>({
      url: `/ops/databases/${id}/schemas/${encodeURIComponent(schema)}/tables/${encodeURIComponent(table)}/rows`,
      method: 'DELETE',
      data: { key },
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisKeys: (id: number, db: number, cursor = '0', count = 100) =>
    request<RedisScanResult>({
      url: `/ops/databases/${id}/keys`,
      params: { db, cursor, count },
      timeout: 60000,
      skipErrorToast: true,
    }),
  redisKeyDetail: (id: number, db: number, key: string) =>
    request<RedisKeyDetail>({
      url: `/ops/databases/${id}/key`,
      params: { db, key },
      timeout: 60000,
      skipErrorToast: true,
    }),

  audit: (params?: {
    target_type?: 'server' | 'database'
    target_id?: number
    limit?: number
    offset?: number
  }) => request<{ items: OpsAuditItem[]; total: number }>({ url: '/ops/audit', params }),

  // ---- SQL 收藏 ------------------------------------------------------------

  listSqlFavorites: (databaseId?: number) =>
    request<{ items: SqlFavorite[]; total: number }>({
      url: '/ops/sql-favorites',
      params: databaseId ? { database_id: databaseId } : undefined,
    }),
  createSqlFavorite: (payload: SqlFavoritePayload) =>
    request<SqlFavorite>({ url: '/ops/sql-favorites', method: 'POST', data: payload }),
  updateSqlFavorite: (id: number, payload: Partial<SqlFavoritePayload>) =>
    request<SqlFavorite>({ url: `/ops/sql-favorites/${id}`, method: 'PUT', data: payload }),
  removeSqlFavorite: (id: number) =>
    request<null>({ url: `/ops/sql-favorites/${id}`, method: 'DELETE' }),
}

// ---- 智能应用 · 简历 ---------------------------------------------------------

export type ResumeStatus = 'uploaded' | 'analyzing' | 'ready' | 'error'

export interface ResumeItem {
  id: number
  title: string
  description: string | null
  filename: string
  mime: string | null
  size: number
  status: ResumeStatus
  error_msg: string | null
  has_report: boolean
  /** 原文件是否已同步到用户自己的百度网盘；为 true 才展示「下载原件」。 */
  has_netdisk: boolean
  analyzed_at: string | null
  created_at: string
  updated_at: string
}

export interface ResumeDetail extends ResumeItem {
  report: string | null
  suggestions: string[] | null
}

export interface ResumePreview {
  id: number
  title: string
  filename: string
  mime: string | null
  size: number
  content: string
}

export interface ResumeComparisonItem {
  id: number
  title: string | null
  resume_ids: number[]
  resume_titles: string[]
  status: 'analyzing' | 'ready' | 'error'
  error_msg: string | null
  created_at: string
}

export interface ResumeComparisonDetail extends ResumeComparisonItem {
  report: string | null
}

// ---- 智能应用 · 求职助手 -------------------------------------------------------

export type ToolkitKind =
  | 'optimize'
  | 'career-match'
  | 'jd-match'
  | 'interview-prep'
  | 'portfolio-plan'
  | 'salary-negotiation'

export interface ResumeToolkitCreate {
  kind: ToolkitKind
  /** 简历来源二选一：引用已上传简历或直接粘贴文本。 */
  resume_id?: number
  resume_text?: string
  job_description?: string
  position?: string
  background?: string
  offer_amount?: string
  notes?: string
}

export interface ResumeToolkitItem {
  id: number
  kind: ToolkitKind
  kind_label: string
  title: string
  status: 'analyzing' | 'ready' | 'error'
  error_msg: string | null
  created_at: string
}

export interface ResumeToolkitDetail extends ResumeToolkitItem {
  inputs: Record<string, unknown>
  report: string | null
}

export const resumeApi = {
  list: () => request<{ items: ResumeItem[]; total: number }>({ url: '/office/resumes' }),
  upload: (file: File, title?: string, description?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (title) fd.append('title', title)
    if (description) fd.append('description', description)
    return request<ResumeItem>({
      url: '/office/resumes',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
      skipErrorToast: true,
    })
  },
  detail: (id: number) => request<ResumeDetail>({ url: `/office/resumes/${id}` }),
  preview: (id: number) =>
    request<ResumePreview>({ url: `/office/resumes/${id}/preview`, skipErrorToast: true }),
  update: (id: number, payload: { title?: string; description?: string }) =>
    request<ResumeItem>({ url: `/office/resumes/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/office/resumes/${id}`, method: 'DELETE' }),
  analyze: (id: number) =>
    request<ResumeItem>({
      url: `/office/resumes/${id}/analyze`,
      method: 'POST',
      skipErrorToast: true,
    }),
  // responseType:'blob' 时响应拦截器原样返回整个 AxiosResponse；出错时后端
  // 给的是 JSON 信封，由 saveBlobResponse 识别并弹消息。
  download: (id: number) =>
    request<AxiosResponse<Blob>>({
      url: `/office/resumes/${id}/download`,
      responseType: 'blob',
      timeout: 0,
      skipErrorToast: true,
    }),

  compare: (resumeIds: number[], title?: string) =>
    request<ResumeComparisonItem>({
      url: '/office/comparisons',
      method: 'POST',
      data: { resume_ids: resumeIds, title: title || undefined },
      skipErrorToast: true,
    }),
  listComparisons: () =>
    request<{ items: ResumeComparisonItem[]; total: number }>({ url: '/office/comparisons' }),
  comparisonDetail: (id: number) =>
    request<ResumeComparisonDetail>({ url: `/office/comparisons/${id}` }),
  removeComparison: (id: number) =>
    request<null>({ url: `/office/comparisons/${id}`, method: 'DELETE' }),

  createToolkitTask: (payload: ResumeToolkitCreate) =>
    request<ResumeToolkitItem>({
      url: '/office/toolkit',
      method: 'POST',
      data: payload,
      skipErrorToast: true,
    }),
  listToolkitTasks: () =>
    request<{ items: ResumeToolkitItem[]; total: number }>({ url: '/office/toolkit' }),
  toolkitTaskDetail: (id: number) =>
    request<ResumeToolkitDetail>({ url: `/office/toolkit/${id}` }),
  removeToolkitTask: (id: number) =>
    request<null>({ url: `/office/toolkit/${id}`, method: 'DELETE' }),
}

// ---- 智能应用 · 面试记录 ---------------------------------------------------------

export type InterviewResult = 'pending' | 'passed' | 'failed' | 'offer'

/** 每题 AI 参考答案的生成状态。 */
export type InterviewRefStatus = 'none' | 'analyzing' | 'ready' | 'error'

export interface InterviewQuestionPayload {
  question: string
  my_answer?: string
  note?: string
}

/** 落库后的一条问题：比提交载荷多了 qid 与 AI 参考答案三件套，可空字段用 null。 */
export interface InterviewQuestion {
  qid: string
  question: string
  my_answer: string | null
  note: string | null
  ref_answer: string | null
  ref_status: InterviewRefStatus
  ref_error: string | null
}

export interface InterviewCreate {
  company: string
  position: string
  interview_date?: string
  round?: string
  result?: InterviewResult
  notes?: string
}

/** 元数据修改；questions 有题目级端点，不走这里（避免覆盖后台回写的 ref_*）。 */
export type InterviewUpdate = Partial<InterviewCreate>

export interface InterviewItem {
  id: number
  company: string
  position: string
  interview_date: string | null
  round: string | null
  result: InterviewResult
  notes: string | null
  question_count: number
  created_at: string
  updated_at: string
}

export interface InterviewDetail extends InterviewItem {
  questions: InterviewQuestion[]
}

export const interviewApi = {
  list: () => request<{ items: InterviewItem[]; total: number }>({ url: '/office/interviews' }),
  create: (payload: InterviewCreate) =>
    request<InterviewDetail>({ url: '/office/interviews', method: 'POST', data: payload }),
  detail: (id: number) => request<InterviewDetail>({ url: `/office/interviews/${id}` }),
  update: (id: number, payload: InterviewUpdate) =>
    request<InterviewDetail>({ url: `/office/interviews/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/office/interviews/${id}`, method: 'DELETE' }),

  // 题目级写操作统一返回整条详情，调用方直接整换本地数据。
  addQuestion: (id: number, payload: InterviewQuestionPayload) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions`,
      method: 'POST',
      data: payload,
    }),
  updateQuestion: (id: number, qid: string, payload: InterviewQuestionPayload) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}`,
      method: 'PUT',
      data: payload,
    }),
  removeQuestion: (id: number, qid: string) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}`,
      method: 'DELETE',
    }),
  generateAnswer: (id: number, qid: string) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}/answer`,
      method: 'POST',
    }),
}

// ---- 智能应用 · 百度网盘绑定 ----------------------------------------------------

export interface NetdiskStatus {
  /** 后端是否已配置 AppKey；false 时前端隐藏整个网盘入口。 */
  configured: boolean
  bound: boolean
  baidu_name: string | null
  expires_at: string | null
}

export const netdiskApi = {
  status: () => request<NetdiskStatus>({ url: '/office/netdisk/status' }),
  authUrl: () => request<{ url: string }>({ url: '/office/netdisk/auth-url' }),
  bind: (code: string) =>
    request<NetdiskStatus>({
      url: '/office/netdisk/bind',
      method: 'POST',
      data: { code },
      // 换 token + 拉用户信息是两走出站请求，慢起来会超过全局 30 秒。
      timeout: 60000,
      skipErrorToast: true,
    }),
  unbind: () => request<null>({ url: '/office/netdisk/unbind', method: 'POST' }),
  /** 把一段文本（分析报告等）存进网盘的 reports/ 子目录，返回完整路径。 */
  saveText: (filename: string, content: string) =>
    request<{ path: string }>({
      url: '/office/netdisk/save-text',
      method: 'POST',
      data: { filename, content },
      timeout: 60000,
    }),
}

// ---- AI 网关（仅管理员） ----------------------------------------------------

export interface AiChannelItem {
  id: number
  name: string
  base_url: string
  /** 掩码，明文永不回传。 */
  api_key: string
  api_key_error: string | null
  protocol: string
  models: string[] | null
  enabled: boolean
  remark: string | null
  last_tested_at: string | null
  last_test_ok: boolean
  last_test_error: string | null
  created_at: string
  updated_at: string
}

export interface AiChannelPayload {
  name: string
  base_url: string
  api_key?: string | null
  models?: string[] | null
  enabled?: boolean
  remark?: string | null
}

export interface AiModelRouteItem {
  id: number
  model_name: string
  channel_id: number
  channel_name: string | null
  upstream_model: string | null
  priority: number
  enabled: boolean
  remark: string | null
  created_at: string
  updated_at: string
}

export interface AiModelRoutePayload {
  model_name: string
  channel_id: number
  upstream_model?: string | null
  priority?: number
  enabled?: boolean
  remark?: string | null
}

export interface AiApiKeyItem {
  id: number
  name: string
  key_prefix: string
  enabled: boolean
  remark: string | null
  call_count: number
  last_used_at: string | null
  created_at: string
  updated_at: string
}

export interface AiApiKeyCreated {
  item: AiApiKeyItem
  /** 明文只在建钥匙这一次返回，关掉弹窗就再也拿不到。 */
  api_key: string
}

export interface AiCallLogItem {
  id: number
  request_id: string
  key_id: number | null
  key_name: string | null
  endpoint: string
  model: string | null
  stream: boolean
  channel_id: number | null
  channel_name: string | null
  upstream_model: string | null
  status_code: number
  success: boolean
  error: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  latency_ms: number
  first_token_ms: number | null
  client_ip: string | null
  created_at: string
}

export interface AiCallAttempt {
  channel_id: number
  channel_name: string
  status_code: number
  error: string | null
  latency_ms: number
}

export interface AiCallLogDetail extends AiCallLogItem {
  attempts: AiCallAttempt[] | null
  request_body: string | null
  response_body: string | null
}

export interface AiStatsBucket {
  name: string
  calls: number
  failed: number
  total_tokens: number
  avg_latency_ms: number
}

export interface AiStats {
  totals: {
    calls: number
    success: number
    failed: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    avg_latency_ms: number
    max_latency_ms: number
  }
  daily: AiStatsBucket[]
  by_channel: AiStatsBucket[]
  by_model: AiStatsBucket[]
  by_key: AiStatsBucket[]
}

export interface AiGatewayOverview {
  base_path: string
  channel_count: number
  channel_enabled: number
  route_count: number
  route_enabled: number
  key_count: number
  key_enabled: number
  log_count: number
  payload_logging: boolean
}

export interface AiLogQuery {
  page?: number
  page_size?: number
  key_id?: number
  channel_id?: number
  model?: string
  endpoint?: string
  success?: boolean
  keyword?: string
  start?: string
  end?: string
}

export const aiGatewayApi = {
  overview: () => request<AiGatewayOverview>({ url: '/ai-gateway/overview' }),

  channels: () =>
    request<{ items: AiChannelItem[]; total: number }>({ url: '/ai-gateway/channels' }),
  createChannel: (payload: AiChannelPayload) =>
    request<AiChannelItem>({ url: '/ai-gateway/channels', method: 'POST', data: payload }),
  updateChannel: (id: number, payload: Partial<AiChannelPayload>) =>
    request<AiChannelItem>({ url: `/ai-gateway/channels/${id}`, method: 'PUT', data: payload }),
  removeChannel: (id: number) =>
    request<null>({ url: `/ai-gateway/channels/${id}`, method: 'DELETE' }),
  testChannel: (id: number, model?: string) =>
    request<{ success: boolean; message: string }>({
      url: `/ai-gateway/channels/${id}/test`,
      method: 'POST',
      data: { model: model || null },
      // 真发一次上游请求，全局 30 秒不够。
      timeout: 120000,
      skipErrorToast: true,
    }),

  routes: (modelName?: string) =>
    request<{ items: AiModelRouteItem[]; total: number }>({
      url: '/ai-gateway/routes',
      params: modelName ? { model_name: modelName } : undefined,
    }),
  createRoute: (payload: AiModelRoutePayload) =>
    request<AiModelRouteItem>({ url: '/ai-gateway/routes', method: 'POST', data: payload }),
  updateRoute: (id: number, payload: Partial<AiModelRoutePayload>) =>
    request<AiModelRouteItem>({ url: `/ai-gateway/routes/${id}`, method: 'PUT', data: payload }),
  removeRoute: (id: number) => request<null>({ url: `/ai-gateway/routes/${id}`, method: 'DELETE' }),

  keys: () => request<{ items: AiApiKeyItem[]; total: number }>({ url: '/ai-gateway/keys' }),
  createKey: (payload: { name: string; remark?: string | null }) =>
    request<AiApiKeyCreated>({ url: '/ai-gateway/keys', method: 'POST', data: payload }),
  updateKey: (id: number, payload: { name?: string; enabled?: boolean; remark?: string | null }) =>
    request<AiApiKeyItem>({ url: `/ai-gateway/keys/${id}`, method: 'PUT', data: payload }),
  removeKey: (id: number) => request<null>({ url: `/ai-gateway/keys/${id}`, method: 'DELETE' }),

  stats: (days = 7) => request<AiStats>({ url: '/ai-gateway/stats', params: { days } }),
  logs: (params: AiLogQuery) =>
    request<{ items: AiCallLogItem[]; total: number }>({ url: '/ai-gateway/logs', params }),
  logDetail: (id: number) => request<AiCallLogDetail>({ url: `/ai-gateway/logs/${id}` }),
  purgeLogs: (beforeDays: number) =>
    request<{ deleted: number }>({
      url: '/ai-gateway/logs/purge',
      method: 'POST',
      data: { before_days: beforeDays },
      timeout: 120000,
    }),
}

/** 拼一条同源的 ws:// 或 wss:// 地址，路径沿用 axios 的 `/api` 前缀。 */
export function opsWsUrl(path: string, params: Record<string, string | number>): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) query.set(key, String(value))
  return `${scheme}://${window.location.host}/api/ops/${path}?${query.toString()}`
}

export default http
