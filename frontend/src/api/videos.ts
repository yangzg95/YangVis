/**
 * 短视频域：项目的创建与保存、脚本（创作卡 + 分镜）的重新生成、首帧与片段渲染及取消。
 * 参考素材不在这里——它在账号级的素材库（api/materials.ts），镜头上只存引用的 id。
 * 字面量类型与后端 ``models/schemas.py`` 的白名单一字不差——那些值原样进方舟的请求体，
 * 中间没有翻译层，这里写错一个字符就是上游 400。
 *
 * 两阶段各有进度来源：脚本阶段是工作流运行（``status``），渲染阶段是 ``media_job``
 * （``image_job.status`` / ``video_job.status``）。所以这一页既没有「等待结果」的接口，
 * 也不该有：入队之后唯一诚实的答案是「排队中」，出片要几十秒到几分钟，靠详情接口的轮询跟随。
 */
import { request } from './http'
import type { RunStatus, RunStepStatus } from './workflows'

// ---- 智能应用 · 短视频 -------------------------------------------------------

export type VideoPlatform = 'douyin' | 'kuaishou' | 'other'
/** 首帧流是默认档；``first_last_frame`` 还没开（尾帧要另起一条产物链路），界面上禁用。 */
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
  watermark: boolean
  /** 默认关：方舟那一侧默认开，留着就会拿到一条模型自己编的人声。人声统一走 tts_enabled。 */
  generate_audio: boolean
  image_size: VideoImageSize
  /** 成片时是否叠 TTS 配音：按各镜台词逐句合成、按镜头时间轴混进成片，片段始终静音。 */
  tts_enabled: boolean
  /** 音色 ID；空串表示用服务端 TTS_DEFAULT_VOICE。 */
  tts_voice: string
  /** 尾帧链式渲染：上一镜尾帧作为下一镜首帧，镜间画面物理连续；开启后同项目片段串行渲染。 */
  chain_clips: boolean
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
  /** 链式渲染里这条作业在等第几镜的尾帧；非空时 queued 应显示「等待第 N 镜」。 */
  chain_wait_shot: number | null
  created_at: string | null
  finished_at: string | null
}

/** 工作台里的一镜：两个指针各带最近一次作业，状态与产物都从作业行读。 */
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
  video_job: MediaJob | null
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

/** 脚本生成里的一步。prompt、产物与轨迹都在运行详情页，这里只要状态与耗时。 */
export interface VideoScriptStep {
  /** 内置流程的两步：``brief``（创作卡）与 ``script``（分镜）。 */
  key: string
  status: RunStepStatus
  elapsed_ms: number | null
  /** 在跑的那一步靠它算「已跑多久」；还没轮到就没有。 */
  started_at: string | null
  finished_at: string | null
}

/** 最近一次脚本生成。两步串行，AI 网关按调用分行记日志，这里给整条的分解。 */
export interface VideoScriptRun {
  id: number
  status: RunStatus
  /** 整条的墙上时间，不是各步相加。 */
  elapsed_ms: number | null
  started_at: string | null
  steps: VideoScriptStep[]
}

/** 发布作业状态；通道字面量与后端 Literal 白名单一字不差。 */
export type PublishJobStatus = 'queued' | 'running' | 'succeeded' | 'failed'
export type PublishChannel = 'manual'

export interface PublishJobProgress {
  stage: string
  percent: number
  detail: string
  /** TTS 等按数量推进的阶段给出已完成/总数；其他阶段为 null。 */
  current: number | null
  total: number | null
}

/**
 * 一条发布作业（成片合成 + 手动发布包）。``url`` 是成片的站内地址，
 * 还没合成成功时是空串——rel_path 不出后端，前端只认这个地址。
 */
export interface PublishJob {
  id: number
  platform: VideoPlatform
  channel: PublishChannel
  status: PublishJobStatus
  url: string
  file_size: number | null
  error_msg: string | null
  /** 从入队算到成片落盘的本机耗时。未跑完为 null。 */
  elapsed_ms: number | null
  created_at: string | null
  finished_at: string | null
  progress: PublishJobProgress | null
}

