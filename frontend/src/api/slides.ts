/**
 * 幻灯片域： deck 的创建、保存、重新生成、HTML 导出与配图素材。
 * 主题、版式与比例的字面量类型由工作台与放映页共用。
 */
import type { AxiosResponse } from 'axios'
import { request } from './http'

// ---- 智能应用 · 幻灯片 -------------------------------------------------------

export type SlideStatus = 'analyzing' | 'ready' | 'error'
/** 页面版式；每个版式对应后端渲染器的一个分支，别在这里造新值。 */
export type SlideLayout =
  | 'cover'
  | 'section'
  | 'bullets'
  | 'image'
  | 'quote'
  | 'closing'
  | 'table'
  | 'steps'
  | 'flow'
  | 'arch'
/** amber（白底橙强调）是后端默认预设。 */
export type SlidePreset = 'amber' | 'ink' | 'teal' | 'paper' | 'violet'
export type SlideRatio = '16x9' | '4x3'

export interface SlideTheme {
  preset: SlidePreset
  ratio: SlideRatio
  /** 自定义强调色（#rgb / #rrggbb）；非白名单值会被后端回落成预设色。 */
  accent: string | null
}

/** 对比表：列名与行，行数/格长超限时由后端裁。 */
export interface SlideTable {
  columns: string[]
  rows: string[][]
}

/** 步骤条里的一步，``desc`` 可以空。 */
export interface SlideStep {
  title: string
  desc: string
}

/** 链路图里的一格。``via`` 是走到下一格的条件，画在右侧箭头上；最后一格没有箭头。 */
export interface SlideFlowNode {
  title: string
  desc: string
  via: string
}

/** 分层图的一层：``blocks`` 是层内模块名，数组顺序就是页面上自上而下的层序。 */
export interface SlideArchLayer {
  name: string
  blocks: string[]
}

/** 页面的可编辑部分。新建时不填 sid，由服务端发放。 */
export interface SlidePageInput {
  layout: SlideLayout
  /** 页头的小类别标签（「核心概念」这类），空时渲染器退回章节序号。 */
  kicker?: string | null
  title?: string | null
  subtitle?: string | null
  /** 文本里的 ``**关键词**`` 会渲染成加粗强调，是内容的一部分。 */
  bullets?: string[]
  table?: SlideTable
  steps?: SlideStep[]
  /** 链路图的格子，2-5 格；不足两格时后端把这页降级成要点页。 */
  flow?: SlideFlowNode[]
  /** 分层图的层，2-5 层。 */
  layers?: SlideArchLayer[]
  /** 贴在页面底部的一句话结论。 */
  takeaway?: string | null
  asset_id?: string | null
  notes?: string | null
}

export interface SlidePage extends SlidePageInput {
  sid: string
}

export interface SlideAsset {
  id: string
  name: string
  mime: string
  size: number
  /** 相对 UPLOAD_MEDIA_DIR 的路径，前端只用它认身份，取地址走 url。 */
  rel_path: string
  /** 浏览器可直接浏览的站内路径（/uploads/…）。 */
  url: string
}

export interface SlideDeckItem {
  id: number
  title: string
  description: string | null
  status: SlideStatus
  error_msg: string | null
  page_count: number
  asset_count: number
  created_at: string
  updated_at: string
}

export interface SlideDeckDetail extends SlideDeckItem {
  requirement: string | null
  source_text: string
  theme: SlideTheme
  slides: SlidePage[]
  assets: SlideAsset[]
}

export interface SlideDeckCreate {
  title: string
  description?: string
  requirement?: string
  /** 直接粘贴的文案，与文档文件同为材料。 */
  text?: string
  files?: File[]
}

export interface SlideDeckSavePayload {
  title?: string
  description?: string
  theme?: SlideTheme
  /** 整组页面（含新增页与重排）；不带表示不改页面。 */
  slides?: SlidePageInput[]
}

export const slidesApi = {
  list: () => request<{ items: SlideDeckItem[]; total: number }>({ url: '/office/slides' }),
  create: (payload: SlideDeckCreate) => {
    const fd = new FormData()
    fd.append('title', payload.title)
    if (payload.description) fd.append('description', payload.description)
    if (payload.requirement) fd.append('requirement', payload.requirement)
    if (payload.text) fd.append('text', payload.text)
    for (const file of payload.files ?? []) fd.append('files', file)
    return request<SlideDeckItem>({
      url: '/office/slides',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      // 多份文档的解析是服务端同步做的，比单文件上传慢。
      timeout: 120000,
      skipErrorToast: true,
    })
  },
  detail: (id: number) => request<SlideDeckDetail>({ url: `/office/slides/${id}` }),
  /** 保存返回 Detail：新页的 sid 由服务端发放，前端要用规范化后的列表对齐自己那份。 */
  save: (id: number, payload: SlideDeckSavePayload) =>
    request<SlideDeckDetail>({
      url: `/office/slides/${id}`,
      method: 'PUT',
      data: payload,
      skipErrorToast: true,
    }),
  remove: (id: number) => request<null>({ url: `/office/slides/${id}`, method: 'DELETE' }),
  regenerate: (id: number, requirement?: string) =>
    request<SlideDeckItem>({
      url: `/office/slides/${id}/generate`,
      method: 'POST',
      data: { requirement: requirement || null },
      skipErrorToast: true,
    }),
  // 渲染好的 HTML 文档。用 axios 取文本再交给 iframe.srcdoc：iframe 自己发不了
  // 带 token 的请求，直连 URL 会被登录守卫拦下。
  html: (id: number) =>
    request<string>({
      url: `/office/slides/${id}/html`,
      responseType: 'text',
      timeout: 60000,
      skipErrorToast: true,
    }),
  exportHtml: (id: number) =>
    request<AxiosResponse<Blob>>({
      url: `/office/slides/${id}/export`,
      responseType: 'blob',
      timeout: 0,
      skipErrorToast: true,
    }),
  uploadAsset: (id: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<SlideDeckDetail>({
      url: `/office/slides/${id}/assets`,
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 60000,
      skipErrorToast: true,
    })
  },
  removeAsset: (id: number, assetId: string) =>
    request<SlideDeckDetail>({
      url: `/office/slides/${id}/assets/${assetId}`,
      method: 'DELETE',
      skipErrorToast: true,
    }),
}
