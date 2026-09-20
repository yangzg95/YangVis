/** 报告（简历分析/对比/求职助手）的共用组装与加载。 */
import { resumeApi } from '@/api'

/** 改进意见单独呈现在正文前面，导出/存档/发起对话时拼回去，出去的才是完整的一份。 */
export function composeReportMarkdown(
  report: string | null,
  suggestions: string[] | null,
): string {
  let content = report || ''
  if (suggestions?.length) {
    const list = suggestions.map((item, index) => `${index + 1}. ${item}`).join('\n')
    content = `## 改进意见\n\n${list}\n\n---\n\n${content}`
  }
  return content
}

/**
 * 按 kind/id 拉取报告全文（含改进意见），供对话页「基于报告发起对话」组装首条消息。
 *
 * 标题取详情接口里的原始 title，不带 ReportView 的「分析报告 · 」前缀——
 * 那个前缀是页面展示用的，会话标题和引导语要的是报告名本身。
 */
export async function loadReportForChat(
  kind: string,
  id: number,
): Promise<{ title: string; markdown: string }> {
  if (kind === 'resume') {
    const detail = await resumeApi.detail(id)
    return { title: detail.title, markdown: composeReportMarkdown(detail.report, detail.suggestions) }
  }
  if (kind === 'comparison') {
    const detail = await resumeApi.comparisonDetail(id)
    return {
      title: detail.title || `#${detail.id}`,
      markdown: composeReportMarkdown(detail.report, null),
    }
  }
  if (kind === 'toolkit') {
    const detail = await resumeApi.toolkitTaskDetail(id)
    return { title: detail.title, markdown: composeReportMarkdown(detail.report, null) }
  }
  throw new Error(`未知的报告类型：${kind}`)
}
