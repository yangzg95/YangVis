/**
 * 把在线简历文档（ResumeDocData）拍成给大模型读的 Markdown。
 *
 * 对话页「基于简历发起对话」时，首条消息带的就是这份文本（见 Chat.vue 的
 * resume-doc 深链）。字段取舍、时间段口径与后端 services/resume_builder.py
 * 的 _full_resume_text 对齐，区别是这里保留标题层级与列表换行，模型读起来
 * 结构更清楚；模块顺序尊重 doc.module_order（与用户在编辑器里排的一致）。
 */
import type {
  CompanyEntry,
  EducationEntry,
  ProjectEntry,
  ResumeDocData,
  ResumeModuleKey,
} from './resumeDoc'
import { MODULE_LABELS } from './resumeDoc'
import { entryIsEmpty, monthRangeLabel } from './resumeTemplate'

// ---- 富文本片段 → 纯文本 --------------------------------------------------------

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
}

function decodeEntities(input: string): string {
  return input.replace(/&(#x?[0-9a-f]+|[a-z]+);/gi, (raw, body: string) => {
    if (body[0] === '#') {
      const code =
        body[1].toLowerCase() === 'x'
          ? parseInt(body.slice(2), 16)
          : parseInt(body.slice(1), 10)
      if (Number.isFinite(code) && code > 0) return String.fromCodePoint(code)
      return raw
    }
    return NAMED_ENTITIES[body.toLowerCase()] ?? raw
  })
}

/**
 * 消毒过的简历富文本（只含 p/br/div/ul/ol/li/a 等白名单标签）拍成纯文本：
 * 列表项保留成「- 」开头的行，段落与换行保留，行内样式标签直接去掉。
 * 刻意不依赖 DOM，与 resumeTemplate.ts 的消毒器同理由。
 */
export function richHtmlToPlainText(html: string): string {
  if (!html) return ''
  const text = html
    .replace(/<br\s*\/?>(?:[^\S\n]*\n)?/gi, '\n')
    .replace(/<\/(p|div|blockquote|ul|ol)>/gi, '\n')
    .replace(/<\/li\s*>/gi, '\n')
    .replace(/<li[^>]*>/gi, '- ')
    .replace(/<[^>]+>/g, '')
  return decodeEntities(text)
    .replace(/[^\S\n]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

// ---- 文档 → Markdown -----------------------------------------------------------

function nonEmpty(...values: string[]): string[] {
  return values.map((v) => (v || '').trim()).filter(Boolean)
}

function profileSection(doc: ResumeDocData): string[] {
  const p = doc.profile
  const blocks: string[] = []

  const head = nonEmpty(p.name, p.job_target, p.city).join(' / ')
  if (head) blocks.push(`# ${head}`)

  const contacts: string[] = []
  for (const [label, value] of [
    ['性别', p.gender],
    ['出生年月', p.birthday],
    ['电话', p.phone],
    ['邮箱', p.email],
    ['个人网站', p.website],
  ] as const) {
    if (value && value.trim()) contacts.push(`- ${label}：${value.trim()}`)
  }
  if (contacts.length) blocks.push(contacts.join('\n'))

  if (p.intro.trim()) blocks.push(p.intro.trim())

  const summary = richHtmlToPlainText(p.summary_html)
  if (summary) blocks.push(`## 个人优势\n\n${summary}`)
  return blocks
}

function companyBlock(entry: CompanyEntry): string {
  const heading = nonEmpty(entry.company, entry.position).join(' / ')
  const lines: string[] = []
  if (entry.department.trim()) lines.push(`- 部门：${entry.department.trim()}`)
  const period = monthRangeLabel(entry.start, entry.end, entry.current)
  if (period) lines.push(`- 时间：${period}`)
  const desc = richHtmlToPlainText(entry.description_html)
  if (desc) lines.push(desc)
  return [`### ${heading || '工作经历'}`, lines.join('\n')].filter(Boolean).join('\n\n')
}

function projectBlock(entry: ProjectEntry): string {
  const heading = nonEmpty(entry.name, entry.role).join(' / ')
  const lines: string[] = []
  if (entry.department.trim()) lines.push(`- 部门：${entry.department.trim()}`)
  const period = monthRangeLabel(entry.start, entry.end, entry.current)
  if (period) lines.push(`- 时间：${period}`)
  const desc = richHtmlToPlainText(entry.description_html)
  if (desc) lines.push(desc)
  return [`### ${heading || '项目经历'}`, lines.join('\n')].filter(Boolean).join('\n\n')
}

function educationBlock(entry: EducationEntry): string {
  const heading = nonEmpty(entry.school, entry.major).join(' / ')
  const lines: string[] = []
  for (const [label, value] of [
    ['学院', entry.college],
    ['学历', entry.degree],
    ['就读形式', entry.study_mode],
  ] as const) {
    if (value && value.trim()) lines.push(`- ${label}：${value.trim()}`)
  }
  const period = monthRangeLabel(entry.start, entry.end, entry.current)
  if (period) lines.push(`- 时间：${period}`)
  const desc = richHtmlToPlainText(entry.description_html)
  if (desc) lines.push(desc)
  return [`### ${heading || '教育经历'}`, lines.join('\n')].filter(Boolean).join('\n\n')
}

function moduleSection(doc: ResumeDocData, key: ResumeModuleKey): string {
  if (key === 'profile') return profileSection(doc).join('\n\n')

  if (key === 'skills') {
    const skills = richHtmlToPlainText(doc.skills_html)
    return skills ? `## ${MODULE_LABELS.skills}\n\n${skills}` : ''
  }

  // 加了没填的空条目直接跳过（与预览/导出的口径一致，见 entryIsEmpty），
  // 不然模型会读到一串没有信息量的空标题。
  if (key === 'companies') {
    const entries = doc.companies.filter((entry) => !entryIsEmpty('companies', entry))
    return entries.length
      ? `## ${MODULE_LABELS.companies}\n\n${entries.map(companyBlock).join('\n\n')}`
      : ''
  }
  if (key === 'projects') {
    const entries = doc.projects.filter((entry) => !entryIsEmpty('projects', entry))
    return entries.length
      ? `## ${MODULE_LABELS.projects}\n\n${entries.map(projectBlock).join('\n\n')}`
      : ''
  }
  if (key === 'educations') {
    const entries = doc.educations.filter((entry) => !entryIsEmpty('educations', entry))
    return entries.length
      ? `## ${MODULE_LABELS.educations}\n\n${entries.map(educationBlock).join('\n\n')}`
      : ''
  }
  return ''
}

/** 整份简历 → Markdown；空模块整体省略，完全空白的文档得到空串。 */
export function resumeDocToMarkdown(doc: ResumeDocData): string {
  const sections = doc.module_order
    .map((key) => moduleSection(doc, key))
    .filter((section) => section.trim())
  return sections.join('\n\n').trim()
}

// ---- 空文档判断 ----------------------------------------------------------------

/**
 * 简历是否有任何可分析的内容（纯空骨架时不该发起对话）。
 * 转换函数对每个模块都做了内容门禁，markdown 非空即等价于「有内容」。
 */
export function resumeDocHasContent(doc: ResumeDocData): boolean {
  return resumeDocToMarkdown(doc).length > 0
}
