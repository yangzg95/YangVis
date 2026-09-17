/** 结果集是二维数组，a-table 要的是对象数组，在这里统一转一次。 */
export interface GridSource {
  columns: string[]
  rows: unknown[][]
}

export function toRows(source: GridSource): Record<string, unknown>[] {
  return source.rows.map((row, index) => {
    const record: Record<string, unknown> = { __i: index }
    source.columns.forEach((col, i) => {
      record[`c${i}`] = row[i]
    })
    return record
  })
}

/** 按字符宽度估算占位：CJK 算 2 个单位，其余 1 个。 */
function textUnits(value: unknown): number {
  const text = value == null ? '' : String(value)
  let units = 0
  for (const ch of text) units += ch.charCodeAt(0) > 255 ? 2 : 1
  return units
}

/** 长内容判定：超过列宽上限能显示的视觉宽度，基本已被截断，单元格才挂「展开」按钮。 */
export function isLongValue(value: unknown): boolean {
  return textUnits(value) > 36
}

// copyText 提升到了共享 utils（聊天页也要用），这里保持原导出路径不变。
export { copyText } from '@/utils/clipboard'

/** 一行值序列化成 CSV（null 成空串，含引号/逗号/换行的加引号）。 */
export function csvLine(values: unknown[]): string {
  return values
    .map((value) => {
      if (value == null) return ''
      const text = String(value)
      return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
    })
    .join(',')
}

/** 按 c{i} 约定从行记录里取出原始值数组。 */
export function recordValues(record: Record<string, unknown>, count: number): unknown[] {
  return Array.from({ length: count }, (_, i) => record[`c${i}`])
}

/** 「保存数据为…」：把文本内容落成一次浏览器下载。 */
export function downloadText(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

/**
 * scroll.x = 'max-content' 下表格是 table-layout: fixed，不给列宽会被均分、
 * 表头直接截断。这里按「字段名 + 采样行里最长的值」算一个动态宽度，
 * 上下限夹一下，超长内容仍交给 ellipsis。
 */
export function toColumns(source: GridSource) {
  return source.columns.map((col, i) => {
    let units = textUnits(col)
    for (const row of source.rows.slice(0, 50)) {
      units = Math.max(units, textUnits(row[i]))
    }
    // +56 = 单元格内边距 + 列头排序按钮的占位。
    const width = Math.min(Math.max(units * 8 + 56, 96), 320)
    return {
      title: col,
      dataIndex: `c${i}`,
      key: `c${i}`,
      ellipsis: true,
      width,
    }
  })
}
