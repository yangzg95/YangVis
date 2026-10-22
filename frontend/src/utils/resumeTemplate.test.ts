import { describe, expect, it } from 'vitest'
import {
  entryIsEmpty,
  isModuleEmpty,
  monthRangeLabel,
  pageBreakTops,
  plainTextOfHtml,
  renderClassic,
  renderDoc,
  sanitizeRichHtml,
} from './resumeTemplate'
import { THEME_OPTIONS } from './resumeDoc'
import { createEmptyDoc, type ResumeDocData } from './resumeDoc'

describe('sanitizeRichHtml', () => {
  it('剥掉 script 原文、事件属性与非白名单标签', () => {
    const out = sanitizeRichHtml(
      '<p>hi<script>alert(1)</script><img src=x onerror="alert(1)"><b onclick="x()">bold</b></p>',
    )
    expect(out).not.toContain('<script')
    expect(out).not.toContain('alert')
    expect(out).not.toContain('onerror')
    expect(out).not.toContain('<img')
    expect(out).toContain('<b>bold</b>')
    expect(out.startsWith('<p>hi')).toBe(true)
  })

  it('危险协议链接只丢标签，文本保留', () => {
    expect(sanitizeRichHtml('<a href="https://a.com">x</a>')).toBe('<a href="https://a.com">x</a>')
    const out = sanitizeRichHtml('<a href="javascript:alert(1)">点我</a>')
    expect(out).not.toContain('<a')
    expect(out).not.toContain('javascript')
    expect(out).toContain('点我')
  })

  it('编码变形的 javascript: 同样拦住', () => {
    const out = sanitizeRichHtml('<a href="java&#115;cript:alert(1)">x</a>')
    expect(out).not.toContain('<a')
  })

  it('修补未闭合标签并转义裸文本', () => {
    const out = sanitizeRichHtml('<p>a<b>b<ul><li>c')
    expect(out).toBe('<p>a<b>b<ul><li>c</li></ul></b></p>')
    expect(sanitizeRichHtml('<p>a & b > c</p>')).toBe('<p>a &amp; b &gt; c</p>')
  })

  it('对齐归一成受控 class，与后端 _HtmlSanitizer 同规则', () => {
    expect(sanitizeRichHtml('<p style="text-align:center">a</p>')).toBe('<p class="rb-ta-center">a</p>')
    expect(sanitizeRichHtml("<div style='TEXT-ALIGN: right'>a</div>")).toBe('<div class="rb-ta-right">a</div>')
    expect(sanitizeRichHtml('<li align="justify">a</li>')).toBe('<li class="rb-ta-justify">a</li>')
    // left 是默认值不出 class；行内标签不吃对齐；用户自己的 class 依旧不透传。
    expect(sanitizeRichHtml('<p style="text-align:left">a</p>')).toBe('<p>a</p>')
    expect(sanitizeRichHtml('<span style="text-align:center">a</span>')).toBe('<span>a</span>')
    expect(sanitizeRichHtml('<p style="text-align:center" class="evil">a</p>')).toBe('<p class="rb-ta-center">a</p>')
    expect(sanitizeRichHtml('<p style="text-align:expression(alert(1))">a</p>')).toBe('<p>a</p>')
    expect(sanitizeRichHtml('<p data-align="right">a</p>')).toBe('<p>a</p>')
  })

  it('缩进只保留一层 blockquote', () => {
    expect(sanitizeRichHtml('<blockquote>a</blockquote>')).toBe('<blockquote>a</blockquote>')
    expect(sanitizeRichHtml('<blockquote><blockquote>a</blockquote></blockquote>')).toBe(
      '<blockquote>a</blockquote>',
    )
    expect(sanitizeRichHtml("<blockquote style='text-align:center'>a</blockquote>")).toBe(
      '<blockquote class="rb-ta-center">a</blockquote>',
    )
  })

  it('不保留 style/class，非字符串给空串', () => {
    expect(sanitizeRichHtml('<p style="x" class="y">a</p>')).toBe('<p>a</p>')
    expect(sanitizeRichHtml(undefined)).toBe('')
    expect(sanitizeRichHtml(123)).toBe('')
  })

  it('已有的实体先解码再转义，不会双重编码', () => {
    expect(sanitizeRichHtml('<p>a &amp; b</p>')).toBe('<p>a &amp; b</p>')
  })
})

