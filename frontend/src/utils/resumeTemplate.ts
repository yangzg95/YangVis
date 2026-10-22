/**
 * 在线简历的打印渲染与富文本前端消毒。
 *
 * - sanitizeRichHtml 是后端白名单消毒器的前端同款：contenteditable 粘贴与
 *   v-html/打印预览前先过一遍。刻意不依赖 DOM（正则分词 + 标签栈重建），
 *   这样在 vitest 的 node 环境里也能直接单测。
 * - renderClassic 产出一份完整独立的打印 HTML（CSS 全内联，blob 新标签页里
 *   没有任何框架样式可用）。加模板时在这里新增 render 函数，与后端 TEMPLATES 一起改。
 */
import type {
  CompanyEntry,
  EducationEntry,
  EntryModuleKey,
  ProjectEntry,
  ResumeDocData,
  ResumeModuleKey,
} from './resumeDoc'
import { MODULE_LABELS, THEME_OPTIONS, type ThemeOption } from './resumeDoc'

// ---- 富文本消毒 ---------------------------------------------------------------

const ALLOWED_TAGS = new Set([
  'p',
  'br',
  'div',
  'span',
  'strong',
  'b',
  'em',
  'i',
  'u',
  'ul',
  'ol',
  'li',
  'a',
])
const VOID_TAGS = new Set(['br'])
// 这些标签连同其原文内容整体丢弃。
const RAW_DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed'])
const SAFE_HREF = /^\s*(?:https?|mailto|tel):/i
const HTML_LIMIT = 20_000

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
  colon: ':',
  tab: '\t',
  newline: '\n',
  semi: ';',
  equals: '=',
  sol: '/',
}

/** 把 HTML 实体还原成字符（属性协议检测与文本转义前都要先还原）。 */
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

function escapeText(input: string): string {
  return decodeEntities(input)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

const TOKEN_RE = /<!--[\s\S]*?-->|<!DOCTYPE[^>]*>|<\/?[a-zA-Z][^>]*>?|[^<]+/g
const TAG_NAME_RE = /^<\s*(\/?)\s*([a-zA-Z][a-zA-Z0-9]*)/

function attrValue(tagText: string, name: string): string {
  const m = tagText.match(
    new RegExp(`${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i'),
  )
  return m ? (m[1] ?? m[2] ?? m[3] ?? '') : ''
}

/**
 * 把任意 HTML 重建成白名单内、标签平衡的片段。语义与后端 _HtmlSanitizer 一致：
 * 非白名单标签丢标签留文本；script/style 等连内容一起丢；<a> 只放行
 * http(s)/mailto/tel；所有属性（style/class/on*）都不保留。
 */
export function sanitizeRichHtml(raw: unknown): string {
  if (typeof raw !== 'string' || !raw) return ''
  const parts: string[] = []
  const stack: string[] = []
  let rawSkip = 0

  for (const match of raw.matchAll(TOKEN_RE)) {
    const token = match[0]
    if (token[0] !== '<') {
      // 文本节点。
      if (!rawSkip) parts.push(escapeText(token))
      continue
    }
    if (token.startsWith('<!--') || /^<!/i.test(token)) continue

    const parsed = token.match(TAG_NAME_RE)
    if (!parsed) {
      if (!rawSkip) parts.push(escapeText(token))
      continue
    }
    const isClose = parsed[1] === '/'
    const tag = parsed[2].toLowerCase()

    if (RAW_DROP_TAGS.has(tag)) {
      if (isClose) rawSkip = Math.max(0, rawSkip - 1)
      else rawSkip += 1
      continue
    }
    if (rawSkip) continue
    if (!ALLOWED_TAGS.has(tag)) continue

    if (isClose) {
      if (VOID_TAGS.has(tag)) continue
      // 漏闭合时从栈顶一路关到匹配的那个，保证输出标签平衡。
      if (stack.includes(tag)) {
        while (stack.length) {
          const opened = stack.pop() as string
          parts.push(`</${opened}>`)
          if (opened === tag) break
        }
      }
      continue
    }

    const selfClosing = token.trimEnd().endsWith('/>')
    if (tag === 'br' || (selfClosing && VOID_TAGS.has(tag))) {
      parts.push('<br>')
      continue
    }
    if (selfClosing) continue // 非白名单 void（如 <img/>）上面已丢

    if (tag === 'a') {
      const href = decodeEntities(attrValue(token, 'href') || '').trim()
      if (!SAFE_HREF.test(href)) continue // 危险链接：丢标签留文本
      parts.push(`<a href="${href.replace(/"/g, '&quot;')}">`)
    } else {
      parts.push(`<${tag}>`)
    }
    stack.push(tag)
  }

  while (stack.length) parts.push(`</${stack.pop()}>`)
  return parts.join('').slice(0, HTML_LIMIT)
}

