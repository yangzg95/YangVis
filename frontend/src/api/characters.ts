/**
 * 人物库：账号级的角色档案（人设/衣着/外形特色）+ 专属形象图。
 *
 * 与素材库的分工：素材是无结构的图/视频文件，人物是带档案的角色。一条短视频通过
 * 项目角色组（video_project.character_ids）认领人物：脚本生成时档案会拼进提示词，
 * 渲染时形象图自动并进每镜的参考图请求（角色图在前，与镜头素材合计最多 4 张）。
 *
 * 删除是硬删除：项目里残留的 id 由后端在保存/渲染时按「已不存在」跳过，与素材文件
 * 丢失同一降级口径；删除回执里的 references 仅作界面提示。
 */
import { request } from './http'

/**
 * 一条短视频最多选几个角色进项目角色组，与后端 config.CHARACTER_MAX_PER_PROJECT
 * 同口径：角色形象图要并进每镜的参考图请求（总预算 4 张），再多只会互相干扰。
 */
export const MAX_CHARACTERS_PER_PROJECT = 3

export interface Character {
  id: number
  name: string
  /** 人设：身份、性格、背景、说话方式。 */
  persona: string
  /** 衣着：固定穿搭。 */
  outfit: string
  /** 外形特色：发型、体型、标志物等视觉特征。 */
  features: string
  mime: string
  size: number
  /** 站内可浏览地址；文件被挪走时为空串，按「形象图已丢失」呈现。 */
  url: string
  /** 列表缩略图（320px WebP）；没有时为空串，界面回退到 url。 */
  thumb_url: string
  created_at: string | null
}

export interface CharacterList {
  items: Character[]
  total: number
  max_per_owner: number
}

/** 建档/换图共用的 multipart 字段；形象图只收图片，视频会被后端拒绝。 */
export interface CharacterFormPayload {
  name: string
  persona?: string
  outfit?: string
  features?: string
}

export interface CharacterUsageProject {
  project_id: number
  title: string
}

export interface CharacterUsage {
  character_id: number
  projects: CharacterUsageProject[]
}

export interface CharacterDeleteResult {
  id: number
  /** 仍引用该角色的短视频项目数，仅用于删除提示。 */
  references: number
}

export const charactersApi = {
  /** ``q`` 只搜角色名。 */
  list: (params: { q?: string; limit?: number; offset?: number } = {}) =>
    request<CharacterList>({ url: '/office/characters', params }),
  /** 建档：multipart 一次收形象图与文字档案，魔数判型在服务端写盘之前。 */
  create: (file: File, form: CharacterFormPayload) => {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('name', form.name)
    if (form.persona) fd.append('persona', form.persona)
    if (form.outfit) fd.append('outfit', form.outfit)
    if (form.features) fd.append('features', form.features)
    return request<Character>({
      url: '/office/characters',
      method: 'POST',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
      skipErrorToast: true,
    })
  },
  /** 改文字档案，不动形象图；字段给了才改，name 给了不能是空白。 */
  update: (id: number, payload: Partial<Omit<CharacterFormPayload, 'name'>> & { name?: string }) =>
    request<Character>({
      url: `/office/characters/${id}`,
      method: 'PUT',
      data: payload,
      skipErrorToast: true,
    }),
  /** 换形象图：旧图在服务端 best-effort 删除。 */
  replacePortrait: (id: number, file: File) => {
    const fd = new FormData()
    fd.append('file', file)
    return request<Character>({
      url: `/office/characters/${id}/portrait`,
      method: 'PUT',
      data: fd,
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
      skipErrorToast: true,
    })
  },
  /** 删除前的影响面：被哪些短视频项目选进了角色组。 */
  usage: (ids: number[]) =>
    request<CharacterUsage[]>({
      url: '/office/characters/usage',
      method: 'POST',
      data: { ids },
      skipErrorToast: true,
    }),
  /** 硬删除：删行 + 删形象图，残留引用由视频侧自然跳过。 */
  remove: (id: number) =>
    request<CharacterDeleteResult>({
      url: `/office/characters/${id}`,
      method: 'DELETE',
      skipErrorToast: true,
    }),
}
