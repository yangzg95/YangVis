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
}
