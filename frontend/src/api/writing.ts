/**
 * 智能应用 · 创作（写小说 / 写文章）。
 * 信封协议同 notes.ts；列表行不带正文，详情才回大纲树与全部正文块。
 */
import type { AxiosResponse } from 'axios'
import { request } from './http'

// ---- 类型（与后端 schemas 同形）-------------------------------------------------

export type WritingKind = 'novel' | 'article'
export type WritingNodeKind = 'volume' | 'chapter' | 'section'
export type WritingExportFormat = 'md' | 'txt'

export interface OutlineNode {
  cid: string
  kind: WritingNodeKind | string
  title: string
  synopsis: string
  target_words: number | null
  children?: OutlineNode[]
}

/** 提交给后端的大纲节点：新节点还没有 cid，服务端清洗时发放。 */
export interface OutlineNodeInput {
  cid?: string | null
  kind: string
  title?: string
  synopsis?: string
  target_words?: number | null
  children?: OutlineNodeInput[] | null
}

/**
 * 正文块状态同样由最近一次 writing-part 运行翻译：
 * idle=没跑过、analyzing=该块 AI 生成中、ready=已有 AI 产物、error=最近一次运行失败。
 */
export type WritingPartStatus = 'idle' | 'analyzing' | 'ready' | 'error'

export interface WritingPart {
  cid: string
  sort_order: number
  title: string
  content: string
  word_count: number
  status: WritingPartStatus
  error_msg: string | null
  created_at: string
  updated_at: string
}

/** 七个正文动作：小说全量支持；文章段落只有五个编辑动作，整篇起草走作品级流程。 */
export type WritingPartAction =
  | 'draft'
  | 'continue'
  | 'rewrite'
  | 'polish'
  | 'expand'
  | 'condense'
  | 'retone'

export const NOVEL_ACTIONS: WritingPartAction[] = [
  'draft',
  'continue',
  'rewrite',
  'polish',
  'expand',
  'condense',
  'retone',
]

export const ARTICLE_ACTIONS: WritingPartAction[] = [
  'rewrite',
  'polish',
  'expand',
  'condense',
  'retone',
]

/** 编辑类动作要求块里已有正文；起草/续写是空块也能发起的写手动作。 */
export const WRITER_ACTIONS: WritingPartAction[] = ['draft', 'continue']

export const ACTION_LABELS: Record<WritingPartAction, string> = {
  draft: '起草',
  continue: '续写',
  rewrite: '重写',
  polish: '润色',
  expand: '扩写',
  condense: '缩写',
  retone: '换语气',
}

export interface WritingPartGeneratePayload {
  action: WritingPartAction
  instruction?: string | null
  tone?: string | null
  model_config_id?: number | null
}

export interface WritingDraftGeneratePayload {
  model_config_id?: number | null
}

/**
 * 作品状态不在业务行上存储，由后端把最近一次 workflow_run 翻译过来：
 * uploaded=还没生成过、analyzing=后台生成中、ready=有大纲产物、error=最近一次运行失败。
 */
export type WritingWorkStatus = 'uploaded' | 'analyzing' | 'ready' | 'error'

export interface WritingWorkItem {
  id: number
  kind: WritingKind
  title: string
  params: Record<string, string | number>
  part_count: number
  word_count: number
  status: WritingWorkStatus
  error_msg: string | null
  created_at: string
  updated_at: string
}

/** 大纲生成请求：requirement 不传就用作品已存的补充要求；模型不传走默认对话模型。 */
export interface WritingOutlineGeneratePayload {
  requirement?: string | null
  model_config_id?: number | null
}

export interface WritingWorkDetail extends WritingWorkItem {
  brief: string
  requirement: string | null
  outline: OutlineNode[]
  meta: Record<string, unknown>
  parts: WritingPart[]
}

export interface WritingWorkCreate {
  kind: WritingKind
  title: string
  brief?: string
  requirement?: string
  params?: Record<string, string | number>
}

export type WritingWorkUpdate = Partial<Omit<WritingWorkCreate, 'kind'>>

// ---- 创作参数表单元数据（白名单与后端 outline.PARAM_KEYS 对齐）-------------------

export interface ParamFieldMeta {
  key: string
  label: string
  placeholder?: string
  /** select 选项；不给就是自由文本输入。 */
  options?: string[]
}

export const ARTICLE_TYPES = ['公众号', '知乎', '小红书', '技术博客', '公文', '软文']

