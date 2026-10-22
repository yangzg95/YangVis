import { describe, expect, it } from 'vitest'
import {
  resumeDocHasContent,
  resumeDocToMarkdown,
  richHtmlToPlainText,
} from './resumeDocMarkdown'
import { createEmptyDoc, createEntry, type ResumeDocData } from './resumeDoc'

describe('richHtmlToPlainText', () => {
  it('列表项转成 - 开头的行，段落与 br 换行保留', () => {
    const out = richHtmlToPlainText('<ul><li>甲</li><li>乙</li></ul><p>一行<br>二行</p>')
    expect(out).toBe('- 甲\n- 乙\n\n一行\n二行')
  })

  it('行内标签去掉，实体解码', () => {
    expect(richHtmlToPlainText('<p>a &amp; b&nbsp;&gt; c</p>')).toBe('a & b > c')
  })

  it('空片段得到空串', () => {
    expect(richHtmlToPlainText('')).toBe('')
  })
})

describe('resumeDocToMarkdown', () => {
  it('空文档得到空串', () => {
    expect(resumeDocToMarkdown(createEmptyDoc())).toBe('')
    expect(resumeDocHasContent(createEmptyDoc())).toBe(false)
  })

  it('个人信息与技能按结构输出', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '张三'
    doc.profile.job_target = '高级前端工程师'
    doc.profile.city = '上海'
    doc.profile.phone = '13800000000'
    doc.profile.intro = '一句话招牌'
    doc.profile.summary_html = '<ul><li>五年经验</li></ul>'
    doc.skills_html = '<p>Vue / TypeScript</p>'

    const md = resumeDocToMarkdown(doc)
    expect(md).toContain('# 张三 / 高级前端工程师 / 上海')
    expect(md).toContain('- 电话：13800000000')
    expect(md).toContain('一句话招牌')
    expect(md).toContain('## 个人优势\n\n- 五年经验')
    expect(md).toContain('## 专业技能\n\nVue / TypeScript')
    expect(resumeDocHasContent(doc)).toBe(true)
  })

  it('经历条目带时间段与描述，空条目被跳过', () => {
    const doc = createEmptyDoc()
    const company = createEntry('companies') as ResumeDocData['companies'][number]
    company.company = '某公司'
    company.position = '前端负责人'
    company.department = '平台研发部'
    company.start = '2020-03'
    company.current = true
    company.description_html = '<ul><li>性能提升 30%</li></ul>'
    doc.companies.push(company, createEntry('companies') as ResumeDocData['companies'][number])

    const md = resumeDocToMarkdown(doc)
    expect(md).toContain('### 某公司 / 前端负责人')
    expect(md).toContain('- 部门：平台研发部')
    expect(md).toContain('- 时间：2020-03 至今')
    expect(md).toContain('- 性能提升 30%')
    // 加了没填的空条目不产出空标题。
    expect(md.match(/###/g)).toHaveLength(1)
  })

  it('教育经历输出学历等白名单字段', () => {
    const doc = createEmptyDoc()
    const edu = createEntry('educations') as ResumeDocData['educations'][number]
    edu.school = '某大学'
    edu.major = '计算机科学'
    edu.degree = '本科'
    edu.study_mode = '全日制'
    edu.start = '2008-09'
    edu.end = '2012-06'
    doc.educations.push(edu)

    const md = resumeDocToMarkdown(doc)
    expect(md).toContain('### 某大学 / 计算机科学')
    expect(md).toContain('- 学历：本科')
    expect(md).toContain('- 就读形式：全日制')
    expect(md).toContain('- 时间：2008-09 - 2012-06')
  })

  it('模块顺序按 module_order 输出', () => {
    const doc = createEmptyDoc()
    doc.profile.name = '张三'
    doc.skills_html = '<p>技能</p>'
    doc.module_order = ['skills', 'profile', 'companies', 'projects', 'educations']
    const md = resumeDocToMarkdown(doc)
    expect(md.indexOf('## 专业技能')).toBeLessThan(md.indexOf('# 张三'))
  })
})
