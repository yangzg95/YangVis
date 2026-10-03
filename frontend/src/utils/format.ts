/** 展示用的格式化小工具。 */

/** 字节数 → 「812 B / 4.2 K / 30.1 M / 1.5 G」。 */
export function fmtSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} K`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} M`
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} G`
}

/**
 * 毫秒 → 「850 ms / 12.3 秒 / 3 分 5 秒 / 1 小时 20 分」。
 *
 * 一次运行报的是整条流程的墙上时间，不是各步之和——并行组里两步同时在跑，加起来的
 * 数比实际等待的长，报出来只会让人多等一会儿以为哪里卡住了。
 */
export function fmtElapsed(ms: number): string {
  if (ms < 1000) return `${ms} ms`
  // 先归到整秒再分档：边界上直接进下一档，而不是写出「60.0 秒」「59 分 60 秒」
  // 这种比上限还大、看着像少进了一位的答案。
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${(ms / 1000).toFixed(1)} 秒`
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return `${Math.floor(seconds / 3_600)} 小时 ${Math.floor((seconds % 3_600) / 60)} 分`
}

/** 运行与步骤的状态 → 中文文案 / a-tag 颜色，卡片与运行抽屉共用一份口径。 */
const STATUS_TEXT: Record<string, string> = {
  pending: '待运行',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  cancelled: '已取消',
  skipped: '未执行',
}
const STATUS_COLOR: Record<string, string> = {
  pending: 'default',
  running: 'processing',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'warning',
  skipped: 'default',
}

/** 后端加了新状态值时照原样显示，不让界面空白。 */
export function runStatusText(status: string): string {
  return STATUS_TEXT[status] ?? status
}
export function runStatusColor(status: string): string {
  return STATUS_COLOR[status] ?? 'default'
}
