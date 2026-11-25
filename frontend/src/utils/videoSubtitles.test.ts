import { describe, expect, it } from 'vitest'
import { buildAss, buildSrt, subtitleCues } from '@/utils/videoSubtitles'
import type { VideoShot } from '@/api'

/** 只填了字幕导出用得上的三列，其余按后端默认值补齐。 */
function shot(over: Partial<VideoShot> = {}): VideoShot {
  return {
    id: 1,
    sort_order: 0,
    start_s: 0,
    duration_s: 5,
    scene: '',
    scene_key: '',
    camera: '',
    action: '',
    dialogue: '',
    on_screen_text: '',
    image_prompt: '',
    video_prompt: '',
    gen_mode: 'first_frame',
    material_ids: [],
    image_job: null,
    video_job: null,
    ...over,
  }
}

describe('subtitleCues', () => {
  it('起点按前面各镜时长累加，与后端 save_shots 的时间轴同一口径', () => {
    const cues = subtitleCues([shot({ duration_s: 6 }), shot({ duration_s: 9, on_screen_text: '第二步' })])
    expect(cues).toEqual([{ index: 2, startS: 6, durationS: 9, text: '第二步' }])
  })

  it('没写屏幕文字的镜头不占一行', () => {
    expect(subtitleCues([shot({ on_screen_text: '   ' }), shot()])).toEqual([])
  })

  it('时长为 0 的行至少占一秒，免得导出前后同秒的两条挤在一起', () => {
    expect(subtitleCues([shot({ duration_s: 0, on_screen_text: '开场' })])[0].durationS).toBe(1)
  })
})

describe('buildSrt', () => {
  it('两条 cue 之间空一行，时间戳补到毫秒', () => {
    const text = buildSrt([
      shot({ duration_s: 4, on_screen_text: '开场' }),
      shot({ duration_s: 6, on_screen_text: '结尾' }),
    ])
    expect(text).toBe(
      '1\n00:00:00,000 --> 00:00:04,000\n开场\n\n2\n00:00:04,000 --> 00:00:10,000\n结尾\n\n',
    )
  })

  it('一条字幕都没有时出空串，不给用户落一个只有表头的文件', () => {
    expect(buildSrt([shot(), shot()])).toBe('')
  })

  it('跨过一分钟仍然补零，不写出 00:01:60 这种时间', () => {
    const cues = [shot({ duration_s: 70, on_screen_text: '长镜' })]
    expect(buildSrt(cues)).toContain('00:00:00,000 --> 00:01:10,000')
  })
})

describe('buildAss', () => {
  it('竖屏 720x1280，时间戳是百分秒', () => {
    const text = buildAss([shot({ duration_s: 4, on_screen_text: '开场' })], '季度复盘')
    expect(text).toContain('PlayResY: 1280')
    expect(text).toContain('Title: 季度复盘')
    expect(text).toContain('Dialogue: 0,0:00:00.00,0:00:04.00,Default,0,0,0,开场')
  })

  it('换行转成 \\N，不把一条 cue 拆成两行破坏文件结构', () => {
    expect(buildAss([shot({ on_screen_text: '上句\n下句' })])).toContain('Default,0,0,0,上句\\N下句')
  })
})