describe('monthRangeLabel', () => {
  it('至今优先于结束月', () => {
    expect(monthRangeLabel('2019-03', '', true)).toBe('2019-03 至今')
    expect(monthRangeLabel('2019-03', '2021-01', true)).toBe('2019-03 至今')
  })
  it('普通区间与缺省', () => {
    expect(monthRangeLabel('2019-03', '2021-01', false)).toBe('2019-03 - 2021-01')
    expect(monthRangeLabel('2019-03', '', false)).toBe('2019-03')
    expect(monthRangeLabel('', '2021-01', false)).toBe('2021-01')
    expect(monthRangeLabel('', '', false)).toBe('')
  })
  it('缺开始时间时只丢那一端，不打问号占位', () => {
    expect(monthRangeLabel('', '', true)).toBe('至今')
    expect(monthRangeLabel('  ', '  ', true)).toBe('至今')
  })
})

describe('空模块/空条目判断', () => {
  it('全空文档每个模块都算空', () => {
    const doc = createEmptyDoc()
    for (const key of doc.module_order) {
      expect(isModuleEmpty(doc, key)).toBe(true)
    }
  })
  it('只有空白富文本也算空', () => {
    const doc = createEmptyDoc()
    doc.skills_html = '<p><br></p>'
    expect(isModuleEmpty(doc, 'skills')).toBe(true)
  })
  it('全空条目不撑起模块', () => {
    const doc = createEmptyDoc()
    doc.companies.push({
      cid: 'c1',
      company: '',
      position: '',
      department: '',
      start: '',
      end: '',
      current: false,
      description_html: '',
    })
    expect(isModuleEmpty(doc, 'companies')).toBe(true)
    expect(entryIsEmpty('companies', doc.companies[0])).toBe(true)
  })
})

