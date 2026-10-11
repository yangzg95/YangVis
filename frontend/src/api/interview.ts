/** 面试记录域：面试条目、问题清单与参考答案生成。 */
import { request } from './http'

// ---- 智能应用 · 面试记录 ---------------------------------------------------------

export type InterviewResult = 'pending' | 'passed' | 'failed' | 'offer'

/** 每题 AI 参考答案的生成状态。 */
export type InterviewRefStatus = 'none' | 'analyzing' | 'ready' | 'error'

export interface InterviewQuestionPayload {
  question: string
  my_answer?: string
  note?: string
}

/** 落库后的一条问题：比提交载荷多了 qid 与 AI 参考答案三件套，可空字段用 null。 */
export interface InterviewQuestion {
  qid: string
  question: string
  my_answer: string | null
  note: string | null
  ref_answer: string | null
  ref_status: InterviewRefStatus
  ref_error: string | null
}

export interface InterviewCreate {
  company: string
  position: string
  interview_date?: string
  round?: string
  result?: InterviewResult
  notes?: string
}

/** 元数据修改；questions 有题目级端点，不走这里（避免覆盖后台回写的 ref_*）。 */
export type InterviewUpdate = Partial<InterviewCreate>

export interface InterviewItem {
  id: number
  company: string
  position: string
  interview_date: string | null
  round: string | null
  result: InterviewResult
  notes: string | null
  question_count: number
  created_at: string
  updated_at: string
}

export interface InterviewDetail extends InterviewItem {
  questions: InterviewQuestion[]
}

export const interviewApi = {
  list: () => request<{ items: InterviewItem[]; total: number }>({ url: '/office/interviews' }),
  create: (payload: InterviewCreate) =>
    request<InterviewDetail>({ url: '/office/interviews', method: 'POST', data: payload }),
  detail: (id: number) => request<InterviewDetail>({ url: `/office/interviews/${id}` }),
  update: (id: number, payload: InterviewUpdate) =>
    request<InterviewDetail>({ url: `/office/interviews/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/office/interviews/${id}`, method: 'DELETE' }),

  // 题目级写操作统一返回整条详情，调用方直接整换本地数据。
  addQuestion: (id: number, payload: InterviewQuestionPayload) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions`,
      method: 'POST',
      data: payload,
    }),
  updateQuestion: (id: number, qid: string, payload: InterviewQuestionPayload) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}`,
      method: 'PUT',
      data: payload,
    }),
  removeQuestion: (id: number, qid: string) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}`,
      method: 'DELETE',
    }),
  generateAnswer: (id: number, qid: string) =>
    request<InterviewDetail>({
      url: `/office/interviews/${id}/questions/${qid}/answer`,
      method: 'POST',
    }),
}
