/**
 * 运行记录认领的业务对象能不能被深链。
 *
 * 报告页与工作台都在 ``/office`` 下按整数 id 寻址，运行记录的 ``subject_ref``
 * 就是那个 id。面试题不能：它的 ref 是题目在自己记录里的 qid，光凭这一个数落不到
 * 「哪一条面试记录」——与其给一个会跳错的链接，不如只留标题里快照下来的名字。
 */
import type { Router } from 'vue-router'
import type { WorkflowRun } from '@/api'

export function subjectLink(
  router: Router,
  run: WorkflowRun,
): { text: string; href: string } | null {
  const id = Number(run.subject_ref)
  if (!run.subject_kind || !Number.isInteger(id)) return null
  // 与简历/幻灯片列表「查看报告」同一模式：整页展示是 bare 路由，新开标签页。
  const open = (text: string, to: Parameters<Router['resolve']>[0]) => ({
    text,
    href: router.resolve(to).href,
  })
  switch (run.subject_kind) {
    case 'resume':
      return open('看简历', { name: 'ReportView', params: { kind: 'resume', id } })
    case 'resume-comparison':
      return open('看对比', { name: 'ReportView', params: { kind: 'comparison', id } })
    case 'resume-toolkit':
      return open('看助手', { name: 'ReportView', params: { kind: 'toolkit', id } })
    case 'slide-deck':
      return open('开工作台', { name: 'SlideWorkbench', params: { id } })
    default:
      return null
  }
}
