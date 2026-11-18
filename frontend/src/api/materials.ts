/**
 * 素材库：账号级的图片/视频素材，跨项目复用。
 *
 * 与短视频的「项目内参考图」分界线在归属：库里的条目不属于任何项目，删一条片子不会
 * 带走人物形象图。删除先进回收期（默认 10 分钟），窗口内镜头引用与文件都原样保留，
 * 过期后后端才删文件、解引用；前端只管刷新与刷新时带上「撤销」。
 *
 * 视频素材目前**只是存下来并能被镜头引用**：这一期渲染只到首帧，参考视频要发给上游
 * 还得有公网可达的地址。界面文案因此不说「已用上」，只说「已记录」。
 */
import { request } from './http'

export type MaterialKind = 'image' | 'video'

export interface MaterialAsset {
  id: number
  kind: MaterialKind
  name: string
  /** 打标分组：从短视频存进来的首帧自动打上项目名；null = 未打标（按时间分组展示）。 */
  tag: string | null
  mime: string
  size: number
  /** 站内可浏览地址；文件被挪走时为空串，按「素材已丢失」呈现。 */
  url: string
  /** 列表缩略图（320px WebP）；没有时为空串，回退到 url。 */
  thumb_url: string
  created_at: string | null
}

export interface MaterialList {
  items: MaterialAsset[]
  /** 跟着筛选走的天数，分页按它算。 */
  total: number
  /** 全库占用字节，与当前筛出的是哪几条无关。 */
  bytes_total: number
  max_per_owner: number
  /** 删除后的撤销窗口（秒）：界面照它写「N 分钟内可撤销」。 */
  trash_ttl_seconds: number
}

export interface MaterialUsageShot {
  id: number
  sort_order: number
}

export interface MaterialUsageProject {
  project_id: number
  title: string
  shots: MaterialUsageShot[]
}

/** 一条素材被哪些项目的哪些镜头引用，删除前给用户看的影响面。 */
export interface MaterialUsage {
  asset_id: number
  projects: MaterialUsageProject[]
}

export interface MaterialBatchDelete {
  deleted: number
  /** 仍在引用这些素材的镜头数：回收期后才解绑。 */
  references: number
  /** 撤销窗口（秒），提示里的「撤销」按钮存活时长按它来。 */
  undo_seconds: number
}

export const materialsApi = {
  /** ``kind`` 传空表示全部；``q`` 同时搜名字与打标分组，通配符按字面量处理。 */
  list: (params: { kind?: MaterialKind; q?: string; limit?: number; offset?: number } = {}) =>
    request<MaterialList>({ url: '/office/materials', params }),
  upload: (file: File, onProgress?: (percent: number) => void) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<MaterialAsset>({
      url: '/office/materials',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      // 视频素材按几十 MB 走，30 秒的默认超时在上传路上是会断的。
      timeout: 120000,
      skipErrorToast: true,
      onUploadProgress: (event) => {
        if (!onProgress) return
        const ratio = event.progress ?? (event.total ? event.loaded / event.total : 0)
        onProgress(Math.round(Math.min(1, Math.max(0, ratio)) * 100))
      },
    })
  },
  /** 给素材改名；空白名字后端会拒。 */
  rename: (id: number, name: string) =>
    request<MaterialAsset>({
      url: `/office/materials/${id}/name`,
      method: 'PUT',
      data: { name },
      skipErrorToast: true,
    }),
  /** 改打标分组；空串表示摘掉标签，归回「按时间分组」。 */
  setTag: (id: number, tag: string) =>
    request<MaterialAsset>({
      url: `/office/materials/${id}/tag`,
      method: 'PUT',
      data: { tag },
      skipErrorToast: true,
    }),
  /** 一批素材一次改成同一个分组，整组改名不再逐条发请求。 */
  batchSetTag: (ids: number[], tag: string) =>
    request<{ items: { id: number; tag: string | null }[] }>({
      url: '/office/materials/batch-tag',
      method: 'POST',
      data: { ids, tag },
      skipErrorToast: true,
    }),
  /** 删除前查影响面：被哪些项目的哪些镜头引用。 */
  usage: (ids: number[]) =>
    request<MaterialUsage[]>({
      url: '/office/materials/usage',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),
  /** 批量删除：先进回收期，窗口内可撤销。 */
  batchRemove: (ids: number[]) =>
    request<MaterialBatchDelete>({
      url: '/office/materials/batch-delete',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),
  /** 撤销删除：把还在窗口里的素材原样恢复。 */
  restore: (ids: number[]) =>
    request<{ restored: number }>({
      url: '/office/materials/batch-restore',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),
}
