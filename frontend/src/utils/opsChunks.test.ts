import { describe, expect, it } from 'vitest'
import { foldChunks, type FoldChunk } from './opsChunks'

const step = (over: Partial<FoldChunk> = {}): FoldChunk => ({
  type: 'step',
  detail: 'df -h',
  actionName: '执行命令',
  preview: 'df -h',
  ...over,
})

describe('foldChunks 工具组', () => {
  it('连续两条默认收成一行摘要，次数与失败数一起报', () => {
    const out = foldChunks(
      [step(), step({ preview: 'rm -rf /tmp/x', isError: true })],
      'a1',
      new Set<string>(),
      new Map<number, unknown>(),
    )
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ summary: true, runCount: 2, runErrors: 1 })
  })

  it('全成功的组 runErrors 为 0，摘要行不提失败', () => {
    const out = foldChunks([step(), step()], 'a2', new Set<string>(), new Map<number, unknown>())
    expect(out[0].runErrors).toBe(0)
  })

  it('单条步骤不折叠，动作名与摘要原样透传', () => {
    const out = foldChunks([step()], 'a3', new Set<string>(), new Map<number, unknown>())
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ summary: false, actionName: '执行命令', preview: 'df -h' })
  })

  it('展开后摘要行保留「收起」入口，逐条步骤随行列出', () => {
    const out = foldChunks(
      [step(), step({ runId: 'r2' })],
      'a4',
      new Set<string>(['a4:step:0']),
      new Map<number, unknown>(),
    )
    expect(out.map((c) => c.summary)).toEqual([true, false, false])
    expect(out[0].expanded).toBe(true)
  })

  it('正文把步骤切成两组，单条那组不折叠', () => {
    const out = foldChunks(
      [step(), step(), { type: 'text', detail: '先这些' }, step({ isError: true })],
      'a5',
      new Set<string>(),
      new Map<number, unknown>(),
    )
    const summaries = out.filter((c) => c.summary)
    expect(summaries.map((c) => [c.runCount, c.runErrors, c.runKey])).toEqual([
      [2, 0, 'a5:step:0'],
    ])
    // 正文原位保留，末尾单条步骤直接平铺可见。
    expect(out.map((c) => [c.type, c.summary])).toEqual([
      ['step', true],
      ['text', false],
      ['step', false],
    ])
  })

  it('未收到 step_result 时 running 随行带出，行首继续转圈', () => {
    const out = foldChunks(
      [step({ running: true, runId: 'r1' }), step({ runId: 'r2' })],
      'a6',
      new Set<string>(['a6:step:0']),
      new Map<number, unknown>(),
    )
    expect(out[1].running).toBe(true)
    expect(out[1].runId).toBe('r1')
    expect(out[2].runId).toBe('r2')
  })
})
