/**
 * 工作流域：定义与步骤的维护、运行列表与详情、单步重跑与断点续跑、留存清理。
 * 发起运行和「跟随进度」都是 SSE，共用 sse.ts 的 streamSse 与 StreamHandlers。
 */
import { request } from './http'
import { streamSse, type StreamHandlers } from './sse'

// ---- 工作流 ----------------------------------------------------------------

export interface WorkflowInputField {
  name: string
  label: string
  required: boolean
}

export interface WorkflowStep {
  key: string
  /** 同组并行执行，组间按数组顺序（也就是 sort_order）依次跑。 */
  group_key: string
  sort_order: number
  agent_slug: string
  agent_name?: string | null
  prompt_tpl: string
  output_kind: string
  output_keys?: string[] | null
  /** 有值则本步输出以该名进上下文，供后续步骤的模板引用。 */
  output_var?: string | null
  model_config_id?: number | null
  /** 节点级挂载的 MCP 服务：仅本步生效，与工作流级 mcp_server_ids 取并集。 */
  mcp_server_ids: number[]
  optional: boolean
}

export interface WorkflowLastRun {
  id: number
  status: RunStatus
  elapsed_ms?: number | null
  /** 结束时间，没结束就是开始时间，pending 的行只有创建时间。 */
  at?: string | null
}

export interface Workflow {
  id: number
  slug: string
  name: string
  description?: string | null
  inputs: WorkflowInputField[]
  steps: WorkflowStep[]
  /** 挂载的 MCP 服务 id：运行时所有步骤都能调用这些服务的工具。 */
  mcp_server_ids: number[]
  is_builtin: boolean
  enabled: boolean
  sort_order: number
  /** 最近一次运行的摘要；从没跑过是 null，卡片上整行不渲染。 */
  last_run?: WorkflowLastRun | null
  created_at: string
  updated_at: string
}

/** 提交给后端的整份定义：创建与更新都是整体替换，不是补丁。 */
export interface WorkflowPayload {
  slug?: string
  name: string
  description?: string | null
  inputs: WorkflowInputField[]
  steps: Omit<WorkflowStep, 'sort_order' | 'agent_name'>[]
  mcp_server_ids: number[]
  enabled?: boolean
}

export type RunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type RunStepStatus = RunStatus | 'skipped'

/** 一次运行认领的业务对象类型，与后端 services/workflows.py 的 SUBJECT_* 一致。 */
export type WorkflowSubjectKind =
  | 'resume'
  | 'resume-comparison'
  | 'resume-toolkit'
  | 'slide-deck'
  | 'interview-question'

/** 抽屉按业务对象过滤时的一次入参；label 是列表标题用的名字。 */
export interface WorkflowRunSubject {
  kind: WorkflowSubjectKind
  ref: string | number
  /** 后端有 subject_label 快照，调用方手上有更准的名字时可以直接给。 */
  label?: string
}

export interface WorkflowRun {
  id: number
  workflow_id: number
  /** 定义可能已改名或删除，历史列表靠这一列认得出当时跑的是哪条。 */
  workflow_slug: string
  /** 这次运行是为哪个业务对象跑的；工作流页面上手动跑的那几次是 null。 */
  subject_kind?: WorkflowSubjectKind | null
  subject_ref?: string | null
  /** 发起时抄下的对象名字：业务行删了、改了名，运行记录仍说得出当时给谁跑。 */
  subject_label?: string | null
  status: RunStatus
  error_msg?: string | null
  elapsed_ms?: number | null
  /** 这次运行各步用到的模型名，按步骤顺序去重（列表行上直接显示，不必展开）。 */
  model_names?: string[]
  started_at?: string | null
  finished_at?: string | null
  created_at: string
}

/**
 * 步骤内部统一执行轨迹的一个扁平事件（run_agent 黑盒打开后的记录）。
 * - llm：一次模型往返，只带耗时；
 * - tool_start / tool_end：一个工具调用的起止，前端按 name 配对，
 *   只收到 start 没收到 end（步骤被中断）时该工具按执行中渲染。
 */
