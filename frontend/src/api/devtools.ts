/** 开发工具：本地 Java 接口扫描与 YApi 同步。 */
import { request } from './http'

export type YapiParamLocation = 'query' | 'path' | 'header' | 'body' | 'form'

export interface YapiEndpointParam {
  name: string
  location: YapiParamLocation
  type: string
  required: boolean
  description: string
  example: string
}

export interface YapiScanEndpoint {
  package: string
  category_name: string
  controller: string
  method: string
  title: string
  http_method: string
  path: string
  tag: string
  summary: string
  description: string
  source: string
  line: number
  params: YapiEndpointParam[]
  request_body_type: 'json' | 'form' | ''
  request_body_schema: Record<string, unknown> | null
  response_schema: Record<string, unknown> | null
}

export interface YapiScanCategory {
  package: string
  category_name: string
  controllers: string[]
  controller_count: number
  endpoint_count: number
}

export interface YapiScanResult {
  root_path: string
  java_files: number
  controller_count: number
  endpoint_count: number
  categories: YapiScanCategory[]
  endpoints: YapiScanEndpoint[]
  warnings: string[]
  duration_ms: number
}

export interface YapiScanPayload {
  root_path: string
}

export interface YapiSyncPayload extends YapiScanPayload {
  base_url: string
  project_id: number
  token: string
}

export type YapiSyncAction = 'created' | 'updated' | 'failed'

export interface YapiSyncEndpoint extends YapiScanEndpoint {
  action: YapiSyncAction
  error: string | null
}

export interface YapiSyncResult {
  base_url: string
  project_id: number
  created: number
  updated: number
  failed: number
  categories_created: number
  endpoint_count: number
  endpoints: YapiSyncEndpoint[]
  warnings: string[]
  duration_ms: number
}

// 扫描/同步都在后端同步执行：大仓库扫描可能几十秒，逐条写 YApi 更久，
// 所以超时单独放宽，不跟全局 30s 走。
export const devtoolsApi = {
  pickDirectory: () =>
    request<string | null>({
      url: '/devtools/pick-directory',
      method: 'POST',
      // 弹窗会一直等到用户选择或取消，超时跟扫描一样放宽。
      timeout: 330000,
    }),
  scanYapi: (payload: YapiScanPayload) =>
    request<YapiScanResult>({
      url: '/devtools/yapi/scan',
      method: 'POST',
      data: payload,
      timeout: 180000,
    }),
  syncYapi: (payload: YapiSyncPayload) =>
    request<YapiSyncResult>({
      url: '/devtools/yapi/sync',
      method: 'POST',
      data: payload,
      timeout: 600000,
    }),
}
