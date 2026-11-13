/**
 * 「还原」进度流的读帧与判定。
 *
 * 不拉 jsdom：这条流只用 fetch / ReadableStream / TextDecoder / FormData，node 全都有。
 * 假 fetch 里把 signal 的 abort 接成流报错——真实 fetch 掐掉一个在读的响应体就是这个
 * 行为，「停止」和「看门狗」两条路都得靠它才测得到。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamRestore } from '@/api/restore'

// vi.mock 会被提到文件最前面，所以 spy 必须一起 hoisted。
const { handleUnauthorized } = vi.hoisted(() => ({ handleUnauthorized: vi.fn() }))
vi.mock('@/api/http', () => ({ handleUnauthorized }))

const encoder = new TextEncoder()

interface LiveStream {
  body: ReadableStream<Uint8Array>
  push: (text: string) => void
  fail: (err: unknown) => void
  close: () => void
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

function liveStream(): LiveStream {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
    },
  })
  return {
    body,
    push: (text) => controller.enqueue(encoder.encode(text)),
    fail: (err) => controller.error(err),
    close: () => controller.close(),
  }
}

function abortError(): Error {
  return Object.assign(new Error('The user aborted a request.'), { name: 'AbortError' })
}

interface Captured {
  url?: string
  init?: RequestInit
}

const captured: Captured = {}

/**
 * 装一个假 fetch。
 *
 * @param res 响应头与（可选的）响应体；不给 body 就是「没有流」的信封响应。
 * @param onAbort 流在 abort 时怎么失败——不给就不接（信封路径不需要）。
 */
function stubFetch(res: { status?: number; ok?: boolean; headers?: Record<string, string>; json?: unknown; body?: ReadableStream<Uint8Array> | null }, onAbort?: (err: unknown) => void) {
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    captured.url = url
    captured.init = init
    if (onAbort) {
      init.signal?.addEventListener('abort', () => onAbort(abortError()), { once: true })
    }
    return {
      status: res.status ?? 200,
      ok: res.ok ?? true,
      headers: new Headers(res.headers ?? { 'content-type': 'text/event-stream' }),
      body: res.body ?? null,
      json: async () => res.json,
    }
  })
}

function handlersOf() {
  return { onStart: vi.fn(), onProgress: vi.fn(), onDone: vi.fn(), onError: vi.fn() }
}

function sqlFile(): File {
  return new File(['INSERT INTO t VALUES (1);'], 'shop.sql', { type: 'text/plain' })
}

/** 排干微任务+宏任务：读循环里的 catch 落进 onError 需要走到宏任务。 */
function flush(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  handleUnauthorized.mockClear()
  delete captured.url
  delete captured.init
})

