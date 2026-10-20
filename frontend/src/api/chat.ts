/**
 * 对话域：会话与消息的维护、补全/重试/确认命令三条 SSE 流的入口，以及运维动作卡片的数据结构。
 * 流的实现与回调契约在 sse.ts，这里只声明每类事件对应的载荷类型。
 */
import { request } from './http'
import { streamSse, type StreamHandlers } from './sse'

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

/** 挂在一条提问上的附件。图片有预览地址，文档只留文件名与大小。 */
export interface ChatAttachment {
  id: number
  kind: 'image' | 'file'
  filename: string
  mime: string | null
  size: number
  /** 图片的浏览器访问地址；文档与历史遗留图片为 null。 */
  url: string | null
}

/**
 * 一条助手回答按到达顺序记录的时间线条目。
 * - text：正文增量（连续片段后端已合并）；整条回答的完整文本就是 content，
 *   时间线渲染时跳过它，只用 reason / step 重建「思考 + 调工具」的过程。
 * - reason：推理模型的思维链增量，同样按连续片段合并。
 * - step：一次工具调用，input 是完整入参，展示时按工具名挑关键字段。
 */
export interface TimelineEntry {
  type: 'text' | 'step' | 'reason'
  detail?: string
  tool?: string
  query?: string
  input?: Record<string, unknown>
  /** 仅流式期间使用：该工具是否还在执行。历史消息无此字段，按已完成渲染。 */
  running?: boolean
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  /** 按到达顺序记录的回答流；老消息为 null，前端退回只渲染 content。 */
  timeline?: TimelineEntry[] | null
  /** 这条提问随带了哪些附件（图片原图/文档名）；助手消息恒为空。 */
  attachments?: ChatAttachment[]
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
   * 上传一条对话附件，先拿 id 再随提问发出。
   *
   * 文档在后端当场抽成文本，图片落盘保留原图；上传完只返回元数据，
   * 预览图片直接用返回的 url。
   */
  uploadAttachment: (file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<ChatAttachment>({
      url: '/chat/attachments',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      // 大文件与 PDF 解析可能超过默认的 30 秒。
      timeout: 120000,
      skipErrorToast: true,
    })
  },
  /** 删除一条尚未发送的附件。 */
  deleteAttachment: (id: number) =>
    request<null>({ url: `/chat/attachments/${id}`, method: 'DELETE', skipErrorToast: true }),

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
      /** 已上传、尚未发送的附件 id，按用户选择顺序排列。 */
      attachment_ids?: number[]
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
