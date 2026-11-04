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
  /** upload（上传文档）| repo（仓库同步的源文件，不可单删/替换）。 */
  source: 'upload' | 'repo' | string
  repo_id?: number | null
  language?: string | null
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
  // 代码分片才有：来源、仓库、语言、行号区间、符号。散文分片缺省。
  source: 'upload' | 'repo' | string
  repo_id?: number | null
  language?: string | null
  line_start?: number | null
  line_end?: number | null
  symbol?: string | null
}

export interface RebuildResult {
  started: boolean
  message: string
  doc_count: number
}

// ---- 代码仓库 --------------------------------------------------------------

export type RepoAuthType = 'ssh_key' | 'none'
export type RepoStatus = 'pending' | 'syncing' | 'ready' | 'error'

export interface RepoSyncResult {
  added: number
  changed: number
  removed: number
  skipped: number
  indexed_chunks: number
  duration_ms: number
  commit?: string | null
}

export interface RepoItem {
  id: number
  type_id: number
  project_id: number
  name: string
  auth_type: RepoAuthType | string
  remote_url: string
  branch?: string | null
  has_credential: boolean
  /** 仅接入后首次详情/列表里随仓库带出一次的公钥，便于补登记到代码平台。 */
  public_key?: string | null
  host_fingerprint?: string | null
  exclude_globs?: string | null
  status: RepoStatus | string
  error_msg?: string | null
  head_commit?: string | null
  last_sync_at?: string | null
  last_sync_result?: RepoSyncResult | null
  created_at: string
  updated_at: string
}

export interface RepoKeypair {
  public_key: string
  private_key: string
}

export interface RepoTestConnectionResult {
  default_branch?: string | null
  head_sha: string
  host: string
  host_fingerprint: string
  /** TOFU 记录，创建仓库时原样回传，前端不要解析也不要展示全文。 */
  known_hosts: string
}

export interface RepoSyncResponse {
  started: boolean
  message: string
  already_running: boolean
}

export interface RepoCreatePayload {
  project_id: number
  remote_url: string
  branch?: string | null
  auth_type?: RepoAuthType | string
  private_key?: string | null
  known_hosts?: string | null
  host_fingerprint?: string | null
  name?: string | null
  exclude_globs?: string | null
}

export interface RepoUpdatePayload {
  name?: string
  branch?: string | null
  exclude_globs?: string | null
  /** 缺省=不变；显式空串配合 auth_type='none' 清空私钥。 */
  private_key?: string
  auth_type?: RepoAuthType | string
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

/**
 * 代码仓库接口单独成组：仓库与知识类型 1:1，文档由同步任务物化，不经过上传/单删。
 * 业务失败（地址不合法、指纹冲突等）后端统一用 code=-1 + message 返回，
 * 所以写操作都开 skipErrorToast，由调用方把 message 落到表单内联错误。
 */
export const repoApi = {
  generateKeypair: () =>
    request<RepoKeypair>({ url: '/knowledge/repos/keypair', method: 'POST' }),
  testConnection: (payload: {
    remote_url: string
    branch?: string | null
    private_key?: string | null
  }) =>
    request<RepoTestConnectionResult>({
      url: '/knowledge/test-connection',
      method: 'POST',
      data: payload,
      timeout: 60000,
      skipErrorToast: true,
    }),
  list: (projectId: number) =>
    request<{ items: RepoItem[]; total: number }>({
      url: '/knowledge/repos',
      params: { project_id: projectId },
    }),
  create: (payload: RepoCreatePayload) =>
    request<RepoItem>({
      url: '/knowledge/repos',
      method: 'POST',
      data: payload,
      skipErrorToast: true,
    }),
  update: (repoId: number, payload: RepoUpdatePayload) =>
    request<RepoItem>({
      url: `/knowledge/repos/${repoId}`,
      method: 'PUT',
      data: payload,
      skipErrorToast: true,
    }),
  sync: (repoId: number) =>
    request<RepoSyncResponse>({
      url: `/knowledge/repos/${repoId}/sync`,
      method: 'POST',
      skipErrorToast: true,
    }),
  retrust: (repoId: number) =>
    request<RepoItem>({
      url: `/knowledge/repos/${repoId}/retrust`,
      method: 'POST',
      timeout: 60000,
      skipErrorToast: true,
    }),
  remove: (repoId: number) =>
    request<null>({ url: `/knowledge/repos/${repoId}`, method: 'DELETE' }),
}
