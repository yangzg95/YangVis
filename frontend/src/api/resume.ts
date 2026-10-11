/**
 * 简历域：上传后的解析状态、预览与下载、对比记录，以及求职助手（简历工具箱）的任务与详情。
 * 原文件的「简历」一节只有类型、没有值导出，resumeApi 挂在「求职助手」横幅下，两块本就是一对。
 */
import type { AxiosResponse } from 'axios'
import { request } from './http'

// ---- 智能应用 · 简历 ---------------------------------------------------------

export type ResumeStatus = 'uploaded' | 'analyzing' | 'ready' | 'error'

export interface ResumeItem {
  id: number
  title: string
  description: string | null
  filename: string
  mime: string | null
  size: number
  status: ResumeStatus
  error_msg: string | null
  has_report: boolean
  /** 原文件是否已同步到用户自己的百度网盘；为 true 才展示「下载原件」。 */
  has_netdisk: boolean
  analyzed_at: string | null
  created_at: string
  updated_at: string
}

export interface ResumeDetail extends ResumeItem {
  report: string | null
  suggestions: string[] | null
}

export interface ResumePreview {
  id: number
  title: string
  filename: string
  mime: string | null
  size: number
  content: string
}

export interface ResumeComparisonItem {
  id: number
  title: string | null
  resume_ids: number[]
  resume_titles: string[]
  status: 'analyzing' | 'ready' | 'error'
  error_msg: string | null
  created_at: string
}

export interface ResumeComparisonDetail extends ResumeComparisonItem {
  report: string | null
}

// ---- 智能应用 · 求职助手 -------------------------------------------------------

export type ToolkitKind =
  | 'optimize'
  | 'career-match'
  | 'jd-match'
  | 'interview-prep'
  | 'portfolio-plan'
  | 'salary-negotiation'

export interface ResumeToolkitCreate {
  kind: ToolkitKind
  /** 简历来源二选一：引用已上传简历或直接粘贴文本。 */
  resume_id?: number
  resume_text?: string
  job_description?: string
  position?: string
  background?: string
  offer_amount?: string
  notes?: string
}

export interface ResumeToolkitItem {
  id: number
  kind: ToolkitKind
  kind_label: string
  title: string
  status: 'analyzing' | 'ready' | 'error'
  error_msg: string | null
  created_at: string
}

export interface ResumeToolkitDetail extends ResumeToolkitItem {
  inputs: Record<string, unknown>
  report: string | null
}

export const resumeApi = {
  list: () => request<{ items: ResumeItem[]; total: number }>({ url: '/office/resumes' }),
  upload: (file: File, title?: string, description?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (title) fd.append('title', title)
    if (description) fd.append('description', description)
    return request<ResumeItem>({
      url: '/office/resumes',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
      skipErrorToast: true,
    })
  },
  detail: (id: number) => request<ResumeDetail>({ url: `/office/resumes/${id}` }),
  preview: (id: number) =>
    request<ResumePreview>({ url: `/office/resumes/${id}/preview`, skipErrorToast: true }),
  update: (id: number, payload: { title?: string; description?: string }) =>
    request<ResumeItem>({ url: `/office/resumes/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/office/resumes/${id}`, method: 'DELETE' }),
  analyze: (id: number) =>
    request<ResumeItem>({
      url: `/office/resumes/${id}/analyze`,
      method: 'POST',
      skipErrorToast: true,
    }),
  // responseType:'blob' 时响应拦截器原样返回整个 AxiosResponse；出错时后端
  // 给的是 JSON 信封，由 saveBlobResponse 识别并弹消息。
  download: (id: number) =>
    request<AxiosResponse<Blob>>({
      url: `/office/resumes/${id}/download`,
      responseType: 'blob',
      timeout: 0,
      skipErrorToast: true,
    }),

  compare: (resumeIds: number[], title?: string) =>
    request<ResumeComparisonItem>({
      url: '/office/comparisons',
      method: 'POST',
      data: { resume_ids: resumeIds, title: title || undefined },
      skipErrorToast: true,
    }),
  listComparisons: () =>
    request<{ items: ResumeComparisonItem[]; total: number }>({ url: '/office/comparisons' }),
  comparisonDetail: (id: number) =>
    request<ResumeComparisonDetail>({ url: `/office/comparisons/${id}` }),
  removeComparison: (id: number) =>
    request<null>({ url: `/office/comparisons/${id}`, method: 'DELETE' }),

  createToolkitTask: (payload: ResumeToolkitCreate) =>
    request<ResumeToolkitItem>({
      url: '/office/toolkit',
      method: 'POST',
      data: payload,
      skipErrorToast: true,
    }),
  listToolkitTasks: () =>
    request<{ items: ResumeToolkitItem[]; total: number }>({ url: '/office/toolkit' }),
  toolkitTaskDetail: (id: number) =>
    request<ResumeToolkitDetail>({ url: `/office/toolkit/${id}` }),
  removeToolkitTask: (id: number) =>
    request<null>({ url: `/office/toolkit/${id}`, method: 'DELETE' }),
}
