/**
 * 模型配置域：chat / embedding 两种用途的模型条目、默认位、连通性测试与整体就绪状态。
 * TestResult 是通用的「测一下通不通」返回，ops.ts 的测试连接与测试库借用它。
 */
import { request } from './http'

// ---- 设置 ------------------------------------------------------------------

export type ModelPurpose = 'chat' | 'embedding'

/** 思维链：auto 上游发了才展示；on 带额外参数并要求展示；off 一律不展示。 */
export type ReasoningMode = 'auto' | 'on' | 'off'

export interface ModelConfig {
  id: number
  purpose: ModelPurpose
  title: string
  model_name: string
  base_url: string
  /** 掩码形式（sk-****abcd）；原样提交回来不会改动已保存的密钥。 */
  api_key: string
  api_key_error?: string | null
  remark?: string | null
  is_default: boolean
  vector_size?: number | null
  reasoning_mode?: ReasoningMode
  /** 仅 reasoning_mode=on 生效：原样并入请求体的私有参数。 */
  request_extra?: Record<string, unknown> | null
  last_tested_at?: string | null
  last_test_ok: boolean
  last_test_error?: string | null
  created_at: string
  updated_at: string
}

export interface ModelConfigPayload {
  purpose: ModelPurpose
  title: string
  model_name: string
  base_url: string
  api_key?: string
  remark?: string | null
  reasoning_mode?: ReasoningMode
  /** null = 清空；未传 = 保持原值。 */
  request_extra?: Record<string, unknown> | null
}

export interface TestResult {
  success: boolean
  message: string
  vector_size?: number | null
}

export interface PurposeStatus {
  ready: boolean
  model: string | null
  config_id: number | null
}

export interface ReadinessStatus {
  chat: PurposeStatus
  embedding: PurposeStatus
  kb: {
    status: string
    doc_count: number
    chunk_count: number
    model_mismatch: boolean
  }
}

export const settingsApi = {
  listModels: (purpose?: ModelPurpose) =>
    request<{ items: ModelConfig[]; total: number }>({
      url: '/settings/models',
      params: purpose ? { purpose } : undefined,
    }),
  createModel: (payload: ModelConfigPayload) =>
    request<ModelConfig>({ url: '/settings/models', method: 'POST', data: payload }),
  updateModel: (id: number, payload: Partial<ModelConfigPayload>) =>
    request<ModelConfig>({ url: `/settings/models/${id}`, method: 'PUT', data: payload }),
  deleteModel: (id: number) =>
    request<null>({ url: `/settings/models/${id}`, method: 'DELETE' }),
  setDefault: (id: number) =>
    request<ModelConfig>({ url: `/settings/models/${id}/default`, method: 'POST' }),
  testModel: (id: number) =>
    request<TestResult>({
      url: `/settings/models/${id}/test`,
      method: 'POST',
      // 探测远端服务商可能很慢，全局的 30 秒太紧了。
      timeout: 60000,
      skipErrorToast: true,
    }),
  status: () => request<ReadinessStatus>({ url: '/settings/models/status' }),
}