describe('renderClassic', () => {
  function docWith(over: Partial<ResumeDocData>): ResumeDocData {
    return { ...createEmptyDoc(), ...over }
  }

  it('空文档也能出完整骨架，但没有任何 section', () => {
    const html = renderClassic(createEmptyDoc())
    expect(html).toContain('<!doctype html>')
    expect(html).toContain('@page')
    expect(html).toContain('size: A4')
    expect(html).not.toContain('<section')
  })

  it('个人信息渲染成顶部姓名带，转义用户文本', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '张三<b>'
    doc.profile.job_target = '后端'
    doc.profile.phone = '13800000000'
    const html = renderClassic(doc)
    expect(html).toContain('张三&lt;b&gt;')
    expect(html).toContain('后端')
    expect(html).toContain('13800000000')
  })

  it('联系方式带「电话/邮箱」字样并排在上面，基本信息继续裸值', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '张三'
    doc.profile.phone = '13800000000'
    doc.profile.email = 'a@b.com'
    doc.profile.gender = '男'
    doc.profile.birthday = '1995-06'
    doc.profile.city = '上海'
    const html = renderClassic(doc)
    const contacts = html.match(/<div class="rb-contacts">[\s\S]*?<\/div>/)?.[0] ?? ''
    const info = html.match(/<div class="rb-info">[\s\S]*?<\/div>/)?.[0] ?? ''
    // 13800000000 裸放着没人知道是电话，这一行必须有 label。
    expect(contacts).toContain('13800000000')
    expect(contacts).toContain('a@b.com')
    expect(contacts).toContain('<i class="rb-c-label">电话</i>')
    expect(contacts).toContain('<i class="rb-c-label">邮箱</i>')
    // 基本信息靠形态就能认出来，加 label 只会把右列撑宽、姓名压窄。
    expect(info).toContain('男</span>')
    expect(info).toContain('1995-06</span>')
    for (const label of ['性别', '出生年月', '城市']) {
      expect(info).not.toContain(label)
    }
    // 两行都用「·」断项，否则「男1995-06上海」连成一坨。
    expect(info).toContain('class="rb-sep"')
    expect(contacts).toContain('class="rb-sep"')
    // 联系方式是招聘方要抄走的那行，排在基本信息上面。
    expect(html.indexOf('<div class="rb-contacts">')).toBeLessThan(html.indexOf('<div class="rb-info">'))
  })

  it('个人介绍独占一行压在两列抬头下面；个人网站补协议成链接且挡掉属性注入', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '张三'
    doc.profile.intro = '8 年 Java 后端'
    doc.profile.website = 'me.dev'
    let html = renderClassic(doc)
    expect(html).toContain('class="rb-intro">8 年 Java 后端</div>')
    // 姓名/意向在左列，联系信息在右列，简介在 .rb-header-top 之外。
    expect(html).toMatch(/<div class="rb-header-id"><div class="rb-name">张三<\/div>.*?<\/div><div class="rb-header-meta">/)
    expect(html).toMatch(/<\/div><div class="rb-intro">/)
    expect(html).toContain('href="https://me.dev"')

    // 引号逃逸在属性里被转义。
    const evil = createEmptyDoc()
    evil.profile.website = 'x" onmouseover="alert(1)'
    html = renderClassic(evil)
    // 引号被转义，注入内容只能老实地待在 href 属性值里。
    expect(html).toContain('href="https://x&quot; onmouseover=&quot;alert(1)"')

    // 已带协议的不重复补。
    const https = createEmptyDoc()
    https.profile.website = 'https://me.dev/resume'
    expect(renderClassic(https)).toContain('href="https://me.dev/resume"')
  })

  it('富文本里的脚本不会进入打印文档', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '李四'
    doc.skills_html = '<ul><li>Python</li></ul><script>alert(1)</script>'
    const html = renderClassic(doc)
    expect(html).toContain('<li>Python</li>')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('alert')
  })

  it('模块按 module_order 输出，空模块隐藏', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '王五'
    doc.skills_html = '<p>技能</p>'
    doc.companies.push({
      cid: 'c1',
      company: 'A 公司',
      position: '工程师',
      department: '平台部',
      start: '2018-07',
      end: '',
      current: true,
      description_html: '<p>做事</p>',
    })
    doc.module_order = ['skills', 'companies', 'profile', 'projects', 'educations']
    const html = renderClassic(doc)
    // 个人优势为空：profile 位不产出 section（头部姓名带仍在）。
    const skillsAt = html.indexOf('专业技能')
    const companiesAt = html.indexOf('工作经历')
    expect(skillsAt).toBeGreaterThan(-1)
    expect(skillsAt).toBeLessThan(companiesAt)
    expect(html).toContain('A 公司')
    expect(html).toContain('2018-07 至今')
    expect(html).toContain('平台部')
    expect(html).not.toContain('项目经历')
    expect(html).not.toContain('教育经历')
  })

  it('全空条目不输出', () => {
    const doc = createEmptyDoc()
    doc.educations.push({
      eid: 'e1',
      school: '',
      major: '',
      college: '',
      degree: '',
      study_mode: '',
      start: '',
      end: '',
      current: false,
      description_html: '',
    })
    expect(renderClassic(doc)).not.toContain('教育经历')
  })

  it('教育副行带出学院与就读形式', () => {
    const doc = createEmptyDoc()
    doc.educations.push({
      eid: 'e1',
      school: '辽宁科技学院',
      major: '软件工程',
      college: '信息学院',
      degree: '本科',
      study_mode: '全日制',
      start: '2014-09',
      end: '2018-06',
      current: false,
      description_html: '',
    })
    const html = renderClassic(doc)
    expect(html).toContain('信息学院 · 全日制')

    const noCollege = { ...doc.educations[0], college: '' }
    doc.educations.splice(0, 1, noCollege)
    expect(renderClassic(doc)).toContain('全日制')
  })

  it('缺开始时间的至今条目不打问号', () => {
    const doc = createEmptyDoc()
    doc.companies.push({
      cid: 'c1',
      company: 'A 公司',
      position: '工程师',
      department: '',
      start: '',
      end: '',
      current: true,
      description_html: '',
    })
    const html = renderClassic(doc)
    expect(html).toContain('至今')
    expect(html).not.toContain('? 至今')
    expect(html).not.toContain('? -')
  })
})

