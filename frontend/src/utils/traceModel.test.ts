import { describe, expect, it } from 'vitest'
import { chatTraceNodes, workflowTraceNodes, type TraceNode } from './traceModel'
import type { TimelineEntry, WorkflowTraceEvent } from '@/api'

describe('chatTraceNodes', () => {
  it('过滤正文与确认提议，只留过程节点', () => {
    const entries: TimelineEntry[] = [
      { type: 'text', detail: '正文' },
      { type: 'reason', detail: '想一想' },
      { type: 'step', tool: 'current_datetime', running: true },
      { type: 'confirm', action_id: 9 },
    ]
    const nodes = chatTraceNodes(entries)
    expect(nodes.map((n) => n.kind)).toEqual(['reason', 'tool'])
  })

  it('llm 条目映射为带耗时的模型往返节点', () => {
    const nodes = chatTraceNodes([{ type: 'llm', elapsed_ms: 1230 }])
    expect(nodes).toHaveLength(1)
    expect(nodes[0]).toMatchObject({
      kind: 'llm',
      status: 'done',
      title: '模型思考',
      elapsedMs: 1230,
    })
  })

  it('工具节点带上起始时刻、耗时、入参与返回块', () => {
    const nodes = chatTraceNodes([
      {
        type: 'step',
        tool: 'search_knowledge_base',
        input: { query: 'nginx 502' },
        run_id: 't1',
        output: '上游超时',
        is_error: false,
        started_at: '2026-10-26T10:00:00',
        elapsed_ms: 540,
      },
    ])
    expect(nodes[0]).toMatchObject({
      kind: 'tool',
      status: 'done',
      title: '检索知识库：nginx 502',
      tool: 'search_knowledge_base',
      elapsedMs: 540,
      startedAt: '2026-10-26T10:00:00',
    })
    expect(nodes[0].blocks.map((b) => b.label)).toEqual(['入参', '返回结果'])
    expect(nodes[0].blocks[1].copy).toBe(true)
  })

  it('执行中（running 且无返回）保留转圈态；失败标红', () => {
    const running = chatTraceNodes([{ type: 'step', tool: 'x', running: true }])
    expect(running[0].status).toBe('running')

    // 历史条目没有 running 字段，按已完成渲染。
    const failed = chatTraceNodes([
      { type: 'step', tool: 'x', output: 'boom', is_error: true },
    ])
    expect(failed[0].status).toBe('error')
    expect(failed[0].blocks[0]).toMatchObject({ label: '错误返回', tone: 'error' })
  })
})

describe('workflowTraceNodes', () => {
  it('llm 事件直接成节点，tool_start/end 按名配对', () => {
    const raw: WorkflowTraceEvent[] = [
      { kind: 'llm', elapsed_ms: 200 },
      { kind: 'tool_start', name: 'search', args: { q: 'x' } },
      {
        kind: 'tool_end',
        name: 'search',
        args: { q: 'x' },
        output: '查到了',
        is_error: false,
        elapsed_ms: 300,
      },
    ]
    const nodes = workflowTraceNodes(raw)
    expect(nodes.map((n) => [n.kind, n.status])).toEqual([
      ['llm', 'done'],
      ['tool', 'done'],
    ])
    expect(nodes[1]).toMatchObject({ title: 'search', elapsedMs: 300 })
    expect(nodes[1].blocks.map((b) => b.label)).toEqual(['入参', '返回结果'])
  })

  it('同名工具的 start 压栈，end 与最后一个未闭合的同名 start 配对', () => {
    const raw: WorkflowTraceEvent[] = [
      { kind: 'tool_start', name: 't', args: { i: 1 } },
      { kind: 'tool_start', name: 't', args: { i: 2 } },
      { kind: 'tool_end', name: 't', output: 'b', elapsed_ms: 20 },
      { kind: 'tool_end', name: 't', output: 'a', elapsed_ms: 10 },
    ]
    const nodes = workflowTraceNodes(raw)
    expect(nodes).toHaveLength(2)
    // 后开的先闭合（LIFO）：第一个节点是 i=2 那次。
    expect(JSON.parse(nodes[0].blocks[0].text)).toEqual({ i: 2 })
    expect(nodes[0].blocks[1].text).toBe('b')
    expect(nodes[0].elapsedMs).toBe(20)
    expect(JSON.parse(nodes[1].blocks[0].text)).toEqual({ i: 1 })
  })

  it('流结束仍未闭合的 start 保留为 running（步骤被中断时的形态）', () => {
    const nodes = workflowTraceNodes([
      { kind: 'tool_start', name: 'long_job', args: {} },
    ])
    expect(nodes).toHaveLength(1)
    expect(nodes[0].status).toBe('running')
    expect(nodes[0].elapsedMs).toBeNull()
  })

  it('找不到归属的 end 用自身补一个完成节点', () => {
    const nodes = workflowTraceNodes([
      { kind: 'tool_end', name: 'orphan', output: 'late', elapsed_ms: 5, is_error: true },
    ])
    expect(nodes).toHaveLength(1)
    expect(nodes[0]).toMatchObject({ kind: 'tool', status: 'error', title: 'orphan' })
  })

  it('null / undefined 与空数组都给空列表', () => {
    expect(workflowTraceNodes(null)).toEqual<TraceNode[]>([])
    expect(workflowTraceNodes(undefined)).toEqual<TraceNode[]>([])
    expect(workflowTraceNodes([])).toEqual<TraceNode[]>([])
  })
})
