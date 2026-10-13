/**
 * 运行现场的共享形状：一步收到的上下文变量。
 * 抽屉与运行现场整页都渲染同一份现场，还原逻辑只写这一份。
 */
import type { WorkflowRunDetail, WorkflowRunStep } from '@/api'

/** 一步现场里可展示/可覆盖的一个上下文变量。 */
export interface StepField {
  name: string
  value: string
  /** 值是 JSON 步骤的解析产物：弹框里按 JSON 编辑，提交时服务端按 JSON 解析。 */
  isJson: boolean
}

/**
 * 还原某一步实际收到的上下文变量：运行输入 ＋ **严格早于**本步所在组的步骤产物。
 *
 * 纯函数，形状与后端 ``_rebuild_context`` / ``_context_fields`` 对齐：同组并行
 * 步骤的产物不在里面（第一次跑时它们彼此也看不见）；同名变量由产物覆盖输入。
 */
export function stepInputFields(detail: WorkflowRunDetail, step: WorkflowRunStep): StepField[] {
  // 组在 steps 里首次出现的次序即执行次序（后端按 sort_order 返回）。
  const groupOrder = new Map<string, number>()
  for (const row of detail.steps) {
    if (!groupOrder.has(row.group_key)) groupOrder.set(row.group_key, groupOrder.size)
  }
  const targetGroup = groupOrder.get(step.group_key) ?? 0

  const fields = new Map<string, StepField>()
  for (const [name, value] of Object.entries(detail.inputs)) {
    fields.set(name, { name, value, isJson: false })
  }
  for (const row of detail.steps) {
    if ((groupOrder.get(row.group_key) ?? 0) >= targetGroup || !row.output_var) continue
    const raw: unknown = row.parsed ?? row.output ?? ''
    fields.set(row.output_var, {
      name: row.output_var,
      value: typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2),
      isJson: row.parsed != null,
    })
  }
  return [...fields.values()]
}
