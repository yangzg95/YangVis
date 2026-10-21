/**
 * 工具目录域：目前只有后端代码注册的「本地工具」只读目录。
 * 本地工具以代码为唯一事实源（后端 services/tools/builtins/），没有写接口；
 * MCP 服务的管理沿用 mcp.ts。
 */
import { request } from './http'

export interface LocalToolParam {
  name: string
  description: string
  /** 目录展示用的 JSON 类型粗映射：integer/boolean/number/array/object/string。 */
  type: string
  required: boolean
  default?: unknown
}

export interface LocalTool {
  /** 即模型看到的工具名，^[a-z0-9_]+$，选择器用 `local:<key>` 复合值引用。 */
  key: string
  title: string
  description: string
  params: LocalToolParam[]
  /** 注册位置，如 services/tools/builtins/random_tools.py:13。 */
  source: string
  is_async: boolean
}

export const toolsApi = {
  listLocal: () =>
    request<{ items: LocalTool[]; total: number }>({ url: '/tools/local' }),
}
