/** 展示用的格式化小工具。 */

/** 字节数 → 「812 B / 4.2 K / 30.1 M / 1.5 G」。 */
export function fmtSize(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} K`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} M`
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} G`
}