/** 消毒后拍成纯文本（空判断、占位提示用）。 */
export function plainTextOfHtml(html: string): string {
  return sanitizeRichHtml(html)
    .replace(/<br\s*\/?>/gi, ' ')
    .replace(/<[^>]*>/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

// ---- 小工具 -------------------------------------------------------------------

export function monthRangeLabel(start: string, end: string, current: boolean): string {
  if (current) return `${start || '?'} 至今`
  if (start || end) return `${start || '?'} - ${end || '?'}`
  return ''
}

function escapeAttr(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

type AnyEntry = CompanyEntry | ProjectEntry | EducationEntry

function entryTexts(kind: EntryModuleKey, entry: AnyEntry): string[] {
  if (kind === 'companies') {
    const e = entry as CompanyEntry
    return [e.company, e.position, e.department]
  }
  if (kind === 'projects') {
    const e = entry as ProjectEntry
    return [e.name, e.role, e.department]
  }
  const e = entry as EducationEntry
  return [e.school, e.major, e.college, e.degree, e.study_mode]
}

/** 没有任何可展示内容的条目在导出时整条隐藏。 */
export function entryIsEmpty(kind: EntryModuleKey, entry: AnyEntry): boolean {
  return (
    entryTexts(kind, entry).every((v) => !v?.trim()) &&
    !entry.start &&
    !entry.end &&
    !plainTextOfHtml(entry.description_html)
  )
}

export function isModuleEmpty(doc: ResumeDocData, key: ResumeModuleKey): boolean {
  if (key === 'profile') return !plainTextOfHtml(doc.profile.summary_html)
  if (key === 'skills') return !plainTextOfHtml(doc.skills_html)
  return !doc[key].some((entry) => !entryIsEmpty(key, entry as AnyEntry))
}

// ---- 模板与配色 ---------------------------------------------------------------
//
// 版式（template）决定结构与 CSS，配色（theme）只注入一组 CSS 变量，二者正交：
// classic 通栏抬头单栏 / sidebar 左侧深色名片栏 / minimal 克制用色线条分区。
// 加配色只改 resumeDoc.ts 的 THEME_OPTIONS（后端 THEMES 同步）；加版式新增一套
// CSS + render 函数并在 renderDoc 分派。

const THEME_BY_KEY = new Map(THEME_OPTIONS.map((item) => [item.value, item]))

function resolveTheme(key: string | undefined): ThemeOption {
  return THEME_BY_KEY.get(key || '') ?? THEME_OPTIONS[0]
}

const BASE_CSS = `
* { box-sizing: border-box; }
body { margin: 0; background: #eef0f3; font-family: "Microsoft YaHei","PingFang SC","Segoe UI",sans-serif; color: #1f2329; }
.sheet { width: 210mm; min-height: 297mm; margin: 0 auto; background: #fff; }
.rb-entry-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.rb-entry-title { font-size: 13.5px; font-weight: 600; }
.rb-entry-title .rb-sub { margin-left: 8px; font-weight: 400; color: #4b5563; }
.rb-period { font-size: 12px; color: #6b7280; white-space: nowrap; }
.rb-dept { margin-top: 2px; font-size: 12px; color: #8a8f99; }
.rb-desc { margin-top: 4px; font-size: 12.5px; line-height: 1.7; }
.rb-desc p { margin: 0 0 4px; }
.rb-desc ul,.rb-desc ol { margin: 2px 0 4px; padding-left: 20px; }
.rb-desc li { margin-bottom: 2px; }
.rb-section, .rb-entry { break-inside: avoid; page-break-inside: avoid; }
@media print {
  body { background: #fff; }
  .sheet { width: auto; min-height: 0; margin: 0; }
  @page { size: A4; margin: 14mm 13mm; }
}
`

const CLASSIC_CSS = `
.sheet { padding: 16mm 15mm; }
.rb-header { margin-bottom: 14px; padding-bottom: 12px; border-bottom: 2.5px solid var(--accent); }
.rb-name { font-size: 28px; font-weight: 700; line-height: 1.2; color: var(--accent); }
.rb-target { margin-top: 6px; font-size: 14px; color: #4b5563; }
.rb-intro { margin-top: 8px; font-size: 12.5px; line-height: 1.65; color: #6b7280; }
.rb-info { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12px; color: #374151; }
.rb-contacts { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12px; color: #374151; }
.rb-info .rb-c-label, .rb-contacts .rb-c-label { font-style: normal; margin-right: 5px; color: #9ca3af; }
.rb-contacts a { color: var(--accent); text-decoration: none; }
.rb-section { margin-top: 16px; }
.rb-section h2 { margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid #d9d9d9; font-size: 15px; color: var(--accent); letter-spacing: 1px; }
.rb-entry { margin-bottom: 12px; }
.rb-desc a { color: var(--accent); text-decoration: none; }
@media print {
  .sheet { padding: 0; }
}
`

const MINIMAL_CSS = `
.sheet { padding: 18mm 16mm; }
.rb-header { margin-bottom: 18px; padding-bottom: 14px; }
.rb-name { font-size: 30px; font-weight: 700; line-height: 1.2; color: #1f2329; letter-spacing: 2px; }
.rb-name-bar { width: 38px; height: 3px; margin-top: 10px; background: var(--accent); }
.rb-target { margin-top: 10px; font-size: 13.5px; color: #374151; }
.rb-intro { margin-top: 8px; font-size: 12.5px; line-height: 1.65; color: #6b7280; }
.rb-info { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12px; color: #4b5563; }
.rb-contacts { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12px; color: #4b5563; }
.rb-info .rb-c-label, .rb-contacts .rb-c-label { font-style: normal; margin-right: 5px; color: #aab0b8; }
.rb-contacts a { color: var(--accent); text-decoration: none; }
.rb-section { margin-top: 18px; }
.rb-section h2 { margin: 0 0 10px; padding-left: 9px; border-left: 3px solid var(--accent); border-bottom: 1px solid #ececec; padding-bottom: 5px; font-size: 13.5px; font-weight: 600; color: #374151; letter-spacing: 2px; }
.rb-entry { margin-bottom: 13px; }
.rb-entry-title { color: #1f2329; }
.rb-desc a { color: var(--accent); text-decoration: none; }
@media print {
  .sheet { padding: 0; }
}
`

const SIDEBAR_CSS = `
.sheet { display: flex; align-items: stretch; }
.rb-aside { width: 34%; background: var(--accent-dark); color: #fff; padding: 18mm 9mm 14mm 11mm; }
.rb-main { flex: 1; padding: 18mm 11mm 14mm 10mm; }
.rb-aside-name { font-size: 24px; font-weight: 700; line-height: 1.25; }
.rb-aside-target { margin-top: 6px; font-size: 13px; color: rgba(255,255,255,.82); }
.rb-aside-intro { margin-top: 10px; font-size: 12px; line-height: 1.7; color: rgba(255,255,255,.78); }
.rb-aside-block { margin-top: 18px; }
.rb-contact .rb-c-value a { color: #fff; text-decoration: none; }
.rb-aside-label { margin: 0 0 8px; padding-bottom: 5px; border-bottom: 1px solid rgba(255,255,255,.28); font-size: 12px; letter-spacing: 2px; color: rgba(255,255,255,.78); }
.rb-contact { display: flex; gap: 8px; margin-bottom: 7px; font-size: 12px; line-height: 1.5; }
.rb-contact .rb-c-label { flex: 0 0 48px; color: rgba(255,255,255,.6); }
.rb-contact .rb-c-value { min-width: 0; color: rgba(255,255,255,.94); word-break: break-all; }
.rb-aside .rb-desc, .rb-aside .rb-desc li { color: rgba(255,255,255,.9); }
.rb-aside .rb-desc a { color: #fff; }
.rb-main .rb-section { margin-top: 15px; }
.rb-main .rb-section:first-child { margin-top: 0; }
.rb-main .rb-section h2 { margin: 0 0 8px; padding-bottom: 4px; border-bottom: 1px solid #d9d9d9; font-size: 14.5px; color: var(--accent); letter-spacing: 1px; }
.rb-entry { margin-bottom: 12px; }
.rb-desc a { color: var(--accent); text-decoration: none; }
.rb-aside, .rb-main { break-inside: auto; }
@media print {
  .sheet { min-height: 0; }
  @page { size: A4; margin: 0; }
}
`

interface ContactItem {
  label: string
  value: string
  /** 联系方式项渲染成可点链接（个人网站）。 */
  href?: string
}

function escapeHref(raw: string): string {
  return escapeAttr(raw).replace(/"/g, '&quot;')
}

/** 用户可能只填 me.dev，预览时补成 https://me.dev。 */
function websiteHref(raw: string): string {
  const value = raw.trim()
  return /^https?:\/\//i.test(value) ? value : `https://${value}`
}

/** 抬头第一行：性别 / 出生年月 / 城市（基本信息）。 */
function basicItems(doc: ResumeDocData): ContactItem[] {
  const p = doc.profile
  const items: ContactItem[] = []
  if (p.gender) items.push({ label: '性别', value: p.gender })
  if (p.birthday) items.push({ label: '出生年月', value: p.birthday })
  if (p.city) items.push({ label: '城市', value: p.city })
  return items
}

/** 抬头第二行：电话 / 邮箱 / 个人网站（联系方式）。 */
function contactItems(doc: ResumeDocData): ContactItem[] {
  const p = doc.profile
  const items: ContactItem[] = []
  if (p.phone) items.push({ label: '电话', value: p.phone })
  if (p.email) items.push({ label: '邮箱', value: p.email })
  if (p.website) {
    items.push({ label: '个人网站', value: p.website, href: websiteHref(p.website) })
  }
  return items
}

function inlineItemHtml(it: ContactItem): string {
  const value = it.href
    ? `<a href="${escapeHref(it.href)}" target="_blank" rel="noopener">${escapeAttr(it.value)}</a>`
    : escapeAttr(it.value)
  return `<span><i class="rb-c-label">${escapeAttr(it.label)}</i>${value}</span>`
}

function infoLineHtml(className: string, items: ContactItem[]): string {
  if (!items.length) return ''
  return `<div class="${className}">${items.map(inlineItemHtml).join('')}</div>`
}

function headerHtml(doc: ResumeDocData): string {
  const p = doc.profile
  const basic = basicItems(doc)
  const contacts = contactItems(doc)
  if (!p.name && !p.job_target && !p.intro && basic.length === 0 && contacts.length === 0) return ''
  const parts = ['<header class="rb-header">']
  if (p.name) {
    parts.push(`<div class="rb-name">${escapeAttr(p.name)}</div>`)
    parts.push('<div class="rb-name-bar"></div>') // 仅简约模板给尺寸；经典下无样式
  }
  if (p.job_target) parts.push(`<div class="rb-target">求职意向：${escapeAttr(p.job_target)}</div>`)
  if (p.intro) parts.push(`<div class="rb-intro">${escapeAttr(p.intro)}</div>`)
  parts.push(infoLineHtml('rb-info', basic))
  parts.push(infoLineHtml('rb-contacts', contacts))
  parts.push('</header>')
  return parts.join('')
}

function entryHtml(kind: EntryModuleKey, entry: AnyEntry): string {
  const titleParts: [string, boolean][] =
    kind === 'companies'
      ? [
          [(entry as CompanyEntry).company, true],
          [(entry as CompanyEntry).position, false],
        ]
      : kind === 'projects'
        ? [
            [(entry as ProjectEntry).name, true],
            [(entry as ProjectEntry).role, false],
          ]
        : [
            [(entry as EducationEntry).school, true],
            [(entry as EducationEntry).degree, false],
            [(entry as EducationEntry).major, false],
          ]
  const title = titleParts
    .filter(([v]) => v)
    .map(([v, strong], i) =>
      strong && i === 0 ? escapeAttr(v) : `<span class="rb-sub">${escapeAttr(v)}</span>`,
    )
    .join('')
  const period = monthRangeLabel(entry.start, entry.end, entry.current)
  const dept =
    kind === 'educations'
      ? (entry as EducationEntry).college
      : kind === 'companies'
        ? (entry as CompanyEntry).department
        : (entry as ProjectEntry).department
  const desc = sanitizeRichHtml(entry.description_html)

  return (
    '<div class="rb-entry">' +
    '<div class="rb-entry-head">' +
    `<span class="rb-entry-title">${title}</span>` +
    (period ? `<span class="rb-period">${escapeAttr(period)}</span>` : '') +
    '</div>' +
    (dept ? `<div class="rb-dept">${escapeAttr(dept)}</div>` : '') +
    (plainTextOfHtml(desc) ? `<div class="rb-desc">${desc}</div>` : '') +
    '</div>'
  )
}

function sectionHtml(doc: ResumeDocData, key: ResumeModuleKey): string {
  if (key === 'profile') {
    const summary = sanitizeRichHtml(doc.profile.summary_html)
    if (!plainTextOfHtml(summary)) return ''
    return `<section class="rb-section"><h2>个人优势</h2><div class="rb-desc">${summary}</div></section>`
  }
  if (key === 'skills') {
    const skills = sanitizeRichHtml(doc.skills_html)
    if (!plainTextOfHtml(skills)) return ''
    return `<section class="rb-section"><h2>${MODULE_LABELS.skills}</h2><div class="rb-desc">${skills}</div></section>`
  }
  const entries = (doc[key] as AnyEntry[])
    .filter((entry) => !entryIsEmpty(key, entry as AnyEntry))
    .map((entry) => entryHtml(key, entry as AnyEntry))
  if (entries.length === 0) return ''
  return `<section class="rb-section"><h2>${MODULE_LABELS[key]}</h2>${entries.join('')}</section>`
}

function orderedSections(doc: ResumeDocData, keys: ResumeModuleKey[]): string {
  return doc.module_order
    .filter((key) => keys.includes(key) && MODULE_LABELS[key])
    .map((key) => sectionHtml(doc, key))
    .filter(Boolean)
    .join('\n')
}

function pageDocument(title: string, css: string, body: string, theme: ThemeOption): string {
  const vars = `:root{--accent:${theme.accent};--accent-dark:${theme.dark};--accent-soft:${theme.soft};}`
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>${escapeAttr(title)}</title>
<style>${vars}
${BASE_CSS}
${css}</style>
</head>
<body>
${body}
</body>
</html>`
}

/**
 * 经典模板：通栏抬头 + 单栏按 module_order 排布。
 * 个人信息头部固定最上方（简历惯例），其富文本「个人优势」仍按 module_order 排位。
 */
export function renderClassic(doc: ResumeDocData): string {
  const theme = resolveTheme(doc.theme)
  const body = `<div class="sheet">
${headerHtml(doc)}
${orderedSections(doc, ['profile', 'companies', 'projects', 'educations', 'skills'])}
</div>`
  return pageDocument(doc.profile.name || '我的简历', CLASSIC_CSS, body, theme)
}

/** 简约模板：同样单栏，但去大色块，仅以色条/色线点缀，黑白灰为主。 */
function renderMinimal(doc: ResumeDocData): string {
  const theme = resolveTheme(doc.theme)
  const body = `<div class="sheet">
${headerHtml(doc)}
${orderedSections(doc, ['profile', 'companies', 'projects', 'educations', 'skills'])}
</div>`
  return pageDocument(doc.profile.name || '我的简历', MINIMAL_CSS, body, theme)
}

/** 双栏名片：左深色名片栏（身份 + 联系方式 + 技能），右栏其余模块按 module_order 排。 */
function renderSidebar(doc: ResumeDocData): string {
  const theme = resolveTheme(doc.theme)
  const p = doc.profile
  const contacts = contactItems(doc)
  const skills = sanitizeRichHtml(doc.skills_html)

  const asideParts = ['<aside class="rb-aside">']
  if (p.name) asideParts.push(`<div class="rb-aside-name">${escapeAttr(p.name)}</div>`)
  if (p.job_target) {
    asideParts.push(`<div class="rb-aside-target">求职意向：${escapeAttr(p.job_target)}</div>`)
  }
  if (p.intro) asideParts.push(`<div class="rb-aside-intro">${escapeAttr(p.intro)}</div>`)
  const basic = basicItems(doc)
  if (basic.length) {
    asideParts.push(
      '<div class="rb-aside-block"><div class="rb-aside-label">基本信息</div>' +
        basic
          .map(
            (it) =>
              `<div class="rb-contact"><span class="rb-c-label">${escapeAttr(it.label)}</span>` +
              `<span class="rb-c-value">${escapeAttr(it.value)}</span></div>`,
          )
          .join('') +
        '</div>',
    )
  }
  if (contacts.length) {
    asideParts.push(
      '<div class="rb-aside-block"><div class="rb-aside-label">联系方式</div>' +
        contacts
          .map((it) => {
            const value = it.href
              ? `<a href="${escapeHref(it.href)}" target="_blank" rel="noopener">${escapeAttr(it.value)}</a>`
              : escapeAttr(it.value)
            return (
              `<div class="rb-contact"><span class="rb-c-label">${escapeAttr(it.label)}</span>` +
              `<span class="rb-c-value">${value}</span></div>`
            )
          })
          .join('') +
        '</div>',
    )
  }
  if (plainTextOfHtml(skills)) {
    asideParts.push(
      '<div class="rb-aside-block"><div class="rb-aside-label">专业技能</div>' +
        `<div class="rb-desc">${skills}</div></div>`,
    )
  }
  asideParts.push('</aside>')

  // 身份信息与技能已由名片栏承载，主栏不再重复；模块相对顺序仍遵循 module_order。
  const mainSections = orderedSections(doc, ['profile', 'companies', 'projects', 'educations'])
  const body = `<div class="sheet">
${asideParts.join('\n')}
<main class="rb-main">
${mainSections}
</main>
</div>`
  return pageDocument(doc.profile.name || '我的简历', SIDEBAR_CSS, body, theme)
}

/** 按 doc.template 分派到具体版式；未知值回退经典。空模块（含全空条目）各版式都会隐藏。 */
export function renderDoc(doc: ResumeDocData): string {
  if (doc.template === 'sidebar') return renderSidebar(doc)
  if (doc.template === 'minimal') return renderMinimal(doc)
  return renderClassic(doc)
}