describe('renderDoc 版式分派与配色注入', () => {
  function populatedDoc(): ResumeDocData {
    const doc = createEmptyDoc()
    doc.profile.name = '赵六'
    doc.profile.job_target = '前端工程师'
    doc.profile.phone = '13900000000'
    doc.skills_html = '<ul><li>Vue</li><li>TypeScript</li></ul>'
    doc.companies.push({
      cid: 'c1',
      company: 'B 公司',
      position: '高级前端',
      department: '产品技术',
      start: '2020-03',
      end: '',
      current: true,
      description_html: '<p>负责编辑器</p>',
    })
    doc.profile.summary_html = '<p>六年研发经验</p>'
    return doc
  }

  function htmlAside(html: string): string {
    return html.slice(
      html.indexOf('<aside class="rb-aside">'),
      html.indexOf('<main class="rb-main">'),
    )
  }

  it('未知 template/theme 分别回退 classic / teal', () => {
    const doc = populatedDoc()
    doc.template = 'future-tpl'
    doc.theme = 'future-theme'
    const html = renderDoc(doc)
    expect(html).toContain('--accent:#0a7e86')
    expect(html).not.toContain('rb-aside')
  })

  it('每个合法主题都注入对应的主色变量', () => {
    for (const theme of THEME_OPTIONS) {
      const doc = populatedDoc()
      doc.theme = theme.value
      const html = renderDoc(doc)
      expect(html).toContain(`--accent:${theme.accent}`)
      expect(html).toContain(`--accent-dark:${theme.dark}`)
    }
  })

  function sideDoc(): { html: string; doc: ResumeDocData } {
    const doc = populatedDoc()
    doc.template = 'sidebar'
    doc.theme = 'navy'
    return { html: renderDoc(doc), doc }
  }

  it('sidebar：身份、联系方式与技能进左名片栏', () => {
    const { html } = sideDoc()
    expect(html).toContain('rb-aside')
    expect(html).toContain('rb-main')
    expect(html).toContain('--accent-dark:#163a5c')
    expect(html).toContain('赵六')
    expect(html).toContain('13900000000')
    // 技能在名片栏渲染，主栏不重复。
    expect(html).toContain('Vue')
    const asideAt = html.indexOf('<aside class="rb-aside">')
    const mainAt = html.indexOf('<main class="rb-main">')
    expect(asideAt).toBeGreaterThan(-1)
    expect(mainAt).toBeGreaterThan(asideAt)
    expect(html.slice(asideAt, mainAt)).toContain('Vue')
  })

  it('sidebar：个人介绍、基本信息块与网站链接都在名片栏', () => {
    const doc = populatedDoc()
    doc.template = 'sidebar'
    doc.profile.intro = '一句话介绍'
    doc.profile.website = 'me.dev'
    doc.profile.gender = '男'
    doc.profile.city = '上海'
    const aside = htmlAside(renderDoc(doc))
    expect(aside).toContain('rb-aside-intro')
    expect(aside).toContain('一句话介绍')
    expect(aside).toContain('基本信息')
    expect(aside).toContain('href="https://me.dev"')
  })

  it('sidebar：联系方式按标签/值两列排列', () => {
    const doc = populatedDoc()
    doc.template = 'sidebar'
    const aside = htmlAside(renderDoc(doc))
    expect(aside).toContain('rb-c-label')
    expect(aside).toContain('电话')
    expect(aside).toContain('rb-c-value')
    expect(aside).toContain('13900000000')
  })

  it('sidebar：主栏保留除技能外模块并按 module_order 输出', () => {
    const doc = populatedDoc()
    doc.template = 'sidebar'
    doc.module_order = ['profile', 'companies', 'educations', 'projects', 'skills']
    const html = renderDoc(doc)
    const main = html.slice(html.indexOf('<main class="rb-main">'))
    // 个人优势与工作经历在主栏，顺序遵循 module_order。
    expect(main).toContain('个人优势')
    expect(main).toContain('B 公司')
    expect(main.indexOf('个人优势')).toBeLessThan(main.indexOf('工作经历'))
    // 主栏不再出现第二个「专业技能」标题。
    expect(main).not.toContain('>专业技能<')
  })

  it('minimal：单栏结构 + 姓名色条，空文档无 section', () => {
    const doc = populatedDoc()
    doc.template = 'minimal'
    const html = renderDoc(doc)
    expect(html).not.toContain('rb-aside')
    expect(html).toContain('rb-name-bar')
    expect(html).toContain('B 公司')

    const empty = createEmptyDoc()
    empty.template = 'minimal'
    expect(renderDoc(empty)).not.toContain('<section')
  })

  it('classic：有姓名时才渲染色条占位，主题色生效', () => {
    const doc = populatedDoc()
    doc.theme = 'crimson'
    const html = renderDoc(doc)
    expect(html).toContain('rb-name-bar')
    expect(html).toContain('--accent:#9b3a4b')
    expect(renderClassic(createEmptyDoc())).not.toContain('rb-name-bar')
  })
})

