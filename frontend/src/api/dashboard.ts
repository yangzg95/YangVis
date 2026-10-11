/** 首页工作台域：一页汇总计数。 */
import { request } from './http'

// ---- 首页工作台 --------------------------------------------------------------

export interface DashboardSummary {
  agent_count: number
  knowledge_project_count: number
  knowledge_document_count: number
  conversation_count: number
  resume_count: number
  slide_count: number
  interview_count: number
  workflow_count: number
  /** 今天发起过的运行次数：流水线的存在感不在「有几条定义」，在「今天跑过没有」。 */
  run_count_today: number
  server_count: number
  database_count: number
  /** 网关是平台级共享资产，只对管理员出数；null 表示无权查看而非「一个都没有」。 */
  ai_channel_count: number | null
  ai_call_count_today: number | null
  ai_token_count_today: number | null
}

export const dashboardApi = {
  summary: () => request<DashboardSummary>({ url: '/dashboard/summary' }),
}
