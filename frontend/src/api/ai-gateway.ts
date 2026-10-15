/**
 * AI 网关域（仅管理员）：渠道、模型路由、对外密钥、统计与调用日志。
 * 它是平台级的共享资产，与各业务域自己的模型配置（settings.ts）不是一回事。
 */
import { request } from './http'

// ---- AI 网关（仅管理员） ----------------------------------------------------

export interface AiChannelItem {
  id: number
  name: string
  base_url: string
  /** 掩码，明文永不回传。 */
  api_key: string
  api_key_error: string | null
  protocol: string
  models: string[] | null
  enabled: boolean
  remark: string | null
  last_tested_at: string | null
  last_test_ok: boolean
  last_test_error: string | null
  created_at: string
  updated_at: string
}

export interface AiChannelPayload {
  name: string
  base_url: string
  api_key?: string | null
  models?: string[] | null
  enabled?: boolean
  remark?: string | null
}

/** Auto 判定用的标签：能力标签参与筛选，任务类型标签参与排序。 */
export type AiAutoTag =
  | 'chat'
  | 'embedding'
  | 'vision'
  | 'tools'
  | 'general'
  | 'reasoning'
  | 'code'
  | 'writing'
  | 'translation'

/** 一次 Auto 调用落进日志的决策快照。 */
export interface AiAutoDecision {
  required: string[]
  task: string | null
  /** priority=只按优先级，llm=问过任务类型，llm_failed / llm_unclear=问过但没采用。 */
  source: string
  matched: number
  resolved_model?: string
}

export interface AiModelRouteItem {
  id: number
  model_name: string
  channel_id: number
  channel_name: string | null
  upstream_model: string | null
  priority: number
  enabled: boolean
  remark: string | null
  auto_eligible: boolean
  auto_tags: AiAutoTag[] | null
  created_at: string
  updated_at: string
}

export interface AiModelRoutePayload {
  model_name: string
  channel_id: number
  upstream_model?: string | null
  priority?: number
  enabled?: boolean
  remark?: string | null
  auto_eligible?: boolean
  auto_tags?: AiAutoTag[] | null
}

export interface AiApiKeyItem {
  id: number
  name: string
  key_prefix: string
  enabled: boolean
  remark: string | null
  call_count: number
  last_used_at: string | null
  created_at: string
  updated_at: string
}

export interface AiApiKeyCreated {
  item: AiApiKeyItem
  /** 明文只在建钥匙这一次返回，关掉弹窗就再也拿不到。 */
  api_key: string
}

export interface AiCallLogItem {
  id: number
  request_id: string
  key_id: number | null
  key_name: string | null
  endpoint: string
  model: string | null
  stream: boolean
  channel_id: number | null
  channel_name: string | null
  upstream_model: string | null
  /** Auto 那次的决策快照；点名具体模型时为 null。 */
  auto_route: AiAutoDecision | null
  status_code: number
  success: boolean
  error: string | null
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  latency_ms: number
  first_token_ms: number | null
  client_ip: string | null
  created_at: string
}

export interface AiCallAttempt {
  channel_id: number
  channel_name: string
  status_code: number
  error: string | null
  latency_ms: number
}

export interface AiCallLogDetail extends AiCallLogItem {
  attempts: AiCallAttempt[] | null
  request_body: string | null
  response_body: string | null
}

export interface AiStatsBucket {
  name: string
  calls: number
  failed: number
  total_tokens: number
  avg_latency_ms: number
}

export interface AiStats {
  totals: {
    calls: number
    success: number
    failed: number
    prompt_tokens: number
    completion_tokens: number
    total_tokens: number
    avg_latency_ms: number
    max_latency_ms: number
  }
  daily: AiStatsBucket[]
  by_channel: AiStatsBucket[]
  by_model: AiStatsBucket[]
  by_key: AiStatsBucket[]
}

export interface AiGatewayOverview {
  /** 完整接入地址（含 /v1），直接复制去填 SDK 的 base_url。 */
  base_url: string
  channel_count: number
  channel_enabled: number
  route_count: number
  route_enabled: number
  key_count: number
  key_enabled: number
  log_count: number
  payload_logging: boolean
}

export interface AiLogQuery {
  page?: number
  page_size?: number
  key_id?: number
  channel_id?: number
  model?: string
  endpoint?: string
  success?: boolean
  keyword?: string
  start?: string
  end?: string
}

export const aiGatewayApi = {
  overview: () => request<AiGatewayOverview>({ url: '/ai-gateway/overview' }),

  channels: () =>
    request<{ items: AiChannelItem[]; total: number }>({ url: '/ai-gateway/channels' }),
  createChannel: (payload: AiChannelPayload) =>
    request<AiChannelItem>({ url: '/ai-gateway/channels', method: 'POST', data: payload }),
  updateChannel: (id: number, payload: Partial<AiChannelPayload>) =>
    request<AiChannelItem>({ url: `/ai-gateway/channels/${id}`, method: 'PUT', data: payload }),
  removeChannel: (id: number) =>
    request<null>({ url: `/ai-gateway/channels/${id}`, method: 'DELETE' }),
  testChannel: (id: number, model?: string) =>
    request<{ success: boolean; message: string }>({
      url: `/ai-gateway/channels/${id}/test`,
      method: 'POST',
      data: { model: model || null },
      // 真发一次上游请求，全局 30 秒不够。
      timeout: 120000,
      skipErrorToast: true,
    }),

  routes: (modelName?: string) =>
    request<{ items: AiModelRouteItem[]; total: number }>({
      url: '/ai-gateway/routes',
      params: modelName ? { model_name: modelName } : undefined,
    }),
  createRoute: (payload: AiModelRoutePayload) =>
    request<AiModelRouteItem>({ url: '/ai-gateway/routes', method: 'POST', data: payload }),
  updateRoute: (id: number, payload: Partial<AiModelRoutePayload>) =>
    request<AiModelRouteItem>({ url: `/ai-gateway/routes/${id}`, method: 'PUT', data: payload }),
  removeRoute: (id: number) => request<null>({ url: `/ai-gateway/routes/${id}`, method: 'DELETE' }),

  keys: () => request<{ items: AiApiKeyItem[]; total: number }>({ url: '/ai-gateway/keys' }),
  createKey: (payload: { name: string; remark?: string | null }) =>
    request<AiApiKeyCreated>({ url: '/ai-gateway/keys', method: 'POST', data: payload }),
  updateKey: (id: number, payload: { name?: string; enabled?: boolean; remark?: string | null }) =>
    request<AiApiKeyItem>({ url: `/ai-gateway/keys/${id}`, method: 'PUT', data: payload }),
  removeKey: (id: number) => request<null>({ url: `/ai-gateway/keys/${id}`, method: 'DELETE' }),

  stats: (days = 7) => request<AiStats>({ url: '/ai-gateway/stats', params: { days } }),
  logs: (params: AiLogQuery) =>
    request<{ items: AiCallLogItem[]; total: number }>({ url: '/ai-gateway/logs', params }),
  logDetail: (id: number) => request<AiCallLogDetail>({ url: `/ai-gateway/logs/${id}` }),
  purgeLogs: (beforeDays: number) =>
    request<{ deleted: number }>({
      url: '/ai-gateway/logs/purge',
      method: 'POST',
      data: { before_days: beforeDays },
      timeout: 120000,
    }),
}
