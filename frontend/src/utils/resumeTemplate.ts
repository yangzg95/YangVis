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
  'blockquote',
])
const VOID_TAGS = new Set(['br'])
// 这些标签连同其原文内容整体丢弃。
const RAW_DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed'])
const SAFE_HREF = /^\s*(?:https?|mailto|tel):/i
const HTML_LIMIT = 20_000

// 富文本工具栏的对齐/缩进要能在导出里活下来，所以块级标签上单独提取这两样，
// 归一成受控 class（绝不透传 style）。后端 _HtmlSanitizer 有同一套规则，改这里要同步。
const ALIGN_TAGS = new Set(['p', 'div', 'ul', 'ol', 'li', 'blockquote'])
const ALIGN_CLASSES = new Map([
  ['center', 'rb-ta-center'],
  ['right', 'rb-ta-right'],
  ['justify', 'rb-ta-justify'],
])
const TEXT_ALIGN_RE = /text-align\s*:\s*([a-z-]+)/i

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
  // 名字前必须是标签内的分隔符，否则 data-href / xhref 这类会被误读成 href。
  const m = tagText.match(
    new RegExp(`(?:^|[^\\w-])${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s>]+))`, 'i'),
  )
  return m ? (m[1] ?? m[2] ?? m[3] ?? '') : ''
}

/** 取块级标签的对齐方式：style 优先，兼容旧式 align 属性；left 是默认值不出 class。 */
function alignClass(tagText: string): string {
  const style = attrValue(tagText, 'style')
  const matched = style.match(TEXT_ALIGN_RE)
  const raw = (matched ? matched[1] : attrValue(tagText, 'align')).trim().toLowerCase()
  return ALIGN_CLASSES.get(raw) ?? ''
}