describe('打印样式契约', () => {
  const css = renderClassic(createEmptyDoc())

  it('强制打印背景色，否则双栏名片侧栏变白底白字', () => {
    expect(css).toContain('print-color-adjust: exact')
    expect(css).toContain('-webkit-print-color-adjust: exact')
  })

  it('断页单位是抬头不是整条，否则长条目跳页留半页空白', () => {
    expect(css).toContain('.rb-entry-head,.rb-dept {')
    expect(css).toContain('break-after: avoid')
    expect(css).not.toContain('.rb-entry { break-inside: avoid')
    expect(css).not.toContain('.rb-section, .rb-entry')
    expect(css).toContain('.rb-section h2 { break-after: avoid')
  })

  it('长英文串有溢出策略，标题列可压缩', () => {
    expect(css).toContain('.rb-info,.rb-contacts { overflow-wrap: anywhere')
    expect(css).toContain('.rb-entry-title { font-size: 13.5px; font-weight: 600; min-width: 0')
    expect(css).toContain('orphans: 2; widows: 2')
  })

  it('消毒器放行的对齐 class 与缩进在版面里有对应样式', () => {
    expect(css).toContain('.rb-ta-center { text-align: center; }')
    expect(css).toContain('.rb-ta-right { text-align: right; }')
    expect(css).toContain('.rb-ta-justify { text-align: justify; }')
    expect(css).toContain('.rb-desc blockquote { margin: 6px 0 7px; padding-left: 20px; }')
  })

  it('竖向间距是台阶式的：段 < 条目 < 节，差不到 1mm 就等于没间距', () => {
    // 行框 12.5px * 1.7 = 21.25px，段间距只有比它多一档才看得出换段。
    expect(css).toContain('.rb-desc { margin-top: 7px; font-size: 12.5px; line-height: 1.7')
    expect(css).toContain('.rb-desc p { margin: 0 0 7px; }')
    expect(css).toContain('.rb-desc li { margin-bottom: 4px; }')
    expect(css).toContain('.rb-entry { margin-bottom: 14px; }')
    expect(css).toContain('.rb-section { margin-top: 24px; }')
    const side = renderDoc({ ...createEmptyDoc(), template: 'sidebar' })
    expect(side).toContain('.rb-main .rb-section { margin-top: 22px; }')
    expect(side).toContain('.rb-entry { margin-bottom: 14px; }')
    const minimal = renderDoc({ ...createEmptyDoc(), template: 'minimal' })
    expect(minimal).toContain('.rb-section { margin-top: 26px; }')
    expect(minimal).toContain('.rb-entry { margin-bottom: 15px; }')
  })

  it('头部两列不换行：意向再长也只压列宽，联系信息不许掉到姓名下面独占一行', () => {
    expect(css).toContain('.rb-header-top { display: flex; align-items: flex-end; justify-content: space-between; gap: 8px 24px; }')
    expect(css).toContain('.rb-header-id,.rb-header-meta { min-width: 0; }')
    expect(css).not.toContain('.rb-header-top { display: flex; flex-wrap: wrap')
    // 头部 label 是行内小灰字，不是斜体、也不是小标题。
    expect(css).toContain('.rb-header-meta .rb-c-label { margin-right: 4px; font-style: normal; color: #8a8f99; }')
    // 姓名要比正文大一档才压得住抬头，之前 28px 被两行带标签的小灰字淹掉。
    expect(css).toContain('.rb-name { font-size: 34px')
    const minimal = renderDoc({ ...createEmptyDoc(), template: 'minimal' })
    expect(minimal).toContain('.rb-name { font-size: 32px')
  })

  it('屏显 padding 与 @page 边距同数，预览分页才不漂', () => {
    expect(css).toContain('@page { size: A4; margin: 15mm 14mm; }')
    expect(css).toContain('.sheet { padding: 15mm 14mm; }')
    const minimal = renderDoc({ ...createEmptyDoc(), template: 'minimal' })
    expect(minimal).toContain('.sheet { padding: 18mm 16mm; }')
    expect(minimal).toContain('@page { size: A4; margin: 18mm 16mm; }')
  })

  it('节标题用上 --accent-soft，配色注入不再是死变量', () => {
    expect(css).toContain('background: var(--accent-soft)')
    expect(css).toContain('border-left: 3px solid var(--accent)')
    const side = renderDoc({ ...createEmptyDoc(), template: 'sidebar' })
    expect(side).toContain('.rb-main .rb-section h2 { margin: 0 0 10px; padding: 3px 8px; background: var(--accent-soft)')
  })
})

