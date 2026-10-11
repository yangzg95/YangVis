/** 长期记忆域：单条记忆的增删改与整库清空。 */
import { request } from './http'

// ---- 长期记忆 ----------------------------------------------------------------

export interface MemoryItem {
  id: number
  content: string
  source_conversation_id: number | null
  created_at: string
  updated_at: string
}

export const memoryApi = {
  list: () => request<{ items: MemoryItem[]; total: number }>({ url: '/memory' }),
  create: (content: string) =>
    request<MemoryItem>({ url: '/memory', method: 'POST', data: { content } }),
  update: (id: number, content: string) =>
    request<MemoryItem>({ url: `/memory/${id}`, method: 'PUT', data: { content } }),
  remove: (id: number) => request<null>({ url: `/memory/${id}`, method: 'DELETE' }),
  clear: () => request<null>({ url: '/memory', method: 'DELETE' }),
}
