/**
 * 素材库：账号级的图片/视频素材，跨项目复用。
 *
 * 与短视频的「项目内参考图」分界线在归属：库里的条目不属于任何项目，删一条片子不会
 * 带走人物形象图，反过来删一条素材要把引用它的镜头一起解掉（后端做，前端只管刷新）。
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
  mime: string
  size: number
  /** 站内可浏览地址；文件被挪走时为空串，按「素材已丢失」呈现。 */
  url: string
  created_at: string | null
}

export interface MaterialList {
  items: MaterialAsset[]
  /** 跟着筛选走的天数，分页按它算。 */
  total: number
  /** 全库占用字节，与当前筛出的是哪几条无关。 */
  bytes_total: number
  max_per_owner: number
}

export const materialsApi = {
  /** ``kind`` 传空表示全部；``q`` 按名字里搜，通配符按字面量处理。 */
  list: (params: { kind?: MaterialKind; q?: string; limit?: number; offset?: number } = {}) =>
    request<MaterialList>({ url: '/office/materials', params }),
  upload: (file: File) => {
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
    })
  },
  remove: (id: number) =>
    request<null>({ url: `/office/materials/${id}`, method: 'DELETE', skipErrorToast: true }),
}