// 每页能装的流内容高度是 Edge 打印引擎逐毫米实测出来的（267/261/297mm 恰好还是一页，
// 再多 1mm 就翻页），不是页高 297mm；顶部还要加屏显 .sheet 的留白。
describe('pageBreakTops 分页线几何', () => {
  const MM = 96 / 25.4

  it('一页放得下时一条线都不画', () => {
    expect(pageBreakTops('classic', 1123)).toEqual([])
    expect(pageBreakTops('minimal', 1123)).toEqual([])
    expect(pageBreakTops('sidebar', 1123)).toEqual([])
  })

  it('刚溢出 1mm 就画线，位置是顶部留白 + 一页内容高', () => {
    const tops = pageBreakTops('classic', 298 * MM)
    expect(tops).toHaveLength(1)
    expect(tops[0]).toBeCloseTo(282 * MM, 1)
    // 老写法按整页高 1122.5px 画，会低 56px（正好差一个上边距）。
    expect(Math.abs(tops[0] - 297 * MM)).toBeGreaterThan(40)
  })

  it('各版式按自己的边距推页：线间距等于每页内容高', () => {
    const classic = pageBreakTops('classic', 820 * MM)
    expect(classic).toHaveLength(2)
    expect(classic[0]).toBeCloseTo(282 * MM, 1)
    expect(classic[1] - classic[0]).toBeCloseTo(267 * MM, 1)
    const minimal = pageBreakTops('minimal', 820 * MM)
    expect(minimal).toHaveLength(3)
    expect(minimal[0]).toBeCloseTo(279 * MM, 1)
    expect(minimal[1] - minimal[0]).toBeCloseTo(261 * MM, 1)
    const side = pageBreakTops('sidebar', 820 * MM)
    expect(side[0]).toBeCloseTo(315 * MM, 1)
    expect(side[1] - side[0]).toBeCloseTo(297 * MM, 1)
  })

  it('几何表与 CSS 边距同源，改了 padding 必须同步 stride', () => {
    expect(renderClassic(createEmptyDoc())).toContain('@page { size: A4; margin: 15mm 14mm; }')
    expect(renderDoc({ ...createEmptyDoc(), template: 'minimal' })).toContain(
      '@page { size: A4; margin: 18mm 16mm; }',
    )
    const side = renderDoc({ ...createEmptyDoc(), template: 'sidebar' })
    expect(side).toContain('@page { size: A4; margin: 0; }')
    expect(side).toContain('.rb-main { flex: 1; padding: 18mm 11mm 14mm 10mm; }')
  })

  it('未知版式回退经典', () => {
    expect(pageBreakTops('weird', 820)).toEqual(pageBreakTops('classic', 820))
  })
})

describe('plainTextOfHtml', () => {
  it('拍平标签并收敛空白', () => {
    expect(plainTextOfHtml('<ul><li>a</li><li>b</li></ul>')).toBe('ab')
    expect(plainTextOfHtml('<p>a&nbsp;b</p>')).toBe('a b')
    expect(plainTextOfHtml('<p>x</p><script>y</script>')).toBe('x')
  })
})
