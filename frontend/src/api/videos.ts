/**
 * 短视频域：项目的创建与保存、脚本（创作卡 + 分镜）的重新生成、首帧渲染与取消。
 * 参考素材不在这里——它在账号级的素材库（api/materials.ts），镜头上只存引用的 id。
 * 字面量类型与后端 ``models/schemas.py`` 的白名单一字不差——那些值原样进方舟的请求体，
 * 中间没有翻译层，这里写错一个字符就是上游 400。
 *
 * 两阶段各有进度来源：脚本阶段是工作流运行（``status``），渲染阶段是 ``media_job``
 * （``image_job.status``）。所以这一页既没有「等待结果」的接口，也不该有：入队之后
 * 唯一诚实的答案是「排队中」，出片要几十秒到几分钟，靠详情接口的轮询跟随。
 */
import { request } from './http'

// ---- 智能应用 · 短视频 -------------------------------------------------------

export type VideoPlatform = 'douyin' | 'kuaishou' | 'other'
/** 首帧流是默认档：一期只渲首帧，另外三档先攒在镜头行上。 */
export type VideoGenMode = 't2v' | 'first_frame' | 'first_last_frame' | 'reference'
export type VideoRatio = 'adaptive' | '21:9' | '16:9' | '4:3' | '1:1' | '3:4' | '9:16'
export type VideoResolution = '480p' | '720p' | '1080p'
export type VideoImageSize = '1K' | '2K' | '4K'
export type MediaJobKind = 'image' | 'video'
/** 渲染进度；downloading 是「上游已经出片，正在转存到本地磁盘」。 */
export type MediaJobStatus =
  | 'queued'
  | 'running'
  | 'downloading'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'expired'
/** 脚本阶段的状态，由认领本项目的运行翻译出来；uploaded 只出现在存量数据上。 */
export type VideoProjectStatus = 'uploaded' | 'analyzing' | 'ready' | 'error'

/** 创作卡：脚本流程第一步的产物，也就是「这条片子凭什么让人看完」。 */
export interface VideoBrief {
  hook_type: string
  hook: string
  audience: string
  goal: string
  /** 分几段讲，数组顺序就是段落顺序；最多 8 段。 */
  structure: string[]
  /** 策划师给这条片子定的时长，5-60 秒。 */
  duration_s: number
  risk_notes: string
}

/** 全片风格约束：逐镜提示词都要贴它，改一处比改十几镜更能保持统一。 */
export interface VideoStyle {
  visual_style: string
  persona: string
  palette: string
}

/** 渲染参数。发起作业时抄进 media_job.request，之后改这里不影响在跑的作业。 */
export interface VideoParams {
  ratio: VideoRatio
  resolution: VideoResolution
  fps: number
  watermark: boolean
  /** 默认关：方舟那一侧默认开，留着就会拿到一条模型自己编的人声。 */
  generate_audio: boolean
  image_size: VideoImageSize
}

/** 发布用的一包：标题候选、话题标签、封面描述。 */
export interface VideoPublish {
  title_candidates: string[]
  tags: string[]
  cover_prompt: string
}

/**
 * 一条媒体作业。``url`` 是服务端本地产物的站内地址：上游那条 24 小时就失效，
 * 「已出片」和「还看得见」靠的是这一列，不是上游 URL。
 */
export interface MediaJob {
  id: number
  kind: MediaJobKind
  status: MediaJobStatus
  model_name: string
  url: string
  mime: string | null
  file_size: number | null
  error_msg: string | null
  /** 从提交算到产物落盘，含上游排队时间。未跑完为 null。 */
  elapsed_ms: number | null
  created_at: string | null
  finished_at: string | null
}

/** 工作台里的一镜。一期只有 image_job，视频片段的作业接口下一轮才长。 */
export interface VideoShot {
  id: number
  sort_order: number
  /** 时间轴上的起点，由服务端按各镜时长累加重算，前端不自己算。 */
  start_s: number
  duration_s: number
  scene: string
  camera: string
  action: string
  dialogue: string
  on_screen_text: string
  image_prompt: string
  video_prompt: string
  gen_mode: VideoGenMode
  /** 素材库里的 id（字符串形式），最多 4 条；只有图片类会真的进请求体。 */
  material_ids: string[]
  image_job: MediaJob | null
}

