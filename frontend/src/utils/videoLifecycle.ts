import type { VideoProjectItem, VideoStageProgress } from '@/api/videos'

export type VideoLifecycleStage = 'script' | 'frames' | 'clips' | 'publish'
export type VideoLifecycleState =
  | 'empty'
  | 'pending'
  | 'active'
  | 'partial'
  | 'done'
  | 'attention'
  | 'blocked'
  | 'unknown'

export interface VideoLifecycleView {
  state: VideoLifecycleState
  text: string
  color: string
  detail: string
  hint: string
  /** 即使主状态是“进行中”，也让“需处理”筛选能查到同阶段的失败镜头。 */
  attention: number
}

function view(
  state: VideoLifecycleState,
  text: string,
  color = 'default',
  overrides: Partial<VideoLifecycleView> = {},
): VideoLifecycleView {
  return { state, text, color, detail: '', hint: '', attention: 0, ...overrides }
}

function scriptStage(item: VideoProjectItem): VideoLifecycleView {
  switch (item.status) {
    case 'uploaded':
      return view('pending', '待生成')
    case 'analyzing':
      return view('active', '生成中', 'processing')
    case 'ready':
      return view('done', '已生成', 'success')
    case 'error':
      return view('attention', '失败', 'error', {
        hint: item.error_msg || '脚本生成失败',
        attention: 1,
      })
    default:
      return view('unknown', item.status || '未知')
  }
}

function mediaStage(progress: VideoStageProgress): VideoLifecycleView {
  if (!progress.total) return view('empty', '—')

  const attention = progress.failed + progress.cancelled + progress.expired
  const pending = Math.max(
    0,
    progress.total - progress.succeeded - progress.active - attention,
  )
  const hints = [
    `${progress.succeeded} 镜已完成`,
    progress.active ? `${progress.active} 镜进行中` : '',
    pending ? `${pending} 镜待渲染` : '',
    progress.failed ? `${progress.failed} 镜失败` : '',
    progress.cancelled ? `${progress.cancelled} 镜已取消` : '',
    progress.expired ? `${progress.expired} 镜超时` : '',
  ].filter(Boolean)
  const common = {
    detail: `${progress.succeeded}/${progress.total} 完成`,
    hint: hints.join(' · '),
    attention,
  }

  if (progress.active) return view('active', '渲染中', 'processing', common)
  if (progress.succeeded === progress.total) return view('done', '已完成', 'success', common)
  if (attention) return view('attention', '需处理', 'error', common)
  if (progress.succeeded) return view('partial', '部分完成', 'warning', common)
  return view('pending', '待渲染', 'default', common)
}

function publishStage(item: VideoProjectItem): VideoLifecycleView {
  const job = item.publish_job
  if (job?.status === 'queued' || job?.status === 'running') {
    return view('active', '合成中', 'processing')
  }
  if (job?.status === 'failed') {
    return view('attention', '合成失败', 'error', {
      hint: job.error_msg || '成片合成失败',
      attention: 1,
    })
  }
  if (job?.status === 'succeeded') return view('done', '已合成', 'success')
  if (job) return view('unknown', job.status)
  if (!item.clips.total) return view('empty', '—')
  if (item.clips.succeeded === item.clips.total) return view('pending', '待合成')
  return view('blocked', '待片段')
}

export function videoLifecycleStage(
  item: VideoProjectItem,
  stage: VideoLifecycleStage,
): VideoLifecycleView {
  if (stage === 'script') return scriptStage(item)
  if (stage === 'frames') return mediaStage(item.frames)
  if (stage === 'clips') return mediaStage(item.clips)
  return publishStage(item)
}

/** 空串表示全部；内部值形如 frames:active、publish:attention。 */
export function matchesVideoLifecycle(item: VideoProjectItem, filter: string): boolean {
  if (!filter) return true
  const [stage, expected, extra] = filter.split(':')
  if (
    extra ||
    !['script', 'frames', 'clips', 'publish'].includes(stage) ||
    !expected
  ) {
    return false
  }
  const current = videoLifecycleStage(item, stage as VideoLifecycleStage)
  return expected === 'attention' ? current.attention > 0 : current.state === expected
}

/** 删除预检和轮询共用：本地排队的作业也已经计入 active。 */
export function hasActiveVideoWork(item: VideoProjectItem): boolean {
  return (
    item.status === 'analyzing' ||
    item.frames.active > 0 ||
    item.clips.active > 0 ||
    item.publish_job?.status === 'queued' ||
    item.publish_job?.status === 'running'
  )
}
