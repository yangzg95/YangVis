/**
 * SSE 拉流的公共实现与回调契约：streamSse 用 fetch + ReadableStream 读事件帧，
 * parseSseBlock 与 dispatchSse 把一帧派发成 StreamHandlers 上的回调。
 *
 * 对话与工作流两条链路共用它，所以不放进任何一个域文件里。它反过来只以 import type
 * 依赖 chat.ts 与 workflows.ts 的载荷类型——类型位在编译后擦除，不构成运行时环。
 */
import { getAccessToken } from '@/utils/auth'
import type { Citation, CompactMeta, ConfirmEvent, ExecEvent } from './chat'
import { handleUnauthorized } from './http'
import type { RunStatus, RunStepStatus } from './workflows'

/** 由 SSE 事件流驱动的一组回调。 */
export interface StreamHandlers {
  onMeta?: (data: {
    conversation_id: number
    agent_id: number
    project_id: number | null
    type_ids: number[] | null
    model_config_id: number | null
    /** 本轮生效的工具选择（meta 在提问落库后发出，已含本次覆盖）。 */
    local_tool_keys: string[] | null
    mcp_server_ids: number[] | null
  }) => void
  onCitations?: (citations: Citation[]) => void
  onStep?: (data: {
    tool: string
    query: string
    input?: Record<string, unknown>
    run_id?: string
  }) => void
  /** 一次工具调用返回：把结果预览挂回同 run_id 的步骤卡片。 */
  onStepResult?: (data: { run_id: string; output: string; is_error: boolean }) => void
  onToken?: (token: string) => void
  /** 会输出思维链的模型发来的推理/思考增量；普通模型不会有这个事件。 */
  onReason?: (token: string) => void
  onConfirm?: (data: ConfirmEvent) => void
  onExec?: (data: ExecEvent) => void
  onDone?: (data: { message_id: number; conversation_id: number }) => void
  // ---- 手动压缩预览流（compact_meta → token* → compact_done）----
  onCompactMeta?: (data: CompactMeta) => void
  onCompactDone?: (data: { cut_message_id: number }) => void
  onError?: (data: { code: number; message: string }) => void
  // ---- 工作流运行的进度事件（对话用不到这几项）----

  onRunStart?: (data: {
    run_id: number
    workflow_slug: string
    /** 中途接上的跟随流给的是运行此刻的状态；发起那条流里恒为 running。 */
    status?: string
    steps: {
      key: string
      group_key: string
      agent_slug: string
      agent_name?: string | null
      output_kind?: string | null
      optional: boolean
      /** 跟随流接上时这一步已经跑到哪了；发起流里全是 pending。 */
      status?: string
      /** 跟随流接上时这一步已经用过的模型；发起流里还没跑，为 null。 */
      model_name?: string | null
    }[]
  }) => void
  onStepStart?: (data: { key: string; group_key: string; status?: string }) => void
  onStepEnd?: (data: {
    key: string
    status: RunStepStatus
    error?: string | null
    output_chars: number
    elapsed_ms?: number | null
    /** 这一步实际用的模型：跑完才说得出，没跑到的步骤为 null。 */
    model_name?: string | null
  }) => void
  onRunEnd?: (data: {
    run_id: number
    status: RunStatus
    error?: string | null
    elapsed_ms?: number | null
  }) => void
}

/**
 * 流停顿看门狗：这么久一个字节都没收到就判定假死。
 * 后端每 15s 发一帧心跳，健康连接在长思考期间也不会触发；放宽到 100s
 * （约 6 帧心跳余量）是为了容忍网络抖动，也不会误杀合法的长思考。
 */
const STREAM_STALL_TIMEOUT = 100_000

/**
 * SSE 拉流的公共实现。
 *
 * 这里刻意绕开 http.ts 那个 axios 实例：它的拦截器会去拆一层 JSON envelope，
 * 而 token stream 根本没有这层结构。改用 fetch 加 ReadableStream reader。
 * 默认 POST（要带请求体的那条对话流就靠它）；工作流的「跟随进度」是纯读，
 * 走 GET。返回一个用于中断的函数。
 */
export function streamSse(
  url: string,
  body: unknown,
  handlers: StreamHandlers,
  method: 'GET' | 'POST' = 'POST',
): () => void {
  const controller = new AbortController()
  // 用户主动中断（停止按钮）与看门狗掐断要分开：前者静默，后者要回调错误。
  let watchdogFired = false
  let receivedTerminal = false
  let watchdog: ReturnType<typeof setTimeout> | undefined

  const clearWatchdog = () => {
    if (watchdog !== undefined) {
      clearTimeout(watchdog)
      watchdog = undefined
    }
  }
  // 收到任何字节都算连接活着——心跳注释帧同样会走到这里重置计时。
  const armWatchdog = () => {
    clearWatchdog()
    watchdog = setTimeout(() => {
      watchdogFired = true
      controller.abort()
    }, STREAM_STALL_TIMEOUT)
  }

  void (async () => {
    try {
      armWatchdog()
      const response = await fetch(url, {
        method,
        headers: {
          'Content-Type': 'application/json',
          ...(getAccessToken() ? { Authorization: getAccessToken() as string } : {}),
        },
        body: method === 'GET' || body === undefined ? undefined : JSON.stringify(body),
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
        armWatchdog()
        buffer += decoder.decode(value, { stream: true })

        // 事件之间用空行分隔；最后一个空行之后的内容属于还没收完的半个
        // 事件，先留在 buffer 里等下一批数据。
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() ?? ''

        for (const block of blocks) {
          const event = parseSseBlock(block)
          if (event) {
            if (
              event.event === 'done' ||
              event.event === 'compact_done' ||
              event.event === 'run_end' ||
              event.event === 'error'
            ) {
              receivedTerminal = true
            }
            dispatchSse(event, handlers)
          }
        }
      }

      // 流读完了却没见过终止事件：服务端被掐断、代理断连都会这样，
      // 不补上这一嗓，界面上的「生成中」就永远停在转圈。
      if (!receivedTerminal) {
        handlers.onError?.({ code: -1, message: '连接中断，请重试' })
      }
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        if (watchdogFired) {
          handlers.onError?.({ code: -1, message: '连接超时，请重试' })
        }
        return
      }
      handlers.onError?.({ code: -1, message: (err as Error)?.message || '网络错误' })
    } finally {
      clearWatchdog()
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
    case 'step_result':
      handlers.onStepResult?.(data ?? { run_id: '', output: '', is_error: false })
      break
    case 'token':
      handlers.onToken?.(String(data ?? ''))
      break
    case 'reason':
      handlers.onReason?.(String(data ?? ''))
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
    case 'compact_meta':
      handlers.onCompactMeta?.(data)
      break
    case 'compact_done':
      handlers.onCompactDone?.(data)
      break
    // 工作流的进度事件：事件名与对话的互不重叠，共用一套解析与中断逻辑。
    case 'run_start':
      handlers.onRunStart?.(data)
      break
    case 'step_start':
      handlers.onStepStart?.(data)
      break
    case 'step_end':
      handlers.onStepEnd?.(data)
      break
    case 'run_end':
      handlers.onRunEnd?.(data)
      break
    case 'error':
      handlers.onError?.(data)
      break
  }
}
