import { describe, expect, it } from 'vitest'
import { ARTICLE_ACTIONS, NOVEL_ACTIONS } from '@/api'
import { defaultAction, partActionsFor } from '@/utils/writingActions'

describe('partActionsFor', () => {
  it('小说给全量七个动作，文章只给五个编辑动作', () => {
    expect(partActionsFor('novel', true).map((c) => c.action)).toEqual(NOVEL_ACTIONS)
    expect(partActionsFor('article', true).map((c) => c.action)).toEqual(ARTICLE_ACTIONS)
  })

  it('空块上编辑动作置灰并给原因，写手动作保持可用', () => {
    const choices = partActionsFor('novel', false)
    const draft = choices.find((c) => c.action === 'draft')
    const polish = choices.find((c) => c.action === 'polish')
    expect(draft?.disabled).toBe(false)
    expect(draft?.reason).toBeUndefined()
    expect(polish?.disabled).toBe(true)
    expect(polish?.reason).toContain('起草')
  })

  it('文章空块上没有任何可用动作，引导整篇起草或手工写', () => {
    const choices = partActionsFor('article', false)
    expect(choices.every((c) => c.disabled)).toBe(true)
    expect(choices.every((c) => c.reason)).toBe(true)
  })

  it('有正文后全部动作恢复可用', () => {
    expect(partActionsFor('article', true).every((c) => !c.disabled)).toBe(true)
    expect(partActionsFor('novel', true).every((c) => !c.disabled)).toBe(true)
  })
})

describe('defaultAction', () => {
  it('小说默认起草，文章回落重写', () => {
    expect(defaultAction('novel')).toBe('draft')
    expect(defaultAction('article')).toBe('rewrite')
  })
})
