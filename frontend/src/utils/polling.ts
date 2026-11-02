/**
 * 后台任务轮询的统一节拍器。
 *
 * 要解决的问题：页面用固定 `setInterval(2500)` 打接口时，后端一挂就是每 2.5s
 * 一次失败请求、一条错误 toast；页面切到后台（`document.hidden`）也照打不误。
 *
 * 这里改成一个自调度的 setTimeout 链：
 * - 成功：回到基础间隔；
 * - 失败：间隔翻倍递增，封顶 `maxInterval`，避免故障时持续敲后端；
 * - 页面隐藏：不再发请求，等 `visibilitychange` 回到前台立刻补一轮；
 * - `shouldContinue` 返回 false 时自动停表（数据落定就停，不留空转定时器）。
 *
 * 失败不向调用方抛：轮询失败本身就是下一轮重试的信号，错误提示由调用方
 * 决定是否展示（大多数情况下靠 http 拦截器的去重 toast 兜底）。
 */
export interface PollLoop {
  start: () => void
  stop: () => void
  readonly running: boolean
}

export interface PollLoopOptions {
  /** 正常间隔，毫秒。 */
  interval?: number
  /** 失败退避的上限，毫秒。 */
  maxInterval?: number
  /** 每轮结束后判断是否继续，返回 false 自动停表。 */
  shouldContinue?: () => boolean
}

export function createPollLoop(task: () => Promise<unknown>, options: PollLoopOptions = {}): PollLoop {
  const base = options.interval ?? 2500
  const max = options.maxInterval ?? 30000
  let timer: number | undefined
  let delay = base
  let running = false

  const clear = () => {
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
  }

  const schedule = (ms: number) => {
    clear()
    timer = window.setTimeout(() => void run(), ms)
  }

  const run = async () => {
    timer = undefined
    if (!running) return
    // 后台不请求，交给 visibilitychange 续上。
    if (document.hidden) return
    let failed = false
    try {
      await task()
    } catch {
      // 本轮失败已在拦截器里提示过（且同文案会去重），这里只负责退避。
      failed = true
    }
    if (!running) return
    if (options.shouldContinue && !options.shouldContinue()) {
      stop()
      return
    }
    delay = failed ? Math.min(delay * 2, max) : base
    schedule(delay)
  }

  const onVisibility = () => {
    if (!running || document.hidden || timer !== undefined) return
    // 回到前台立刻补一轮，并复位退避。
    delay = base
    schedule(0)
  }

  const stop = () => {
    running = false
    clear()
    document.removeEventListener('visibilitychange', onVisibility)
  }

  return {
    start() {
      if (running) return
      running = true
      delay = base
      document.addEventListener('visibilitychange', onVisibility)
      if (!document.hidden) schedule(0)
    },
    stop,
    get running() {
      return running
    },
  }
}