describe('streamRestore', () => {
  it('按帧派发 start/progress/done，跨块的半帧等下一批字节', async () => {
    const stream = liveStream()
    stubFetch({ body: stream.body }, stream.fail)
    const handlers = handlersOf()
    streamRestore(7, 'shop', sqlFile(), handlers)

    const start = frame('start', {
      total: 3,
      chunk_size: 200,
      stmt_timeout_seconds: 300,
      schema: 'shop',
      uses: ['archive'],
    })
    const progress = frame('progress', {
      done: 1,
      total: 3,
      succeeded: 1,
      failed: 0,
      skipped: 0,
      not_run: 2,
      errors: [],
      stop_reason: null,
    })
    const done = frame('done', {
      total: 3,
      done: 3,
      succeeded: 2,
      failed: 0,
      skipped: 1,
      refused: 0,
      not_run: 0,
      elapsed_ms: 1200,
      errors: [],
      stopped: false,
      stop_reason: null,
    })
    // 帧被切在任意字节上：半截要留在缓冲区里等下一批，而不是丢掉。
    stream.push(start.slice(0, 12))
    stream.push(start.slice(12) + ': ping\n\n' + progress)
    stream.push(done)
    stream.close()

    await vi.waitFor(() => expect(handlers.onDone).toHaveBeenCalledTimes(1))
    expect(handlers.onStart).toHaveBeenCalledTimes(1)
    expect(handlers.onStart.mock.calls[0][0]).toMatchObject({ total: 3, uses: ['archive'] })
    expect(handlers.onProgress).toHaveBeenCalledTimes(1)
    expect(handlers.onProgress.mock.calls[0][0]).toMatchObject({ done: 1, not_run: 2 })
    expect(handlers.onError).not.toHaveBeenCalled()
  })

  it('流开始前的统一信封按失败处理', async () => {
    stubFetch({
      headers: { 'content-type': 'application/json' },
      json: { code: 4101, message: '还原需要连接开启「允许写入」' },
    })
    const handlers = handlersOf()
    streamRestore(7, 'shop', sqlFile(), handlers)

    await vi.waitFor(() => expect(handlers.onError).toHaveBeenCalledTimes(1))
    expect(handlers.onError.mock.calls[0][0]).toContain('允许写入')
    expect(handlers.onDone).not.toHaveBeenCalled()
  })

  it('401 只走统一的登录过期出口', async () => {
    stubFetch({ status: 401, ok: false })
    const handlers = handlersOf()
    streamRestore(7, 'shop', sqlFile(), handlers)

    await vi.waitFor(() => expect(handleUnauthorized).toHaveBeenCalledTimes(1))
    expect(handlers.onError).not.toHaveBeenCalled()
  })

  it('没跑到 done 就断：无论干净收尾还是流报错，都说「可能只跑了一半」', async () => {
    const closed = liveStream()
    stubFetch({ body: closed.body }, closed.fail)
    const h1 = handlersOf()
    streamRestore(7, 'shop', sqlFile(), h1)
    closed.push(frame('progress', { done: 40, total: 900, succeeded: 40, failed: 0, skipped: 0, not_run: 860, errors: [], stop_reason: null }))
    closed.close()
    await vi.waitFor(() => expect(h1.onError).toHaveBeenCalledTimes(1))
    expect(h1.onError.mock.calls[0][0]).toContain('只跑了一半')

    const broken = liveStream()
    stubFetch({ body: broken.body }, broken.fail)
    const h2 = handlersOf()
    streamRestore(7, 'shop', sqlFile(), h2)
    broken.push(frame('start', { total: 900, chunk_size: 200, stmt_timeout_seconds: 300, schema: 'shop', uses: [] }))
    await vi.waitFor(() => expect(h2.onStart).toHaveBeenCalledTimes(1))
    broken.fail(new Error('socket hang up'))
    await vi.waitFor(() => expect(h2.onError).toHaveBeenCalledTimes(1))
    expect(h2.onError.mock.calls[0][0]).toBe('socket hang up，还原可能只跑了一半，请核对表数据后重试')
  })

  it('点「停止」只断流，既不算错误也不算完成', async () => {
    const stream = liveStream()
    stubFetch({ body: stream.body }, stream.fail)
    const handlers = handlersOf()
    const abort = streamRestore(7, 'shop', sqlFile(), handlers)

    stream.push(frame('start', { total: 900, chunk_size: 200, stmt_timeout_seconds: 300, schema: 'shop', uses: [] }))
    await vi.waitFor(() => expect(handlers.onStart).toHaveBeenCalledTimes(1))
    abort()
    await flush()

    expect(captured.init?.signal?.aborted).toBe(true)
    expect(handlers.onError).not.toHaveBeenCalled()
    expect(handlers.onDone).not.toHaveBeenCalled()
  })

  it('两分钟一个字节都没有就掐断，并说清是没响应', async () => {
    vi.useFakeTimers()
    const stream = liveStream()
    stubFetch({ body: stream.body }, stream.fail)
    const handlers = handlersOf()
    streamRestore(7, 'shop', sqlFile(), handlers)

    await vi.advanceTimersByTimeAsync(120_000)

    expect(handlers.onError).toHaveBeenCalledTimes(1)
    expect(handlers.onError.mock.calls[0][0]).toContain('没有响应')
  })

  it('请求带拉流声明与 stop_on_error，文件走 multipart 的 file 字段', () => {
    stubFetch({ body: liveStream().body })
    streamRestore(7, 'my db', sqlFile(), handlersOf(), { stopOnError: true })

    expect(captured.url).toBe('/api/ops/databases/7/schemas/my%20db/restore?stop_on_error=true')
    expect(captured.init?.headers).toMatchObject({ Accept: 'text/event-stream' })
    expect((captured.init?.body as FormData).get('file')).toBeInstanceOf(File)
  })
})
