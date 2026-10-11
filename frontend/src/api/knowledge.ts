/**
 * 知识库域：项目、内容类型、文档的上传与重建索引、索引状态、向量检索。
 * SPA 要分支处理的三个业务错误码与上传扩展名白名单也留在这里，跟各自的接口挨着。
 */
import { request } from './http'

// ---- 知识库 ----------------------------------------------------------------

/** SPA 会专门处理而不是直接弹提示的业务错误码。 */
export const CODE_EMBEDDING_NOT_READY = 4001
export const CODE_INDEX_MODEL_MISMATCH = 4002
export const CODE_CHAT_NOT_READY = 4004

export interface KbProject {
  id: number
  name: string
  description?: string | null
  type_count: number
  document_count: number
  created_at: string
  updated_at: string
}

export interface KnowledgeType {
  id: number
  name: string
  description?: string | null
  project_id: number
  document_count: number
  created_at: string
  updated_at: string
}

export type DocumentStatus = 'pending' | 'indexing' | 'ready' | 'error'

export interface KnowledgeDocument {
  id: number
  name: string
  type_id: number | null
  size: number
  mime?: string | null
  status: DocumentStatus
  error_msg?: string | null
  chunk_count: number
  created_at: string
  updated_at: string
}

export interface IndexState {
  status: 'uninitialized' | 'indexing' | 'ready' | 'rebuilding' | 'error'
  collection_name: string | null
  vector_size: number
  doc_count: number
  chunk_count: number
  error_msg?: string | null
  /** 当前配置的向量模型，已经和建立索引时用的那个对不上了。 */
  model_mismatch: boolean
  updated_at: string | null
}

export interface SearchHit {
  chunk_id: number
  doc_id: number
  filename: string
  seq: number
  score: number
  content: string
}

export interface RebuildResult {
  started: boolean
  message: string
  doc_count: number
}

/** 保持可读性而不用引入解析依赖；和 chunking.py 一致。 */
export const SUPPORTED_UPLOAD_EXTENSIONS = [
  '.txt',
  '.md',
  '.markdown',
  '.csv',
  '.json',
  '.log',
  '.yaml',
  '.yml',
]

export const knowledgeApi = {
  /** ---- 项目 ---- */
  listProjects: () => request<{ items: KbProject[]; total: number }>({ url: '/knowledge/projects' }),
  createProject: (payload: { name: string; description?: string }) =>
    request<KbProject>({ url: '/knowledge/projects', method: 'POST', data: payload }),
  updateProject: (id: number, payload: { name?: string; description?: string }) =>
    request<KbProject>({ url: `/knowledge/projects/${id}`, method: 'PUT', data: payload }),
  deleteProject: (id: number) =>
    request<null>({ url: `/knowledge/projects/${id}`, method: 'DELETE' }),

  /** ---- 知识类型 ---- */
  listTypes: (projectId: number) =>
    request<{ items: KnowledgeType[]; total: number }>({
      url: '/knowledge/types',
      params: { project_id: projectId },
    }),
  createType: (payload: { name: string; project_id: number; description?: string }) =>
    request<KnowledgeType>({ url: '/knowledge/types', method: 'POST', data: payload }),
  updateType: (id: number, payload: { name?: string; description?: string }) =>
    request<KnowledgeType>({ url: `/knowledge/types/${id}`, method: 'PUT', data: payload }),
  deleteType: (id: number) =>
    request<null>({ url: `/knowledge/types/${id}`, method: 'DELETE' }),

  /** ---- 文档 ---- */
  listDocuments: (typeId: number, page?: number, pageSize?: number) =>
    request<{ items: KnowledgeDocument[]; total: number }>({
      url: `/knowledge/types/${typeId}/documents`,
      // 不传分页参数时后端全量返回（向后兼容）。
      params: page !== undefined && pageSize !== undefined ? { page, page_size: pageSize } : undefined,
    }),
  uploadDocument: (typeId: number, file: File, name?: string) => {
    const fd = new FormData()
    fd.append('file', file)
    if (name) fd.append('name', name)
    return request<KnowledgeDocument>({
      url: `/knowledge/types/${typeId}/documents`,
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      // 这里只负责存下文本；切片与向量化都放在后台跑。
      timeout: 60000,
      skipErrorToast: true,
    })
  },
  deleteDocument: (id: number) =>
    request<null>({ url: `/knowledge/documents/${id}`, method: 'DELETE' }),
  reindexDocument: (id: number) =>
    request<KnowledgeDocument>({
      url: `/knowledge/documents/${id}/reindex`,
      method: 'POST',
      skipErrorToast: true,
    }),
  batchDeleteDocuments: (ids: number[]) =>
    request<{ requested: number; done: number }>({
      url: '/knowledge/documents/batch-delete',
      method: 'POST',
      data: { ids },
    }),
  batchReindexDocuments: (ids: number[]) =>
    request<{ requested: number; done: number }>({
      url: '/knowledge/documents/batch-reindex',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),

  /** ---- 索引 ---- */
  indexState: () => request<IndexState>({ url: '/knowledge/index/state' }),
  rebuildIndex: () =>
    request<RebuildResult>({
      url: '/knowledge/index/rebuild',
      method: 'POST',
      skipErrorToast: true,
    }),

  /** ---- 检索 ---- */
  search: (payload: {
    query: string
    top_k?: number
    project_id?: number
    type_ids?: number[]
    score_threshold?: number
  }) =>
    request<{ hits: SearchHit[]; total: number }>({
      url: '/knowledge/search',
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
}
