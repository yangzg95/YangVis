/** 工作流的并行组布局与模板引用解析，口径与后端 services/workflows.py 一致：
 *  **相邻**且 ``group_key`` 相同的步骤算一个并行组，组内并发、组间顺序执行。
 *
 * 画布的列、引用线、可引用变量芯片都从这里派生，不再各写一份分组逻辑。
 */
import type { WorkflowStep } from '@/api'

/** 编辑态的一行步骤：与接口返回的 ``WorkflowStep`` 同字段，但 ``sort_order`` 由数组顺序决定。 */
export interface StepForm {
  key: string
  group_key: string
  agent_slug: string
  prompt_tpl: string
  output_kind: string
  output_keys: string[]
  output_var: string
  model_config_id: number | null
  /** 节点级挂载的 MCP 服务，仅本步生效；与工作流级挂载取并集。 */
  mcp_server_ids: number[]
  optional: boolean
}

export interface InputFieldForm {
  name: string
  label: string
  required: boolean
}

export interface StepGroup {
  name: string
  /** 属于这一组的步骤下标，按数组顺序。 */
  indexes: number[]
}

export interface ChipGroup {
  label: string
  chips: string[]
}

/** 一条引用线：上游产物 ``variable`` 从 ``from`` 那步流向 ``to`` 那步。 */
export interface StepLink {
  from: number
  to: number
  variable: string
}

export const emptyStep = (groupKey = ''): StepForm => ({
  key: '',
  group_key: groupKey,
  agent_slug: '',
  prompt_tpl: '',
  output_kind: 'markdown',
  output_keys: [],
  output_var: '',
  model_config_id: null,
  mcp_server_ids: [],
  optional: false,
})

/** 后端步骤 → 表单行：编辑回填与「从内置起步」共用同一份映射。 */
export function toStepForm(step: WorkflowStep): StepForm {
  return {
    key: step.key,
    group_key: step.group_key,
    agent_slug: step.agent_slug,
    prompt_tpl: step.prompt_tpl,
    output_kind: step.output_kind,
    output_keys: [...(step.output_keys ?? [])],
    output_var: step.output_var ?? '',
    model_config_id: step.model_config_id ?? null,
    mcp_server_ids: [...(step.mcp_server_ids ?? [])],
    optional: step.optional,
  }
}

/** 切成并行组：同名但不相邻就是两组（后端保存时会拦这种命名，画布按两组画）。 */
export function groupSteps(steps: Array<{ group_key: string }>): StepGroup[] {
  const groups: StepGroup[] = []
  steps.forEach((step, index) => {
    const name = step.group_key.trim()
    const last = groups[groups.length - 1]
    if (last && last.name === name) last.indexes.push(index)
    else groups.push({ name, indexes: [index] })
  })
  return groups
}

/**
 * 并行组的展示名：组内各步的步骤标识（key）按顺序拼出来——
 * 单步组就是那一步的标识，多步并行组形如 ``大纲、配图``；都还没起名则 ``未命名``。
 *
 * ``group_key``（默认 g1/g2）只是「相邻同名即同组」的内部凭据，没有信息量，
 * 界面一律走这里，不直接展示它。
 */
export function groupLabel(
  steps: Array<{ key: string }>,
  group: { indexes: number[] },
): string {
  const names = group.indexes
    .map((i) => steps[i]?.key.trim())
    .filter((name): name is string => !!name)
  return names.length ? names.join('、') : '未命名'
}

/** 每一步属于第几个并行组，与 ``groupSteps`` 同源。 */
export function groupSequence(steps: Array<{ group_key: string }>): number[] {
  const sequences: number[] = []
  let current = -1
  let previous: string | null = null
  for (const step of steps) {
    const name = step.group_key.trim()
    if (name !== previous) {
      current += 1
      previous = name
    }
    sequences.push(current)
  }
  return sequences
}

/** 与后端 ``_PLACEHOLDER`` 同一形状：``{{`` 到下一个 ``}}`` 之间不含花括号。 */
const PLACEHOLDER = /\{\{([^{}]*)\}\}/g

/** 模板里引用到的根变量名（``{{draft.title}}`` 取 ``draft``），按出现顺序去重。 */
export function referencedVars(tpl: string): string[] {
  const roots: string[] = []
  for (const match of tpl.matchAll(PLACEHOLDER)) {
    const name = match[1].split('.')[0].trim()
    if (name && !roots.includes(name)) roots.push(name)
  }
  return roots
}

