import { describe, expect, it } from 'vitest'
import { withDocumentBase } from './slidePrint'

describe('withDocumentBase', () => {
  it('把相对路径指回本站，配图才和预览里取到同一张', () => {
    const html = '<html><head><title>拆解</title></head><body><img src="/uploads/slides/7/aa.png"></body></html>'
    const got = withDocumentBase(html, 'http://localhost:5173')
    expect(got).toContain('<head><base href="http://localhost:5173/"><title>拆解</title>')
  })

  it('认不出 <head> 时原样返回，不凭空插节点', () => {
    const html = '<!doctype html><p>没有头的文档</p>'
    expect(withDocumentBase(html, 'http://localhost:5173')).toBe(html)
  })
})
