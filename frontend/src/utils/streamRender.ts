import { onBeforeUnmount, ref } from 'vue'

/**
 * 流式 markdown 渲染的「缓存 + 节拍器」。
 *
 * 解决的问题：聊天模板里 v-html="md.render(text)" 会在每个 token 到达时触发
 * 一次全组件重渲染，而渲染函数对每条历史消息都会再跑一遍完整 markdown 解析 ——
 * 开销是 token 数 × 消息数 × 全文长度，长回答下肉眼可见地卡。
 *
 * 两个手段：
 * - 缓存：同一条消息内容没变就直接回旧 HTML，历史消息从此零解析开销。
 * - 节拍：正在流式追加的那条消息内容每个 token 都在变，但只在 tick 前进时
 *   才真正重新解析（约每 100ms 一次，视觉上仍是「连续打字」）；tick 之间的
 *   重渲染（比如输入框打字引发的）直接回旧 HTML。
 *
 * 用法：每个 token 调 schedule()；开始流式调 beginStream(key)；结束（完成 /
 * 出错 / 手动停止）调 endStream()，它会立刻推最后一次全量渲染。
 */
export function useStreamRender(render: (text: string) => string) {
  const RENDER_INTERVAL = 100

  const tick = ref(0)
  let timer: number | undefined
  /** 正在流式追加的消息 key；null 表示当前没有流式输出。 */
  let streamingKey: string | null = null

  interface CacheEntry {
    text: string
    html: string
    tick: number
  }
  const cache = new Map<string, CacheEntry>()

  /** 每个 token 调一次；实际重渲染按 RENDER_INTERVAL 合并。 */
  function schedule() {
    if (timer !== undefined) return
    timer = window.setTimeout(() => {
      timer = undefined
      tick.value++
    }, RENDER_INTERVAL)
  }

  /** 标记一条消息进入流式追加。 */
  function beginStream(key: string) {
    streamingKey = key
  }

  /** 流式结束：清掉挂起的节拍，立刻推一次 tick 让最终全文渲染出来。 */
  function endStream() {
    streamingKey = null
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timer = undefined
    }
    tick.value++
  }

  /**
   * 模板里渲染一条消息。第一行必须读 tick.value —— 这是和节拍器建立依赖的
   * 地方，tick 前进时 Vue 才会重跑渲染函数走到这里。
   */
  function renderCached(key: string, text: string): string {
    const currentTick = tick.value
    const hit = cache.get(key)
    if (hit && hit.text === text) return hit.html
    // 流式中的消息：内容变了但节拍没前进，先回旧 HTML，等下一个 tick 再解析。
    if (key === streamingKey && hit && hit.tick === currentTick) return hit.html
    const html = render(text)
    cache.set(key, { text, html, tick: currentTick })
    return html
  }

  /** 消息列表整体换掉时调用，释放缓存。 */
  function clearCache() {
    cache.clear()
  }

  onBeforeUnmount(() => {
    if (timer !== undefined) window.clearTimeout(timer)
  })

  return { schedule, beginStream, endStream, renderCached, clearCache }
}
