/** 智能体域：智能体定义的列表、增删改与复制。 */
import { request } from './http'

// ---- 智能体 ----------------------------------------------------------------

export interface Agent {
  id: number
  slug: string
  name: string
  description?: string | null
  system_prompt: string
  use_knowledge: boolean
  use_ops: boolean
  use_memory: boolean
  chat_visible: boolean
  /** 0-100 整数；null=跟随模型配置。 */
  temperature: number | null
  is_builtin: boolean
  enabled: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

export interface AgentPayload {
  slug: string
  name: string
  description?: string | null
  system_prompt: string
  use_knowledge?: boolean
  use_ops?: boolean
  use_memory?: boolean
  chat_visible?: boolean
  /** 0-100 整数；null=跟随模型配置。 */
  temperature?: number | null
  sort_order?: number
}

/** 版本列表行：只带辨认用的名字，正文在详情里取。 */
export interface AgentVersionItem {
  version: number
  name: string
  created_at: string
}

/** 某个版本的完整人设快照：与 Agent 上影响输出的字段同形。 */
export interface AgentVersionSnapshot {
  name: string
  description?: string | null
  system_prompt: string
  temperature?: number | null
  use_knowledge: boolean
  use_ops: boolean
  use_memory: boolean
  chat_visible: boolean
}

export interface AgentVersionDetail {
  version: number
  created_at: string
  snapshot: AgentVersionSnapshot
}

export const agentsApi = {
  list: (enabledOnly = false) =>
    request<{ items: Agent[]; total: number }>({
      url: '/agents',
      params: enabledOnly ? { enabled_only: true } : undefined,
    }),
  create: (payload: AgentPayload) =>
    request<Agent>({ url: '/agents', method: 'POST', data: payload }),
  update: (id: number, payload: Partial<AgentPayload> & { enabled?: boolean }) =>
    request<Agent>({ url: `/agents/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/agents/${id}`, method: 'DELETE' }),
  duplicate: (id: number) =>
    request<Agent>({ url: `/agents/${id}/duplicate`, method: 'POST' }),
  listVersions: (id: number) =>
    request<{ items: AgentVersionItem[]; total: number }>({
      url: `/agents/${id}/versions`,
    }),
  versionDetail: (id: number, version: number) =>
    request<AgentVersionDetail>({ url: `/agents/${id}/versions/${version}` }),
}
