import { format } from 'sql-formatter'

/**
 * 工作台「美化 SQL」：mysql 方言、关键字大写，与编辑器补全的大写风格一致。
 * 对残句 / 方言边角会抛错——调用方捕获后保持原文不变。
 */
export function formatSql(text: string): string {
  return format(text, { language: 'mysql', keywordCase: 'upper', tabWidth: 2 })
}