/**
 * 把任意 HTML 重建成白名单内、标签平衡的片段。语义与后端 _HtmlSanitizer 一致：
 * 非白名单标签丢标签留文本；script/style 等连内容一起丢；<a> 只放行
 * http(s)/mailto/tel；属性不保留，只有块级标签的对齐会被提取成受控 class。
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
      // 缩进只保留一层：连续按增加缩进会套娃 blockquote，版面就失控了。
      if (tag === 'blockquote' && stack.includes('blockquote')) continue
      const cls = ALIGN_TAGS.has(tag) ? alignClass(token) : ''
      parts.push(cls ? `<${tag} class="${cls}">` : `<${tag}>`)
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

/** 缺失的一端直接不渲染：打「? 至今」这类占位符会跟着简历投出去。 */
export function monthRangeLabel(start: string, end: string, current: boolean): string {
  const from = start?.trim() ?? ''
  const to = end?.trim() ?? ''
  if (current) return from ? `${from} 至今` : '至今'
  return [from, to].filter(Boolean).join(' - ')
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
/* print-color-adjust 是继承属性：写在 body 上，Chrome/Edge 即使不勾「背景图形」
   也会打出双栏名片的深色侧栏与色条，否则侧栏变白底、上面的白字全部隐形。 */
body { margin: 0; background: #eef0f3; font-family: "Microsoft YaHei","PingFang SC","Segoe UI",sans-serif; color: #1f2329; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
.sheet { width: 210mm; min-height: 297mm; margin: 0 auto; background: #fff; }
.rb-entry-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
/* min-width:0 让标题这一列可以被压，长英文公司名才不至于把右侧日期挤出页边。 */
.rb-entry-title { font-size: 13.5px; font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
.rb-entry-title .rb-sub { margin-left: 8px; font-weight: 400; color: #4b5563; }
.rb-period { font-size: 12px; color: #6b7280; white-space: nowrap; }
.rb-dept { margin-top: 3px; font-size: 12px; color: #8a8f99; }
/* 竖向台阶：行框 21.25px(5.6mm)，所以段间距给到 7px、列表项 4px，换段才明显比换行
   松一档；条目/节间距由各自版式再往上加一档（16px / 24px 量级）。之前段间距只有
   4px、列表项 2px，和行距差不到 1mm，整块描述在 A4 上会糊成一团。 */
.rb-desc { margin-top: 7px; font-size: 12.5px; line-height: 1.7; }
.rb-desc p { margin: 0 0 7px; }
.rb-desc ul,.rb-desc ol { margin: 6px 0 7px; padding-left: 20px; }
.rb-desc li { margin-bottom: 4px; }
.rb-desc p,.rb-desc li { orphans: 2; widows: 2; }
/* 消毒器把工具栏的对齐归一成这三个 class；缩进走 blockquote 的一层左边距。 */
.rb-ta-center { text-align: center; }
.rb-ta-right { text-align: right; }
.rb-ta-justify { text-align: justify; }
.rb-desc blockquote { margin: 6px 0 7px; padding-left: 20px; }
/* 头部裸值行里的长邮箱/网址不设溢出策略就会顶破正文宽度（列收缩后只能整体溢出）。 */
.rb-info,.rb-contacts { overflow-wrap: anywhere; }
/* 头部两列（classic/minimal 用；双栏名片自己排 aside，不走这里）：左列姓名+求职意向，
   右列基本信息/联系方式右对齐。两列不许换成两行——意向一长，flex-wrap 会把整块联系信息
   甩到姓名下面独占一行，两行右对齐的裸值看着像另一段；改为列宽按比例收缩、文字在列内折行。 */
.rb-header-top { display: flex; align-items: flex-end; justify-content: space-between; gap: 8px 24px; }
.rb-header-id,.rb-header-meta { min-width: 0; }
/* 联系方式的 label 是行内小灰字（<i> 默认斜体要抹掉）：值本身字号一样，只靠颜色退后，
   这样「电话 13882100882」仍然是一眼能读的一项，不会变成另一层标题。 */
.rb-header-meta .rb-c-label { margin-right: 4px; font-style: normal; color: #8a8f99; }
/* 断页单位只到「抬头 + 部门」这一小段。整条 entry 上 avoid 看着贴心，实际是坑：
   带背景/技术线/负责内容的条目有 40~60mm 高，装不下就整块跳到下一页，上一页留下
   半页空白（实测页尾洞占掉 55% 页高）。条目内部按段流动，再用 orphans/widows 和
   这里的 avoid-after 兜住「标题单独掉在页尾」。 */
.rb-entry-head,.rb-dept {
  break-inside: avoid;
  break-after: avoid;
  page-break-inside: avoid;
  page-break-after: avoid;
}
.rb-section h2 { break-after: avoid; page-break-after: avoid; }
@media print {
  body { background: #fff; }
  .sheet { width: auto; min-height: 0; margin: 0; }
  /* 页边距与 classic 的屏显 padding 同数：打印时 .sheet padding 归零、由 @page
     接手，两边一致预览才量得出真实分页位置。minimal/sidebar 自带 @page 覆盖。 */
  @page { size: A4; margin: 15mm 14mm; }
}
`

const CLASSIC_CSS = `
/* 与 BASE_CSS 的 @page margin 同数（见那里的注释）。 */
.sheet { padding: 15mm 14mm; }
.rb-header { margin-bottom: 18px; padding-bottom: 14px; border-bottom: 2px solid var(--accent); }
/* 头部两列的骨架在 BASE_CSS；这里只给配色与字号。原来五块内容全部左堆等距，
   姓名 28px 被两行小灰字淹掉，右侧空掉 2/3，所以姓名放大到 34px。 */
.rb-name { font-size: 34px; font-weight: 700; line-height: 1.15; letter-spacing: 3px; color: var(--accent); }
.rb-target { margin-top: 8px; font-size: 13.5px; color: #4b5563; }
.rb-header-meta { font-size: 12.5px; line-height: 1.9; color: #374151; text-align: right; }
.rb-header-meta .rb-sep { margin: 0 7px; color: #c3c8d0; }
.rb-intro { margin-top: 12px; font-size: 12.5px; line-height: 1.75; color: #4b5563; }
.rb-contacts a { color: var(--accent); text-decoration: none; }
.rb-section { margin-top: 24px; }
/* 节标题用浅色底 + 主色左竖条：--accent-soft 之前注入了却没人用，配色选择器等于
   只有一半生效；炭灰这类低饱和主题也靠这条竖条撑出层次。 */
.rb-section h2 { margin: 0 0 10px; padding: 3px 8px; background: var(--accent-soft); border-left: 3px solid var(--accent); border-radius: 2px; font-size: 15px; color: var(--accent); letter-spacing: 1px; }
.rb-entry { margin-bottom: 14px; }
.rb-desc a { color: var(--accent); text-decoration: none; }
@media print {
  .sheet { padding: 0; }
}
`

const MINIMAL_CSS = `
/* 留白比经典更松；屏显 padding 与本模板 @page 里的 margin 同数。 */
.sheet { padding: 18mm 16mm; }
.rb-header { margin-bottom: 22px; padding-bottom: 16px; }
.rb-name { font-size: 32px; font-weight: 700; line-height: 1.15; color: #1f2329; letter-spacing: 3px; }
.rb-name-bar { width: 38px; height: 3px; margin-top: 12px; background: var(--accent); }
.rb-target { margin-top: 10px; font-size: 13.5px; color: #374151; }
.rb-header-meta { font-size: 12.5px; line-height: 1.9; color: #4b5563; text-align: right; }
.rb-header-meta .rb-sep { margin: 0 7px; color: #c9ced6; }
.rb-intro { margin-top: 12px; font-size: 12.5px; line-height: 1.75; color: #6b7280; }
.rb-contacts a { color: var(--accent); text-decoration: none; }
.rb-section { margin-top: 26px; }
.rb-section h2 { margin: 0 0 12px; padding-left: 9px; border-left: 3px solid var(--accent); border-bottom: 1px solid #ececec; padding-bottom: 5px; font-size: 13.5px; font-weight: 600; color: #374151; letter-spacing: 2px; }
.rb-entry { margin-bottom: 15px; }
.rb-entry-title { color: #1f2329; }
.rb-desc a { color: var(--accent); text-decoration: none; }
@media print {
  .sheet { padding: 0; }
  /* 覆盖 BASE 的 @page：简约版留白更松，屏显与打印都用这一组数。 */
  @page { size: A4; margin: 18mm 16mm; }
}
`

const SIDEBAR_CSS = `
.sheet { display: flex; align-items: stretch; }
.rb-aside { width: 34%; background: var(--accent-dark); color: #fff; padding: 18mm 9mm 14mm 11mm; }
.rb-main { flex: 1; padding: 18mm 11mm 14mm 10mm; }
.rb-aside-name { font-size: 24px; font-weight: 700; line-height: 1.25; }
.rb-aside-target { margin-top: 8px; font-size: 13px; color: rgba(255,255,255,.82); }
.rb-aside-intro { margin-top: 12px; font-size: 12px; line-height: 1.7; color: rgba(255,255,255,.78); }
.rb-aside-block { margin-top: 24px; }
.rb-contact .rb-c-value a { color: #fff; text-decoration: none; }
.rb-aside-label { margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1px solid rgba(255,255,255,.28); font-size: 12px; letter-spacing: 2px; color: rgba(255,255,255,.78); }
.rb-contact { display: flex; gap: 8px; margin-bottom: 9px; font-size: 12px; line-height: 1.5; }
.rb-contact .rb-c-label { flex: 0 0 48px; color: rgba(255,255,255,.6); }
.rb-contact .rb-c-value { min-width: 0; color: rgba(255,255,255,.94); word-break: break-all; }
.rb-aside .rb-desc, .rb-aside .rb-desc li { color: rgba(255,255,255,.9); }
.rb-aside .rb-desc a { color: #fff; }
.rb-main .rb-section { margin-top: 22px; }
.rb-main .rb-section:first-child { margin-top: 0; }
.rb-main .rb-section h2 { margin: 0 0 10px; padding: 3px 8px; background: var(--accent-soft); border-left: 3px solid var(--accent); border-radius: 2px; font-size: 14.5px; color: var(--accent); letter-spacing: 1px; }
.rb-entry { margin-bottom: 14px; }
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

/** 头部的一行：项间用「·」分隔。
 *  @param withLabel 联系方式行要带「电话/邮箱」字样——13882100882 裸放着没人知道是电话；
 *  基本信息行（男 / 1995-06 / 上海）继续裸值，那几个值靠形态就能认出来，加 label 反而
 *  把右列撑得更宽、姓名被压得更窄。双栏名片栏是竖排，自己每项一行都带 label。 */
function infoLineHtml(className: string, items: ContactItem[], withLabel: boolean): string {
  if (!items.length) return ''
  const values = items.map((it) => {
    const value = it.href
      ? `<a href="${escapeHref(it.href)}" target="_blank" rel="noopener">${escapeAttr(it.value)}</a>`
      : escapeAttr(it.value)
    return withLabel ? `<span><i class="rb-c-label">${escapeAttr(it.label)}</i>${value}</span>` : `<span>${value}</span>`
  })
  return `<div class="${className}">${values.join('<span class="rb-sep">·</span>')}</div>`
}

function headerHtml(doc: ResumeDocData): string {
  const p = doc.profile
  const basic = basicItems(doc)
  const contacts = contactItems(doc)
  if (!p.name && !p.job_target && !p.intro && basic.length === 0 && contacts.length === 0) return ''
  const idParts: string[] = []
  if (p.name) {
    idParts.push(`<div class="rb-name">${escapeAttr(p.name)}</div>`)
    idParts.push('<div class="rb-name-bar"></div>') // 仅简约模板给尺寸；经典下无样式
  }
  if (p.job_target) idParts.push(`<div class="rb-target">求职意向：${escapeAttr(p.job_target)}</div>`)
  // 联系方式排在基本信息上面：这一行才是招聘方要抄走的，裸值那行只是补充画像。
  const metaParts = [infoLineHtml('rb-contacts', contacts, true), infoLineHtml('rb-info', basic, false)].join('')
  const parts = ['<header class="rb-header">', '<div class="rb-header-top">']
  parts.push(idParts.length ? `<div class="rb-header-id">${idParts.join('')}</div>` : '')
  parts.push(metaParts ? `<div class="rb-header-meta">${metaParts}</div>` : '')
  parts.push('</div>')
  if (p.intro) parts.push(`<div class="rb-intro">${escapeAttr(p.intro)}</div>`)
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
  // 教育条目的副行：学院 + 就读形式，否则「全日制」填了没地方显示。
  const dept =
    kind === 'educations'
      ? [(entry as EducationEntry).college, (entry as EducationEntry).study_mode]
          .filter((v) => v?.trim())
          .join(' · ')
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

const MM_PX = 96 / 25.4

/** 每套版式的分页几何（单位 mm）。打印时 .sheet 的 padding 归零、边距交给 @page，
 *  一页装得下的流内容高度是 297mm 减去上下边距（classic 267 / minimal 261 / sidebar
 *  297，用 Edge 打印引擎逐毫米实测过）；屏显同一段流内容却从 .sheet 顶部留白之后才
 *  开始。所以分页线 = 顶部留白 + n*页内容高，不能按整页高的倍数画。 */
const PAGE_GEOMETRY = new Map<string, { topMm: number; bottomMm: number; strideMm: number }>([
  ['classic', { topMm: 15, bottomMm: 15, strideMm: 267 }],
  ['minimal', { topMm: 18, bottomMm: 18, strideMm: 261 }],
  ['sidebar', { topMm: 18, bottomMm: 14, strideMm: 297 }],
])

/** 第 2 页起每页在屏显 .sheet 坐标系里的顶部像素位置；一页放得下即空数组。 */
export function pageBreakTops(template: string, contentHeightPx: number): number[] {
  const geo = PAGE_GEOMETRY.get(template) ?? PAGE_GEOMETRY.get('classic')!
  const tops: number[] = []
  // 线必须落在真实内容里：末尾那段留白不算下一页的开头。
  const limit = contentHeightPx - geo.bottomMm * MM_PX - 2
  for (let page = 2; ; page += 1) {
    const top = (geo.topMm + (page - 1) * geo.strideMm) * MM_PX
    if (top >= limit) return tops
    tops.push(top)
  }
}
