import { describe, expect, it } from 'vitest'
import { countWords } from '@/utils/writingWords'

describe('countWords', () => {
  it('空输入计 0', () => {
    expect(countWords()).toBe(0)
    expect(countWords(null)).toBe(0)
    expect(countWords('')).toBe(0)
  })

  it('CJK 按字计，markdown 标记不计', () => {
    expect(countWords('## 标题 **加粗**')).toBe(4)
  })

  it('连续拉丁字母数字按一个词计，下划线分词', () => {
    expect(countWords('hello world2 v2')).toBe(3)
    // 与后端同口径：下划线不在拉丁字符类里，word_count 拆成两个 token。
    expect(countWords('word_count')).toBe(2)
  })

  it('中英混排', () => {
    expect(countWords('你好 world')).toBe(3)
    expect(countWords('少年策马下山，city 不相信眼泪。')).toBe(12)
  })
})
