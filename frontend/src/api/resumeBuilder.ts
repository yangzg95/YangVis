/**
 * 智能办公 · 在线简历编辑器接口。
 *
 * CRUD 走统一信封的 request；AI 帮写没有信封，必须用 sse.ts 的 streamSse
 * （axios 拦截器会把 SSE 当 JSON 拆掉）。事件约定与对话流一致：
 * token 增量、done 收尾、error 带 {code,message}。
 */
import { request } from './http'
import { streamSse, type StreamHandlers } from './sse'
import type { ResumeDocData, ResumeModuleKey } from '@/utils/resumeDoc'

export interface ResumeDocItem {
  id: number
  title: string
  updated_at: string
  created_at: string
}

export interface ResumeDocDetail extends ResumeDocItem {
  /** 出库时后端已按当前契约归一化过。 */
  doc: ResumeDocData
}

export type ResumeAiAction = 'write' | 'polish' | 'check'

export interface ResumeAiPayload {
  doc_id?: number | null
  module: ResumeModuleKey
  action: ResumeAiAction
  /** 三类经历模块的条目 id（cid/pid/eid）；个人信息/技能不带。 */
  item_id?: string | null
  /** 用户选中的文字：润色原文或帮写聚焦点。 */
  selection?: string | null
}

export type ResumeAiHandlers = Pick<StreamHandlers, 'onToken' | 'onDone' | 'onError'>

export const resumeBuilderApi = {
  list: () => request<{ items: ResumeDocItem[]; total: number }>({ url: '/office/resume-docs' }),
  create: (title: string) =>
    request<ResumeDocDetail>({ url: '/office/resume-docs', method: 'POST', data: { title } }),
  detail: (id: number) => request<ResumeDocDetail>({ url: `/office/resume-docs/${id}` }),
  update: (id: number, payload: { title?: string; doc?: ResumeDocData }) =>
    request<ResumeDocDetail>({ url: `/office/resume-docs/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) =>
    request<null>({ url: `/office/resume-docs/${id}`, method: 'DELETE' }),

  /** 流式 AI 帮写/润色/体检；返回的函数用于「停止」。 */
  aiAssist: (payload: ResumeAiPayload, handlers: ResumeAiHandlers): (() => void) =>
    streamSse('/api/office/resume-docs/ai/assist', payload, handlers),
}
