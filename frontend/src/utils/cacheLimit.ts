/**
 * 给无界 Map 兜底：超过上限就按插入顺序丢掉最旧的条目。
 *
 * 聊天的渲染缓存按「消息 key」存放，长会话会一直增长；虽然换会话时会整体清空，
 * 但停留在同一段长对话里也能堆到上千条。这里只做容量兜底，不做严格 LRU：
 * 缓存的 key 都对应已落定的消息，旧条目被丢掉后最多多解析一次。
 */
export function capCache<K, V>(cache: Map<K, V>, limit: number): void {
  while (cache.size > limit) {
    const oldest = cache.keys().next()
    if (oldest.done) return
    cache.delete(oldest.value)
  }
}
