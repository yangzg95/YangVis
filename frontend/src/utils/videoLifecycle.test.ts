import { describe, expect, it } from 'vitest'
import type { VideoProjectItem, VideoStageProgress } from '@/api/videos'
import {
  hasActiveVideoWork,
  matchesVideoLifecycle,
  videoLifecycleStage,
} from './videoLifecycle'

function progress(overrides: Partial<VideoStageProgress> = {}): VideoStageProgress {
  return {
    total: 0,
    succeeded: 0,
    active: 0,
    failed: 0,
    cancelled: 0,
    expired: 0,
    ...overrides,
  }
}

function project(overrides: Partial<VideoProjectItem> = {}): VideoProjectItem {
  return {
    id: 1,
    title: '测试短片',
    platform: 'douyin',
    status: 'ready',
    error_msg: null,
    shot_count: 0,
    duration_s: 0,
    cover_url: '',
    frames: progress(),
    clips: progress(),
    publish_job: null,
    created_at: '2026-11-18T10:00:00',
    updated_at: '2026-11-18T10:00:00',
    ...overrides,
  }
}

describe('videoLifecycleStage', () => {
  it('保留脚本状态与失败原因', () => {
    expect(videoLifecycleStage(project({ status: 'analyzing' }), 'script')).toMatchObject({
      state: 'active',
      text: '生成中',
    })
    expect(
      videoLifecycleStage(project({ status: 'error', error_msg: '模型超时' }), 'script'),
    ).toMatchObject({ state: 'attention', hint: '模型超时', attention: 1 })
  })

  it('区分无镜头、待渲染、部分完成和全部完成', () => {
    expect(videoLifecycleStage(project(), 'frames').state).toBe('empty')
    expect(
      videoLifecycleStage(project({ frames: progress({ total: 3 }) }), 'frames'),
    ).toMatchObject({ state: 'pending', detail: '0/3 完成' })
    expect(
      videoLifecycleStage(
        project({ frames: progress({ total: 3, succeeded: 1 }) }),
        'frames',
      ),
    ).toMatchObject({ state: 'partial', detail: '1/3 完成' })
    expect(
      videoLifecycleStage(
        project({ frames: progress({ total: 3, succeeded: 3 }) }),
        'frames',
      ),
    ).toMatchObject({ state: 'done', detail: '3/3 完成' })
  })

  it('进行中优先展示，同时保留失败、取消和超时的需处理数', () => {
    const stage = videoLifecycleStage(
      project({
        frames: progress({
          total: 7,
          succeeded: 1,
          active: 2,
          failed: 1,
          cancelled: 1,
          expired: 1,
        }),
      }),
      'frames',
    )

    expect(stage).toMatchObject({ state: 'active', attention: 3, detail: '1/7 完成' })
    expect(stage.hint).toContain('2 镜进行中')
    expect(stage.hint).toContain('1 镜待渲染')
    expect(stage.hint).toContain('1 镜失败')
    expect(stage.hint).toContain('1 镜已取消')
    expect(stage.hint).toContain('1 镜超时')
  })

  it('成片区分待片段、待合成、合成中、失败和完成', () => {
    expect(videoLifecycleStage(project(), 'publish').state).toBe('empty')
    expect(
      videoLifecycleStage(project({ clips: progress({ total: 2, succeeded: 1 }) }), 'publish'),
    ).toMatchObject({ state: 'blocked', text: '待片段' })
    expect(
      videoLifecycleStage(project({ clips: progress({ total: 2, succeeded: 2 }) }), 'publish'),
    ).toMatchObject({ state: 'pending', text: '待合成' })

    const base = { platform: 'douyin' as const, channel: 'manual' as const }
    expect(
      videoLifecycleStage(
        project({
          publish_job: {
            ...base,
            id: 1,
            status: 'queued',
            url: '',
            file_size: null,
            error_msg: null,
            elapsed_ms: null,
            created_at: null,
            finished_at: null,
            progress: null,
          },
        }),
        'publish',
      ).state,
    ).toBe('active')
    expect(
      videoLifecycleStage(
        project({
          publish_job: {
            ...base,
            id: 2,
            status: 'failed',
            url: '',
            file_size: null,
            error_msg: 'ffmpeg 失败',
            elapsed_ms: null,
            created_at: null,
            finished_at: null,
            progress: null,
          },
        }),
        'publish',
      ),
    ).toMatchObject({ state: 'attention', hint: 'ffmpeg 失败' })
    expect(
      videoLifecycleStage(
        project({
          publish_job: {
            ...base,
            id: 3,
            status: 'succeeded',
            url: '/uploads/videos/1/final.mp4',
            file_size: 100,
            error_msg: null,
            elapsed_ms: 1000,
            created_at: null,
            finished_at: null,
            progress: null,
          },
        }),
        'publish',
      ).state,
    ).toBe('done')
  })
})

describe('matchesVideoLifecycle', () => {
  it('按阶段筛选，并能筛出进行中阶段里已有的失败镜头', () => {
    const item = project({
      frames: progress({ total: 3, active: 2, failed: 1 }),
      clips: progress({ total: 3 }),
    })

    expect(matchesVideoLifecycle(item, '')).toBe(true)
    expect(matchesVideoLifecycle(item, 'frames:active')).toBe(true)
    expect(matchesVideoLifecycle(item, 'frames:attention')).toBe(true)
    expect(matchesVideoLifecycle(item, 'clips:pending')).toBe(true)
    expect(matchesVideoLifecycle(item, 'clips:attention')).toBe(false)
    expect(matchesVideoLifecycle(item, 'unknown')).toBe(false)
  })
})

describe('hasActiveVideoWork', () => {
  it('脚本、本地排队媒体或合成任一活动都需要继续轮询', () => {
    expect(hasActiveVideoWork(project({ status: 'analyzing' }))).toBe(true)
    expect(hasActiveVideoWork(project({ frames: progress({ total: 1, active: 1 }) }))).toBe(
      true,
    )
    expect(
      hasActiveVideoWork(
        project({
          publish_job: {
            id: 1,
            platform: 'douyin',
            channel: 'manual',
            status: 'running',
            url: '',
            file_size: null,
            error_msg: null,
            elapsed_ms: null,
            created_at: null,
            finished_at: null,
            progress: null,
          },
        }),
      ),
    ).toBe(true)
    expect(hasActiveVideoWork(project())).toBe(false)
  })
})
