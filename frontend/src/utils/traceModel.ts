/**
 * 统一 Trace 时间线的节点模型与适配函数。
 *
 * 三个来源——对话回答（chat_message.timeline）、工作流步骤（workflow_run_step.trace）、
 * 运维问答（复用对话 timeline）——各自的存储形状不同，渲染前都在这里适配成同一组
 * TraceNode，交给 components/trace/TraceTimeline.vue 一套组件渲染。
 *
 * 后端对应约定：
 * - chat timeline 条目：{type:'reason'|'step'|'llm'|'text'|'confirm', ...}，
 *   step 带 tool/query/input/run_id/output/is_error/started_at/elapsed_ms；
 * - workflow 步骤 trace 是扁平事件流：{kind:'llm',elapsed_ms} 与
 *   {kind:'tool_start'|'tool_end',name,args,output,is_error,elapsed_ms}，
 *   tool_start/tool_end 在这里配对成一个工具节点。
 */
import { toolLabel } from './chatTools'
import type { TimelineEntry, WorkflowTraceEvent } from '@/api'

export type TraceKind = 'reason' | 'tool' | 'llm'
export type TraceStatus = 'running' | 'done' | 'error'

export interface TraceBlock {
  label: string
  text: string
  tone?: 'normal' | 'error'
  /** 返回结果类的块给复制按钮；入参块不给。 */
  copy?: boolean
}

export interface TraceNode {
  key: string
  kind: TraceKind
  status: TraceStatus
  /** 行内主文案：工具的中文名/「模型思考」。 */
  title: string
  /** 工具原始名（悬浮提示用）。 */
  tool?: string
  /** 耗时毫秒；running 或老数据没有时为 null。 */
  elapsedMs: number | null
  /** 开始时刻 ISO 字符串（chat 侧有，工作流侧只有相对耗时）；悬浮在耗时上看。 */
  startedAt?: string
  /** 可展开的代码块（入参 / 返回结果）。 */
  blocks: TraceBlock[]
  /** reason 节点的思维链全文。 */
  detail?: string
}

function argsText(input: Record<string, unknown> | undefined): string {
  if (!input || !Object.keys(input).length) return ''
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

/** chat timeline 条目 → 统一节点。text/confirm（正文与确认提议）不属于过程，过滤掉。 */
export function chatTraceNodes(entries: TimelineEntry[]): TraceNode[] {
  const nodes: TraceNode[] = []
  let toolSeq = 0
  for (const entry of entries) {
    if (entry.type === 'reason') {
      nodes.push({
        key: `r${nodes.length}`,
        kind: 'reason',
        status: 'done',
        title: '思考',
        elapsedMs: null,
        blocks: [],
        detail: entry.detail ?? '',
      })
      continue
    }
    if (entry.type === 'llm') {
      nodes.push({
        key: `l${nodes.length}`,
        kind: 'llm',
        status: 'done',
        title: '模型思考',
        elapsedMs: entry.elapsed_ms ?? null,
        blocks: [],
      })
      continue
    }
    if (entry.type !== 'step') continue

    const blocks: TraceBlock[] = []
    const args = argsText(entry.input)
    if (args) blocks.push({ label: '入参', text: args })
    if (entry.output != null) {
      blocks.push({
        label: entry.is_error ? '错误返回' : '返回结果',
        text: entry.output,
        tone: entry.is_error ? 'error' : 'normal',
        copy: true,
      })
    }
    nodes.push({
      key: `t${toolSeq++}`,
      kind: 'tool',
      // output 还没回来且标了 running 才是执行中；历史条目没有 running 字段，按完成渲染。
      status: entry.running && entry.output == null ? 'running' : entry.is_error ? 'error' : 'done',
      title: toolLabel(entry.tool ?? '', entry.input),
      tool: entry.tool,
      elapsedMs: entry.elapsed_ms ?? null,
      startedAt: entry.started_at,
      blocks,
    })
  }
  return nodes
}

type ToolStartEvent = Extract<WorkflowTraceEvent, { kind: 'tool_start' }>
type ToolEndEvent = Extract<WorkflowTraceEvent, { kind: 'tool_end' }>

/**
 * 工作流步骤的扁平事件流 → 统一节点。
 *
 * run_agent 里工具是顺序 await 的（同一步骤内不会真交错），配对仍按「同名 start
 * 压栈、end 与最后一个未闭合的同名 start 结合」处理，稳妥不串。到流结束还没等到
 * end 的 start 保留为 running 节点（步骤被中断时正是这个形态）。
 */
export function workflowTraceNodes(raw: WorkflowTraceEvent[] | null | undefined): TraceNode[] {
  const nodes: TraceNode[] = []
  const pending: ToolStartEvent[] = []
  let seq = 0

  const pushTool = (start: ToolStartEvent, end: ToolEndEvent | null) => {
    const args = (start.args ?? null) as Record<string, unknown> | undefined
    const blocks: TraceBlock[] = []
    const argsString = argsText(args)
    if (argsString) blocks.push({ label: '入参', text: argsString })
    if (end?.output != null) {
      blocks.push({
        label: end.is_error ? '错误返回' : '返回结果',
        text: end.output,
        tone: end.is_error ? 'error' : 'normal',
        copy: true,
      })
    }
    nodes.push({
      key: `wt${seq++}`,
      kind: 'tool',
      status: end == null ? 'running' : end.is_error ? 'error' : 'done',
      title: start.name || '工具调用',
      tool: start.name,
      elapsedMs: end?.elapsed_ms ?? null,
      blocks,
    })
  }

  for (const event of raw ?? []) {
    if (event.kind === 'llm') {
      nodes.push({
        key: `wl${seq++}`,
        kind: 'llm',
        status: 'done',
        title: '模型思考',
        elapsedMs: event.elapsed_ms ?? null,
        blocks: [],
      })
    } else if (event.kind === 'tool_start') {
      pending.push(event)
    } else {
      // 与最后一个同名未闭合 start 配对；找不到归属就用 end 自身补一个节点。
      let matched = -1
      for (let i = pending.length - 1; i >= 0; i -= 1) {
        if (pending[i].name === event.name) {
          matched = i
          break
        }
      }
      if (matched >= 0) {
        const [start] = pending.splice(matched, 1)
        pushTool(start, event)
      } else {
        pushTool({ kind: 'tool_start', name: event.name, args: event.args }, event)
      }
    }
  }
  for (const start of pending) pushTool(start, null)
  return nodes
}
