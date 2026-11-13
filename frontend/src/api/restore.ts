/**
 * 「还原备份 / 导入 SQL」的进度流。
 *
 * 请求与响应两头都不是 axios 那套：上去的是 multipart 文件，回来的是一段段 SSE
 * 事件——一份几十万句的脚本要跑几分钟，普通请求只会在服务端还在写的时候先超时。
 * 和 sse.ts 同一个理由放在域文件外面：它服务的是这一条流自己的事件名，
 * 不并进对话/工作流那套 handlers。
 *
 * 后端把可预期错误（没有写权限、文件过大、不是 UTF-8、连不上库）留在流开始之前，
 * 那时给的还是统一信封，所以 content-type 是 JSON 就按失败处理。
 */
import { getAccessToken } from '@/utils/auth'
import { handleUnauthorized } from './http'

export interface RestoreErrorItem {
  /** 脚本里的第几句（从 1 开始，与拆分顺序一致）。 */
  index: number
  sql: string
  message: string
  elapsed_ms: number
}

export interface RestoreStartEvent {
  total: number
  chunk_size: number
  stmt_timeout_seconds: number
  schema: string | null
  /** 脚本里出现的 ``USE``：默认库会跟着它们走，界面选的目标库只是起点。 */
  uses: string[]
}

export interface RestoreProgressEvent {
  done: number
  total: number
  succeeded: number
  failed: number
  /** 这两个计数跟着每一帧走：跑到一半也要分得清「没跑」和「只是跳过」。 */
  skipped: number
  not_run: number
  errors: RestoreErrorItem[]
  stop_reason: string | null
}

export interface RestoreDoneEvent {
  total: number
  done: number
  succeeded: number
  failed: number
  skipped: number
  /** 被网关拦下、压根没送进数据库的句数。 */
  refused: number
  /** 停手之后一句都没跑的句数——它和失败是两件事。 */
  not_run: number
  elapsed_ms: number
  started_at: string
  finished_at: string
  stopped: boolean
  stop_reason: string | null
  errors: RestoreErrorItem[]
  chunk_size: number
  stmt_timeout_seconds: number
}

export interface RestoreHandlers {
  onStart?: (data: RestoreStartEvent) => void
  onProgress?: (data: RestoreProgressEvent) => void
  onDone?: (data: RestoreDoneEvent) => void
  /** 信封错误、看门狗掐断、流半路断了都走这里；用户自己点停止不会。 */
  onError?: (message: string) => void
}

/**
 * 停顿看门狗：服务端每 15 秒一帧心跳，两分钟一个字节都没有就是假死。
 * 从响应头到达起算——上传 64MB 期间本来就没有响应字节。
 */
const STALL_TIMEOUT = 120_000

/** 只要没收到 done 帧就一定要带上这句：逐句 autocommit，断了就是「部分落盘」。 */
const HALF_RUN_HINT = '还原可能只跑了一半，请核对表数据后重试'

/**
 * 发起一次还原，返回中断函数。
 *
 * 「中断」只让浏览器不再收进度：服务端那一侧的会话不会因此回滚，已经落盘的语句
 * 留在库里，正在跑的那一批跑完当前语句才停。调用方要把这两件事说清楚。
 */
export function streamRestore(
  databaseId: number,
  schema: string,
  file: File,
  handlers: RestoreHandlers,
  opts?: { stopOnError?: boolean; signal?: AbortSignal },
): () => void {
  const controller = new AbortController()
  if (opts?.signal) {
    if (opts.signal.aborted) controller.abort()
    else opts.signal.addEventListener('abort', () => controller.abort(), { once: true })
  }
  let watchdog: ReturnType<typeof setTimeout> | undefined
  let watchdogFired = false
  let receivedTerminal = false

  const clearWatchdog = () => {
    if (watchdog !== undefined) {
      clearTimeout(watchdog)
      watchdog = undefined
    }
  }
  const armWatchdog = () => {
    clearWatchdog()
    watchdog = setTimeout(() => {
      watchdogFired = true
      controller.abort()
    }, STALL_TIMEOUT)
  }

  const query = opts?.stopOnError ? '?stop_on_error=true' : ''
  const body = new FormData()
  body.append('file', file)

  void (async () => {
    try {
      const token = getAccessToken()
      const res = await fetch(
        `/api/ops/databases/${databaseId}/schemas/${encodeURIComponent(schema)}/restore${query}`,
        {
          method: 'POST',
          // 不写 Content-Type：multipart 的 boundary 只能由浏览器自己补。
          // 显式声明拉流，后端的压缩中间件才会放过这条响应。
          headers: {
            Accept: 'text/event-stream',
            ...(token ? { Authorization: token } : {}),
          },
          body,
          signal: controller.signal,
        },
      )
      if (res.status === 401) {
        handleUnauthorized()
        return
      }
      if (res.headers.get('content-type')?.includes('application/json')) {
        const envelope = await res.json().catch(() => null)
        handlers.onError?.(envelope?.message || `还原失败（HTTP ${res.status}）`)
        return
      }
      if (!res.ok || !res.body) {
        handlers.onError?.(`还原失败（HTTP ${res.status}）`)
        return
      }

      armWatchdog()
      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        armWatchdog()
        buffer += decoder.decode(value, { stream: true })
        const blocks = buffer.split('\n\n')
        // 末尾没有空行说明这帧还没收完，留在 buffer 里等下一批字节。
        buffer = blocks.pop() ?? ''
        for (const block of blocks) {
          const frame = parseFrame(block)
          if (!frame) continue
          if (frame.event === 'done') receivedTerminal = true
          if (frame.event === 'start') handlers.onStart?.(frame.data as RestoreStartEvent)
          else if (frame.event === 'progress') handlers.onProgress?.(frame.data as RestoreProgressEvent)
          else if (frame.event === 'done') handlers.onDone?.(frame.data as RestoreDoneEvent)
        }
      }
      // 没见过 done 就是半路断了：服务端挂了、代理掐了都会这样。计数已经通过
      // progress 帧交给界面了，这里只补一句「到哪一步了没人知道」。
      if (!receivedTerminal) handlers.onError?.(`连接中断，${HALF_RUN_HINT}`)
    } catch (err) {
      const message = (err as Error)?.message || '网络错误'
      if ((err as Error)?.name === 'AbortError') {
        // 用户点停止走的也是这里，但他知道自己停了——只有看门狗掐断才要报。
        if (watchdogFired) handlers.onError?.(`还原没有响应，连接已断开，${HALF_RUN_HINT}`)
        return
      }
      // 流本身报错（代理掐了、后端挂了）同样是「没跑到 done」：只甩一句
      // socket hang up，用户无从知道库现在算跑完了没有。
      handlers.onError?.(receivedTerminal ? message : `${message}，${HALF_RUN_HINT}`)
    } finally {
      clearWatchdog()
    }
  })()

  return () => controller.abort()
}

function parseFrame(block: string): { event: string; data: unknown } | null {
  // 心跳是注释帧（``: ping``），EventSource 语义下客户端必须忽略。
  if (!block.trim() || block.trimStart().startsWith(':')) return null
  let event = ''
  let raw = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) raw += line.slice(5).trim()
  }
  if (!event) return null
  try {
    return { event, data: raw ? JSON.parse(raw) : {} }
  } catch {
    return null
  }
}