export type WorkflowTraceEvent =
  | { kind: 'llm'; elapsed_ms?: number | null }
  | { kind: 'tool_start'; name: string; args?: unknown }
  | {
      kind: 'tool_end'
      name: string
      args?: unknown
      output?: string
      is_error?: boolean
      elapsed_ms?: number | null
    }

export interface WorkflowRunStep {
  id: number
  key: string
  group_key: string
  agent_slug: string
  /** 开跑时绑定的人设版本：功能上线前的老运行两列都是 null。 */
  agent_id?: number | null
  agent_version?: number | null
  status: RunStepStatus
  /** 这一步挂的人设与要什么格式：来自流程当前的定义，定义改过就对不上，为 null。 */
  agent_name?: string | null
  output_kind?: string | null
  /** 产物以什么名字进的上下文：据此还原「这一步收到了哪些输入」，定义改过则为 null。 */
  output_var?: string | null
  optional?: boolean
  /** 渲染后的 prompt：「当时模型到底收到了什么」问得出来。 */
  prompt?: string | null
  output?: string | null
  parsed?: Record<string, unknown> | null
  /** 步骤内部的执行轨迹（模型往返 / 工具调用）；run_agent 黑盒时代的老运行为 null。 */
  trace?: WorkflowTraceEvent[] | null
  error_msg?: string | null
  model_config_id?: number | null
  /** 这一步当时用的模型：按步骤覆盖与默认模型可能混在同一条流程里，按步才说得清。 */
  model_name?: string | null
  elapsed_ms?: number | null
  started_at?: string | null
  finished_at?: string | null
}

export interface WorkflowRunDetail extends WorkflowRun {
  inputs: Record<string, string>
  steps: WorkflowRunStep[]
}

export const workflowsApi = {
  list: (enabledOnly = false) =>
    request<{ items: Workflow[]; total: number }>({
      url: '/workflows',
      params: enabledOnly ? { enabled_only: true } : undefined,
    }),
  get: (id: number) => request<Workflow>({ url: `/workflows/${id}` }),
  create: (payload: WorkflowPayload) =>
    request<Workflow>({ url: '/workflows', method: 'POST', data: payload }),
  update: (id: number, payload: WorkflowPayload) =>
    request<Workflow>({ url: `/workflows/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/workflows/${id}`, method: 'DELETE' }),
  duplicate: (id: number) =>
    request<Workflow>({ url: `/workflows/${id}/duplicate`, method: 'POST' }),
  listRuns: (
    params: {
      workflowId?: number
      status?: RunStatus
      page?: number
      pageSize?: number
      subject?: [WorkflowSubjectKind, string | number]
    } = {},
  ) =>
    request<{ items: WorkflowRun[]; total: number }>({
      url: '/workflows/runs',
      params: {
        workflow_id: params.workflowId,
        // kind 与 ref 成对：整数 id 在各张表上各自编号，只给 ref 会把
        // 「简历 3」和「deck 3」混进同一页，后端会直接 400。
        subject_kind: params.subject?.[0],
        subject_ref: params.subject?.[1],
        status: params.status,
        page: params.page ?? 1,
        page_size: params.pageSize ?? 20,
      },
    }),
  runDetail: (id: number) => request<WorkflowRunDetail>({ url: `/workflows/runs/${id}` }),
  /**
   * 发起一次运行并跟随进度。
   *
   * 事件只报状态与长度，正文一律从 {@link runDetail} 按步取：中途换设备、
   * 流断开之后打开历史详情，看到的进度是一样的。
   */
  streamRun: (workflowId: number, inputs: Record<string, string>, handlers: StreamHandlers) =>
    streamSse(`/api/workflows/${workflowId}/runs/stream`, { inputs }, handlers),
  /**
   * 接上一次已经在跑的运行：先回放现状，再等它跑完。
   *
   * 功能页面发起的生成本来就不在 {@link streamRun} 那条流里跑，页面中途打开时
   * 用这个跟上。纯读，所以是 GET。
   */
  followRun: (runId: number, handlers: StreamHandlers) =>
    streamSse(`/api/workflows/runs/${runId}/stream`, undefined, handlers, 'GET'),
  /**
   * 重跑一次运行里的单个步骤。接口立刻返回，进度还是走 {@link followRun}。
   *
   * 这一步的产物不会写回业务行——后处理只认整条流程跑完，要新结果请回功能页面
   * 重新发起一次完整任务。
   */
  rerunStep: (runId: number, key: string, inputs?: Record<string, string>) =>
    request<WorkflowRun>({
      url: `/workflows/runs/${runId}/steps/${encodeURIComponent(key)}/rerun`,
      method: 'POST',
      data: { inputs: inputs ?? {} },
    }),
  /**
   * 从某一步所在并行组继续跑到结束。接口立刻返回，进度还是走 {@link followRun}。
   *
   * 目标组及之后各组重置重跑；inputs 覆盖这次续跑起点收到的上下文变量，不写回
   * 任何行。与单步重跑一样不写回业务行，要新结果请回功能页面重新发起完整任务。
   */
  resumeFromStep: (runId: number, key: string, inputs: Record<string, string>) =>
    request<WorkflowRun>({
      url: `/workflows/runs/${runId}/steps/${encodeURIComponent(key)}/resume`,
      method: 'POST',
      data: { inputs },
    }),
  /** 清掉早于 keep_days、又不再是谁的当前状态的运行记录。 */
  pruneRuns: (keepDays: number) =>
    request<{ deleted_runs: number; deleted_steps: number }>({
      url: '/workflows/runs/prune',
      method: 'POST',
      data: { keep_days: keepDays },
    }),
}