export interface VideoStageProgress {
  total: number
  succeeded: number
  /** 本地排队、上游生成和转存都还在进行中。 */
  active: number
  failed: number
  cancelled: number
  expired: number
}

export interface VideoProjectItem {
  id: number
  title: string
  platform: VideoPlatform
  /** 仅脚本阶段；首帧、片段与成片分别看下面三个字段。 */
  status: VideoProjectStatus | string
  error_msg: string | null
  shot_count: number
  /** 全部镜头时长之和，秒。 */
  duration_s: number
  cover_url: string
  frames: VideoStageProgress
  clips: VideoStageProgress
  /** 最近一次成片合成作业；从没合成过为 null。 */
  publish_job: PublishJob | null
  created_at: string
  updated_at: string
}

export interface VideoProjectDetail extends VideoProjectItem {
  idea: string
  requirement: string
  brief: VideoBrief
  /** 整条口播文案；TTS 配音读的是每镜 dialogue，这份只是供人通读的成稿记录。 */
  script: string
  style: VideoStyle
  params: VideoParams
  publish: VideoPublish
  /** 项目角色组：人物库 id（字符串形式），上限见 MAX_CHARACTERS_PER_PROJECT。 */
  character_ids: string[]
  shots: VideoShot[]
  /** 最近一次脚本生成的现场；运行记录被留存清理删掉后为 null。 */
  script_run: VideoScriptRun | null
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
  /** 出场角色：建档时就选好，脚本生成会带上角色档案，渲染会注入形象图。 */
  character_ids?: string[]
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
  /** 空数组是显式清空角色组；undefined 保持原样。 */
  character_ids?: string[]
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
  /** 「参考上一版片段」能不能用：方舟的视频参考只收公网 URL，没配基址就发不出去。 */
  video_reference_ready: boolean
}

export const videosApi = {
  list: (params: { q?: string; order?: string } = {}) =>
    request<{ items: VideoProjectItem[]; total: number }>({
      url: '/office/videos',
      params,
    }),
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
  /**
   * ``shotIds`` 为空表示项目里全部镜头。首帧档要求这一镜的首帧产物已经落盘。
   * ``referencePrevious`` 再把本镜上一版成片挂成参考视频，要求它也已落盘。
   */
  renderClips: (id: number, shotIds: number[], referencePrevious = false) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/clips`,
      method: 'POST',
      data: { shot_ids: shotIds, reference_previous: referencePrevious },
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
  /** 把这一镜成功的视频片段复制一条进素材库（视频类，本期只记录引用不参与渲染）。 */
  saveClipToMaterial: (id: number, shotId: number) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/shots/${shotId}/clip-to-material`,
      method: 'POST',
      skipErrorToast: true,
      timeout: 120000,
    }),
  /**
   * 入队一条成片合成：各镜最近一次成功片段按镜头顺序拼成一个 MP4，进度看 publish_job。
   * allowMissing 为真时出草稿片，跳过还没有成功片段的镜头（TTS 时间轴由后端重算）。
   */
  compose: (id: number, allowMissing = false) =>
    request<VideoProjectDetail>({
      url: `/office/videos/${id}/compose`,
      method: 'POST',
      data: { allow_missing: allowMissing },
      skipErrorToast: true,
    }),
  /**
   * 单句试听：真合成一段 mp3 回二进制（真计费，只由按钮显式触发）。
   * 失败时后端回的是 JSON 信封且响应类型仍是 blob，由调用方读 blob 文本拆 message。
   */
  ttsPreview: async (text: string, voice: string): Promise<Blob> => {
    const response = await request<{ data: Blob }>({
      url: '/office/videos/tts-preview',
      method: 'POST',
      data: { text, voice },
      responseType: 'blob',
      skipErrorToast: true,
      timeout: 60000,
    })
    return response.data
  },
}