/** 整批保存时的一行：不带 ``id`` 是新建，没列进来的现有镜头会被删掉。 */
export interface VideoShotInput {
  id?: number | null
  duration_s: number
  scene: string
  camera: string
  action: string
  dialogue: string
  on_screen_text: string
  image_prompt: string
  video_prompt: string
  gen_mode: VideoGenMode
  material_ids: string[]
}

export interface VideoProjectItem {
  id: number
  title: string
  platform: VideoPlatform
  status: VideoProjectStatus | string
  error_msg: string | null
  shot_count: number
  /** 全部镜头时长之和，秒。 */
  duration_s: number
  cover_url: string
  created_at: string
  updated_at: string
}

export interface VideoProjectDetail extends VideoProjectItem {
  idea: string
  requirement: string
  brief: VideoBrief
  /** 整条口播文案；这一期不配音，只作为内容与字幕的底稿。 */
  script: string
  style: VideoStyle
  params: VideoParams
  publish: VideoPublish
  shots: VideoShot[]
}

export interface VideoProjectCreate {
  title?: string
  platform: VideoPlatform
  idea: string
  /** 想直接指定成片时长就填这里，不填由策划师按选题定。 */
  duration_s?: number
  requirement?: string
  style?: VideoStyle
  params?: VideoParams
}

/** 整包保存：给了才改，undefined 保持原样。镜头走 saveShots，不塞在这里。 */
export interface VideoProjectSave {
  title?: string
  platform?: VideoPlatform
  idea?: string
  requirement?: string
  brief?: VideoBrief
  script?: string
  style?: VideoStyle
  params?: VideoParams
  publish?: VideoPublish
}

/** 一类作业的并发现状：渲染前那屏「现在有几个在跑、还能塞几个」。 */
export interface MediaCapacity {
  kind: MediaJobKind
  inflight: number
  limit: number
}

export interface MediaCapacityOut {
  jobs: MediaCapacity[]
  /** data_url 时首帧必须落在服务器本地；public_url 需要配公网基址。 */
  image_transport: string
}

export const videosApi = {
  list: () => request<{ items: VideoProjectItem[]; total: number }>({ url: '/office/videos' }),
  /** 选镜头之前就要显示的并发余量。 */
  capacity: () => request<MediaCapacityOut>({ url: '/office/videos/capacity' }),
  /** 创建即发起脚本生成：一次两阶段流程要几十秒，后端交给后台任务，这里不等。 */
  create: (payload: VideoProjectCreate) =>
    request<VideoProjectItem>({
      url: '/office/videos',
      method: 'POST',
      data: payload,
      skipErrorToast: true,
    }),
  detail: (id: number) => request<VideoProjectDetail>({ url: `/office/videos/${id}` }),
  save: (id: number, payload: VideoProjectSave) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}`,
      method: 'PUT',
      data: payload,
      skipErrorToast: true,
    }),
  remove: (id: number) => request<null>({ url: `/office/videos/${id}`, method: 'DELETE' }),
  /** 重跑创作卡与分镜：只有这两个字段会影响输出，其余改动走 save。 */
  regenerate: (id: number, payload: { requirement?: string; duration_s?: number }) =>
    request<VideoProjectItem>({
      url: `/office/videos/${id}/script`,
      method: 'POST',
      data: payload,
      skipErrorToast: true,
    }),
  /** 整批保存镜头，返回 Detail：新镜的 id 与重算过的时间轴都在服务端。 */
  saveShots: (id: number, shots: VideoShotInput[]) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/shots`,
      method: 'PUT',
      data: { shots },
      skipErrorToast: true,
    }),
  /** ``shotIds`` 为空表示项目里全部镜头。 */
  renderFrames: (id: number, shotIds: number[]) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/frames`,
      method: 'POST',
      data: { shot_ids: shotIds },
      skipErrorToast: true,
    }),
  renderCover: (id: number) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/cover`,
      method: 'POST',
      skipErrorToast: true,
    }),
  cancelJob: (id: number, jobId: number) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/jobs/${jobId}/cancel`,
      method: 'POST',
      skipErrorToast: true,
    }),
  /**
   * 把这一镜渲好的首帧复制一条进素材库。名字与归属都在服务端定（``项目｜第 N 镜首帧``），
   * 所以这里没有请求体；库里那条与产物是两份文件，删项目不会把它带走。
   */
  saveFrameToMaterial: (id: number, shotId: number) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/shots/${shotId}/to-material`,
      method: 'POST',
      skipErrorToast: true,
    }),
}