/** 组名不能撞：撞了就会被算进同一组，所以新列一律取一个没占用过的名字。 */
export function uniqueGroupName(steps: Array<{ group_key: string }>): string {
  const used = new Set(steps.map((step) => step.group_key.trim()))
  for (let n = steps.length + 1; ; n += 1) {
    if (!used.has(`g${n}`)) return `g${n}`
  }
}

/**
 * 这一步的模板里能引用谁，按来源分组：声明过的输入，加上**前面**并行组的产物。
 *
 * JSON 步骤额外列出按字段取值（``{{产物.字段}}``，字段来自它的期望字段）——后端
 * 只允许 JSON 步这样取，芯片列出的集合必须和校验一致，否则点出来的是一个存不下的
 * 模板。分组名写明来源步骤，用户不必再记「这个名字是哪步来的」。
 */
export function chipGroupsFor(
  inputs: InputFieldForm[],
  steps: StepForm[],
  index: number,
): ChipGroup[] {
  const sequences = groupSequence(steps)
  const own = sequences[index] ?? 0
  const groups: ChipGroup[] = []
  const declared = inputs
    .map((field) => field.name.trim())
    .filter((name) => name)
    .map((name) => `{{${name}}}`)
  if (declared.length) groups.push({ label: '输入变量', chips: [...new Set(declared)] })
  steps.forEach((step, stepIndex) => {
    if ((sequences[stepIndex] ?? 0) >= own) return
    const variable = step.output_var.trim()
    if (!variable) return
    const chips = [`{{${variable}}}`]
    if (step.output_kind === 'json') {
      for (const key of step.output_keys) {
        const field = key.trim()
        if (field) chips.push(`{{${variable}.${field}}}`)
      }
    }
    const label = `第 ${stepIndex + 1} 步 · ${step.key.trim() || variable}`
    groups.push({ label, chips: [...new Set(chips)] })
  })
  return groups
}

/** 可引用变量全集（芯片集合的拍平版），「{{」自动补全用的是同一份。 */
export function chipsFor(inputs: InputFieldForm[], steps: StepForm[], index: number): string[] {
  return chipGroupsFor(inputs, steps, index).flatMap((group) => group.chips)
}

/**
 * 当前步的「上一步」产出：紧邻的前一个并行组里声明了产物的步骤，各列出根变量芯片。
 *
 * 只回退一组而不是所有上游——写模板时手边常接的就是这一级，更早的产物仍在
 * 「插入引用」弹层里完整列着。「上一步」按并行组取：同组内几步并发、彼此不可见，
 * 按数组下标取会点出一个后端存不下的引用。第一步前面没有组，返回空列表。
 */
export function upstreamGroupsFor(steps: StepForm[], index: number): ChipGroup[] {
  const sequences = groupSequence(steps)
  const own = sequences[index] ?? 0
  const target = own - 1
  if (target < 0) return []
  const groups: ChipGroup[] = []
  steps.forEach((step, stepIndex) => {
    if ((sequences[stepIndex] ?? 0) !== target) return
    const variable = step.output_var.trim()
    if (!variable) return
    groups.push({
      label: `第 ${stepIndex + 1} 步 · ${step.key.trim() || variable}`,
      chips: [`{{${variable}}}`],
    })
  })
  return groups
}

/**
 * 引用线：某步模板里用到的根变量，若来自前面某一组的产物，就从那一步连一条过来。
 *
 * 只连「产物 → 消费者」这一层，输入变量不参与画线：每个步骤都可能用到输入，
 * 全连上去就是一片糊，输入可用性在芯片那一栏列着。
 */
export function stepLinks(steps: StepForm[]): StepLink[] {
  const sequences = groupSequence(steps)
  const links: StepLink[] = []
  steps.forEach((consumer, consumerIndex) => {
    const own = sequences[consumerIndex] ?? 0
    for (const root of referencedVars(consumer.prompt_tpl)) {
      const producer = steps.findIndex(
        (step, index) =>
          index !== consumerIndex &&
          (sequences[index] ?? 0) < own &&
          step.output_var.trim() === root,
      )
      if (producer >= 0) links.push({ from: producer, to: consumerIndex, variable: root })
    }
  })
  return links
}

