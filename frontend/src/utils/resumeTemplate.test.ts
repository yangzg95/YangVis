import { describe, expect, it } from 'vitest'
import {
  entryIsEmpty,
  isModuleEmpty,
  monthRangeLabel,
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
    expect(monthRangeLabel('', '2021-01', false)).toBe('? - 2021-01')
    expect(monthRangeLabel('', '', false)).toBe('')
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

describe('plainTextOfHtml', () => {
  it('拍平标签并收敛空白', () => {
    expect(plainTextOfHtml('<ul><li>a</li><li>b</li></ul>')).toBe('ab')
    expect(plainTextOfHtml('<p>a&nbsp;b</p>')).toBe('a b')
    expect(plainTextOfHtml('<p>x</p><script>y</script>')).toBe('x')
  })
})