export const NOVEL_PARAM_FIELDS: ParamFieldMeta[] = [
  { key: 'genre', label: '题材', placeholder: '如：玄幻 / 都市 / 科幻' },
  { key: 'style', label: '风格', placeholder: '如：硬核 / 轻松 / 暗黑' },
  { key: 'tone', label: '基调', placeholder: '如：热血 / 治愈 / 悬疑' },
  { key: 'viewpoint', label: '视角', placeholder: '如：第一人称 / 第三人称限知' },
  { key: 'lead', label: '主角', placeholder: '一句话主角设定' },
  { key: 'worldview', label: '世界观', placeholder: '世界规则与背景' },
]

export const ARTICLE_PARAM_FIELDS: ParamFieldMeta[] = [
  { key: 'article_type', label: '文章类型', options: ARTICLE_TYPES },
  { key: 'platform', label: '发布平台', placeholder: '如：公众号 / 知乎专栏' },
  { key: 'audience', label: '目标受众', placeholder: '如：工作三年的产品经理' },
  { key: 'tone', label: '语气', placeholder: '如：专业克制 / 犀利幽默' },
  { key: 'viewpoint', label: '视角', placeholder: '如：从业者第一人称' },
  { key: 'core_points', label: '核心观点', placeholder: '想让读者记住的两三句话' },
]

// ---- 接口 ---------------------------------------------------------------------

export const writingApi = {
  list: (kind?: WritingKind, keyword?: string) =>
    request<{ items: WritingWorkItem[]; total: number }>({
      url: '/studio/works',
      params: { kind, keyword: keyword || undefined },
    }),
  create: (payload: WritingWorkCreate) =>
    request<WritingWorkDetail>({ url: '/studio/works', method: 'POST', data: payload }),
  detail: (id: number) => request<WritingWorkDetail>({ url: `/studio/works/${id}` }),
  update: (id: number, payload: WritingWorkUpdate) =>
    request<WritingWorkDetail>({ url: `/studio/works/${id}`, method: 'PUT', data: payload }),
  remove: (id: number) =>
    request<null>({ url: `/studio/works/${id}`, method: 'DELETE' }),

  /** 后台生成大纲：响应回来时作品已是 analyzing，进度靠列表/工作台轮询跟随。 */
  generateOutline: (id: number, payload: WritingOutlineGeneratePayload = {}) =>
    request<WritingWorkItem>({
      url: `/studio/works/${id}/outline/generate`,
      method: 'POST',
      data: payload,
    }),
  saveOutline: (id: number, nodes: OutlineNodeInput[]) =>
    request<WritingWorkDetail>({
      url: `/studio/works/${id}/outline`,
      method: 'PUT',
      data: { nodes },
    }),
  addPart: (id: number, title = '', afterCid?: string | null) =>
    request<WritingWorkDetail>({
      url: `/studio/works/${id}/parts`,
      method: 'POST',
      data: { title, after_cid: afterCid ?? null },
    }),
  savePart: (id: number, cid: string, payload: { title?: string; content?: string }) =>
    request<WritingWorkDetail>({
      url: `/studio/works/${id}/parts/${cid}`,
      method: 'PUT',
      data: payload,
    }),
  deletePart: (id: number, cid: string) =>
    request<WritingWorkDetail>({
      url: `/studio/works/${id}/parts/${cid}`,
      method: 'DELETE',
    }),

  /** 单块 AI 生成（七个动作之一）：响应是排队瞬间的详情快照，进度靠轮询跟随。 */
  generatePart: (id: number, cid: string, payload: WritingPartGeneratePayload) =>
    request<WritingWorkDetail>({
      url: `/studio/works/${id}/parts/${cid}/generate`,
      method: 'POST',
      data: payload,
    }),

  /** 文章整篇起草：作品级运行，成稿按二级标题切回各段落。 */
  generateDraft: (id: number, payload: WritingDraftGeneratePayload = {}) =>
    request<WritingWorkItem>({
      url: `/studio/works/${id}/draft/generate`,
      method: 'POST',
      data: payload,
    }),

  exportWork: (id: number, format: WritingExportFormat) =>
    request<AxiosResponse<Blob>>({
      url: `/studio/works/${id}/export`,
      params: { format },
      responseType: 'blob',
      timeout: 60000,
      skipErrorToast: true,
    }),
}
