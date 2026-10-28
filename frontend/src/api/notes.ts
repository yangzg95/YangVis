/**
 * 求职笔记域：简历页「笔记」tab 的个人记录本。
 * 列表只给摘要，正文走详情；与面试记录同一套 owner 隔离与信封约定。
 */
import { request } from './http'

// ---- 智能应用 · 求职笔记 ---------------------------------------------------------

export interface NoteItem {
  id: number
  title: string
  /** 折叠空白后的正文摘要，空正文为 null。 */
  snippet: string | null
  created_at: string
  updated_at: string
}

export interface NoteDetail extends NoteItem {
  /** markdown 纯文本。 */
  content: string | null
}

export interface NoteCreate {
  title: string
  content?: string
}

export type NoteUpdate = Partial<NoteCreate>

export const noteApi = {
  list: (keyword?: string) =>
    request<{ items: NoteItem[]; total: number }>({
      url: '/office/notes',
      params: keyword ? { keyword } : undefined,
    }),
  create: (payload: NoteCreate) =>
    request<NoteDetail>({ url: '/office/notes', method: 'POST', data: payload }),
  detail: (id: number) => request<NoteDetail>({ url: `/office/notes/${id}` }),
  update: (id: number, payload: NoteUpdate) =>
    request<NoteDetail>({ url: `/office/notes/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) => request<null>({ url: `/office/notes/${id}`, method: 'DELETE' }),
}
