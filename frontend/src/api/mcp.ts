/** MCP 服务域：条目的增删改、连通测试与配置的导入导出。 */
import { request } from './http'

// ---- MCP 服务 ----------------------------------------------------------------

export type McpTransport = 'stdio' | 'sse' | 'streamable_http'

export interface McpServer {
  id: number
  owner_id: number
  name: string
  transport: McpTransport
  /** sse / streamable_http 的服务端点。 */
  url?: string | null
  /** stdio 的启动命令与参数。 */
  command?: string | null
  args: string[]
  /** 回参的值是掩码（sk-****xxxx），不是明文。 */
  env: Record<string, string>
  headers: Record<string, string>
  enabled: boolean
  remark?: string | null
  last_test_at?: string | null
  last_test_ok?: boolean | null
  last_test_msg?: string | null
  /** 最近一次测试成功时发现的工具快照；未测/失败/连接信息已变更后为 null。 */
  tools?: McpToolSnapshot[] | null
  created_at: string
  updated_at: string
  /** 密文解密失败时为 true，需要重新填写密钥。 */
  secret_error?: boolean
}

/** 连通测试时从 MCP 服务发现的一个工具快照。 */
export interface McpToolSnapshot {
  name: string
  description: string
  /** 原始 JSON Schema，参数渲染时取 properties / required。 */
  input_schema: Record<string, unknown>
}

export interface McpTestResult {
  success: boolean
  message: string
  tools: McpToolSnapshot[]
}

/** MCP 服务的编辑载荷：env/headers 填明文，掩码或空值的语义见页面说明。 */
export interface McpServerPayload {
  name: string
  transport: McpTransport
  url?: string | null
  command?: string | null
  args?: string[]
  env?: Record<string, string>
  headers?: Record<string, string>
  enabled?: boolean
  remark?: string | null
}

/** 导入时的同名处理：rename 自动改名 / skip 跳过 / overwrite 覆盖。 */
export type McpConflictStrategy = 'rename' | 'skip' | 'overwrite'

export interface McpImportReport {
  created: number
  skipped: number
  overwritten: number
  renamed: string[]
  errors: string[]
}

export interface McpExportResponse {
  mcpServers: Record<string, Record<string, unknown>>
  warnings: string[]
}

export const mcpApi = {
  list: () => request<{ items: McpServer[]; total: number }>({ url: '/mcp' }),
  get: (id: number) => request<McpServer>({ url: `/mcp/${id}` }),
  create: (payload: McpServerPayload) =>
    request<McpServer>({ url: '/mcp', method: 'POST', data: payload }),
  update: (id: number, payload: Partial<McpServerPayload>) =>
    request<McpServer>({ url: `/mcp/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/mcp/${id}`, method: 'DELETE' }),
  /** 真正建立连接并 initialize：成功带回工具快照，失败报原因。 */
  test: (id: number) =>
    request<McpTestResult>({
      url: `/mcp/${id}/test`,
      method: 'POST',
    }),
  /** 导出 mcpServers 标准文档：ids 为空导出全部。 */
  exportConfig: (ids?: number[]) => {
    // URLSearchParams 保证序列化成 ids=1&ids=2（而非 ids[]=1）。
    const params = ids?.length
      ? new URLSearchParams(ids.map((id) => ['ids', String(id)]))
      : undefined
    return request<McpExportResponse>({ url: '/mcp/export', params })
  },
  /** 导入 mcpServers 文档批量创建。 */
  importConfig: (config: Record<string, unknown>, conflict: McpConflictStrategy) =>
    request<McpImportReport>({
      url: '/mcp/import',
      method: 'POST',
      data: { config, conflict },
    }),
}
