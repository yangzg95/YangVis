import { describe, expect, it } from 'vitest'
import type { Router } from 'vue-router'
import type { WorkflowRun } from '@/api'
import { subjectLink } from './runLinks'

function routerStub(): Router {
  return {
    resolve: (to: unknown) => ({ href: `/stub/${JSON.stringify(to)}` }),
  } as unknown as Router
}

function run(partial: Partial<WorkflowRun>): WorkflowRun {
  return {
    id: 1,
    workflow_id: 1,
    workflow_slug: 'novel-outline',
    status: 'succeeded',
    ...partial,
  } as WorkflowRun
}

describe('subjectLink', () => {
  it('writing-work 回链到创作工作台，ref 作为路由 id', () => {
    const link = subjectLink(routerStub(), run({ subject_kind: 'writing-work', subject_ref: '42' }))
    expect(link?.text).toBe('开工作台')
    expect(link?.href).toContain('WritingWorkbench')
    expect(link?.href).toContain('"id":42')
  })

  it('没有 subject 或 ref 不是整数时不给链接', () => {
    expect(subjectLink(routerStub(), run({ subject_kind: 'writing-work' }))).toBeNull()
    expect(
      subjectLink(routerStub(), run({ subject_kind: 'interview-question', subject_ref: 'q-3' })),
    ).toBeNull()
  })

  it('未登记的 subject 类型返回 null，而不是猜一个链接', () => {
    expect(
      subjectLink(routerStub(), run({ subject_kind: 'video-project', subject_ref: '7' })),
    ).toBeNull()
  })

  it('既有类型的链接保持不变', () => {
    const link = subjectLink(routerStub(), run({ subject_kind: 'slide-deck', subject_ref: '9' }))
    expect(link?.text).toBe('开工作台')
    expect(link?.href).toContain('SlideWorkbench')
  })
})
