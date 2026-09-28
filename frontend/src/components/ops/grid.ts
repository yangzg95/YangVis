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

// ---- 「保存数据为…」的四种文本格式 ------------------------------------------------

/**
 * 客户端序列化用的格式。键与后端全量导出的 ``format`` 参数一致，
 * 所以「当前页」和「全部行」两条路共享同一套菜单与后缀。
 */
export type ExportFormat = 'csv' | 'json' | 'markdown' | 'insert'

export const EXPORT_SUFFIX: Record<ExportFormat, string> = {
  csv: 'csv',
  json: 'json',
  markdown: 'md',
  insert: 'sql',
}

export const EXPORT_MIME: Record<ExportFormat, string> = {
  csv: 'text/csv',
  json: 'application/json',
  markdown: 'text/markdown',
  insert: 'application/sql',
}

/** 菜单里的出现顺序与名字：两处网格共用，别各写一份。 */
export const EXPORT_ORDER: ExportFormat[] = ['csv', 'json', 'markdown', 'insert']

export const EXPORT_LABEL: Record<ExportFormat, string> = {
  csv: 'CSV',
  json: 'JSON',
  markdown: 'Markdown',
  insert: 'INSERT 语句',
}

/** 反引号标识符，与后端 ``_quote_ident`` 同规则：内部反引号翻倍。 */
export function quoteIdent(name: string): string {
  return '`' + name.replace(/`/g, '``') + '`'
}

/** ``schema`.`table`` 形式的导出目标。 */
export function quoteTarget(schema: string, table: string): string {
  return `${quoteIdent(schema)}.${quoteIdent(table)}`
}

/**
 * MySQL 字面量，转义与后端 ``_display_literal`` 对齐（默认 sql_mode）。
 * 导出的 .sql 是给人看过再跑的，这里求的是「和屏幕上一致」，不是「可直接执行」。
 */
function sqlLiteral(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'NULL'
  if (typeof value === 'boolean') return value ? '1' : '0'
  const text = String(value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\0/g, '\\0')
  return `'${text}'`
}

/**
 * 筛选条件里用的字面量：这份文本会跟着 WHERE 真的交给后端执行，所以转义口径
 * 和只用于展示的 ``sqlLiteral`` 不同——单引号翻倍（默认 sql_mode 与
 * NO_BACKSLASH_ESCAPES 下都成立），反斜杠也翻倍，免得值以反斜杠收尾时
 * 把收尾引号吃掉。
 */
export function whereLiteral(value: unknown): string {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'NULL'
  if (typeof value === 'boolean') return value ? '1' : '0'
  const text = String(value).replace(/\\/g, '\\\\').replace(/'/g, "''")
  return `'${text}'`
}

/** Markdown 单元格：竖线转义、换行折成 <br>，否则一行表格会被内容劈开。 */
function mdCell(value: unknown): string {
  const text = value == null ? '' : String(value)
  return text.replace(/\|/g, '\\|').replace(/\r?\n/g, '<br>')
}

/** GFM 表格：粘进文档 / Issue 就能直接看的那种。 */
export function markdownTable(source: GridSource): string {
  const head = `| ${source.columns.map(mdCell).join(' | ')} |`
  const rule = `| ${source.columns.map(() => '---').join(' | ')} |`
  const body = source.rows.map((row) => `| ${row.map(mdCell).join(' | ')} |`)
  return [head, rule, ...body].join('\n') + '\n'
}

/** 一批多行 VALUES 的 INSERT；``target`` 是已加反引号的表名。 */
export function insertStatements(source: GridSource, target: string): string {
  if (!source.rows.length) return `-- ${target}：没有可导出的行\n`
  const cols = source.columns.map(quoteIdent).join(', ')
  const values = source.rows
    .map((row) => `(${row.map((v) => sqlLiteral(v)).join(', ')})`)
    .join(',\n')
  return `INSERT INTO ${target} (${cols}) VALUES\n${values};\n`
}

function jsonRecords(source: GridSource) {
  return source.rows.map((row) =>
    Object.fromEntries(source.columns.map((name, i) => [name, row[i] ?? null])),
  )
}

/** 把一份结果集序列化成待落盘的文本。``target`` 只有 INSERT 用得上。 */
export function exportText(format: ExportFormat, source: GridSource, target = '`result`'): string {
  switch (format) {
    case 'csv':
      // BOM 与后端流式导出一致：Excel 只认它，缺了就把中文按本地代码页解。
      return '\ufeff' + [csvLine(source.columns), ...source.rows.map((r) => csvLine(r))].join('\r\n')
    case 'json':
      return JSON.stringify(jsonRecords(source), null, 2)
    case 'markdown':
      return markdownTable(source)
    case 'insert':
      return insertStatements(source, target)
  }
}