// ---- 运行对比评测 -----------------------------------------------------------

export interface EvalCriterion {
  name: string
  description?: string | null
}

export interface EvalRubric {
  id: number
  name: string
  description?: string | null
  criteria: EvalCriterion[]
  created_at: string
  updated_at: string
}

export interface EvalRubricPayload {
  name: string
  description?: string | null
  criteria: EvalCriterion[]
}

export interface EvalScore {
  /** 对齐评分细则快照的下标。 */
  index: number
  score_a: number
  score_b: number
}

export interface EvalRunBrief {
  id: number
  status: RunStatus | 'unknown'
  subject_label?: string | null
  elapsed_ms?: number | null
  created_at: string
}

export interface WorkflowEvaluation {
  id: number
  workflow_id: number
  rubric_id: number
  /** 细则可能已删：null 时标准仍以 rubric_snapshot 为准。 */
  rubric_name?: string | null
  rubric_snapshot: EvalCriterion[]
  run_a_id: number
  run_b_id: number
  run_a: EvalRunBrief
  run_b: EvalRunBrief
  scores: EvalScore[]
  avg_a: number
  avg_b: number
  /** avg_b - avg_a：正数 B 好，负数 A 好。 */
  diff: number
  note?: string | null
  created_at: string
  updated_at: string
}

export const evalApi = {
  listRubrics: () =>
    request<{ items: EvalRubric[]; total: number }>({ url: '/workflows/rubrics' }),
  createRubric: (payload: EvalRubricPayload) =>
    request<EvalRubric>({ url: '/workflows/rubrics', method: 'POST', data: payload }),
  updateRubric: (id: number, payload: EvalRubricPayload) =>
    request<EvalRubric>({ url: `/workflows/rubrics/${id}`, method: 'PUT', data: payload }),
  removeRubric: (id: number) =>
    request<null>({ url: `/workflows/rubrics/${id}`, method: 'DELETE' }),
  /** 某次运行参与过的评测；withRunId 给定时收敛到这一对运行之间。 */
  listForRun: (runId: number, withRunId?: number) =>
    request<{ items: WorkflowEvaluation[]; total: number }>({
      url: '/workflows/evaluations',
      params: { run_id: runId, with_run_id: withRunId },
    }),
  /** 提交评测：同对运行 + 同一把尺子重复提交，后端按更新处理。 */
  save: (payload: {
    run_a_id: number
    run_b_id: number
    rubric_id: number
    scores: EvalScore[]
    note?: string | null
  }) =>
    request<WorkflowEvaluation>({ url: '/workflows/evaluations', method: 'POST', data: payload }),
  remove: (id: number) =>
    request<null>({ url: `/workflows/evaluations/${id}`, method: 'DELETE' }),
}
