/**
 * 字幕导出：把镜头表上的「屏幕文字」按时间轴写成 SRT / ASS。
 *
 * 这一期不烧字幕（烧进去就改不动了），出片时给用户的是**旁边那份文件**：
 * 剪映/PR/必剪都吃 SRT，ASS 自带样式，竖屏直接可用。
 *
 * 时间轴口径与服务层 ``save_shots`` 一致：数组顺序即镜头顺序，起点是前面各镜
 * 时长的累加。前端不重新读库里的 ``start_s``——用户在保存前导出，看到的就该是
 * 屏幕上这套顺序。
 */
import type { VideoShot } from '@/api'

/** 一镜在时间轴上的位置；``start_s`` 是累加出来的，与后端算法同一份。 */
export interface ShotCue {
  index: number
  startS: number
  durationS: number
  text: string
}

const pad = (value: number, width = 2) => String(value).padStart(width, '0')

/** 按指定字段构建时间轴 cue：起点是前面各镜时长的累加，空文本镜头不占行。 */
function cuesFrom(shots: VideoShot[], pick: (shot: VideoShot) => string): ShotCue[] {
  const cues: ShotCue[] = []
  let cursor = 0
  shots.forEach((shot, index) => {
    const text = pick(shot).trim()
    const duration = Math.max(1, Math.round(shot.duration_s || 0))
    if (text) cues.push({ index: index + 1, startS: cursor, durationS: duration, text })
    cursor += duration
  })
  return cues
}

/** 只有写了屏幕文字的镜头出字幕；空文字的那一镜在两份文件里都不该占一行。 */
export function subtitleCues(shots: VideoShot[]): ShotCue[] {
  return cuesFrom(shots, (shot) => shot.on_screen_text || '')
}

/** 按台词（TTS 配音源文本）构建 cue，用来导出配音校对用的 SRT。 */
export function dialogueCues(shots: VideoShot[]): ShotCue[] {
  return cuesFrom(shots, (shot) => shot.dialogue || '')
}

/** 秒 → 「HH:MM:SS,mmm」（SRT）或「H:MM:SS.cc」（ASS，百分秒）。 */
function srtTime(seconds: number): string {
  const ms = Math.round(Math.max(0, seconds) * 1000)
  return `${pad(Math.floor(ms / 3_600_000))}:${pad(Math.floor(ms / 60_000) % 60)}:${pad(
    Math.floor(ms / 1000) % 60,
  )},${pad(ms % 1000, 3)}`
}

function assTime(seconds: number): string {
  const cs = Math.round(Math.max(0, seconds) * 100)
  return `${Math.floor(cs / 360_000)}:${pad(Math.floor(cs / 6_000) % 60)}:${pad(
    Math.floor(cs / 100) % 60,
  )}.${pad(cs % 100)}`
}

function srtFromCues(cues: ShotCue[]): string {
  if (!cues.length) return ''
  // 块与块之间空一行，最后一条也以换行收尾——播放器对结尾空行的容忍度不一样。
  return (
    cues
      .map(
        (cue, i) =>
          `${i + 1}\n${srtTime(cue.startS)} --> ${srtTime(cue.startS + cue.durationS)}\n${cue.text}\n`,
      )
      .join('\n') + '\n'
  )
}

/** 屏幕文字版 SRT：剪映/PR/必剪通用，时间轴按镜头顺序累加。 */
export function buildSrt(shots: VideoShot[]): string {
  return srtFromCues(subtitleCues(shots))
}

/** 台词版 SRT：拿 TTS 配音源文本做配音校对，时间轴与屏幕文字版同一口径。 */
export function buildDialogueSrt(shots: VideoShot[]): string {
  return srtFromCues(dialogueCues(shots))
}

/**
 * ASS：竖屏 720x1280、底部居中、留出安全边距。
 *
 * 字号与边距按 9:16 定，是因为这份文件的用途就是抖音/快手：横屏那条链路
 * 用户会自己换模板，这里替他做一个「打开就能用」的默认。
 */
export function buildAss(shots: VideoShot[], title = '字幕'): string {
  const cues = subtitleCues(shots)
  const head = [
    '[Script Info]',
    `Title: ${title}`,
    'ScriptType: v4.00+',
    'PlayResX: 720',
    'PlayResY: 1280',
    'WrapStyle: 0',
    'ScaledBorderAndShadow: yes',
    '',
    '[V4+ Styles]',
    'Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV',
    // &H00FFFFFF 是 ASS 的 BGR 白；阴影关掉，竖屏上有阴影会糊。
    'Style: Default,PingFang SC,44,&H00FFFFFF,&H64000000,1,1,2,0,2,48,48,150',
    '',
    '[Events]',
    'Format: Layer, Start, End, Style, MarginL, MarginR, MarginV, Text',
  ]
  const lines = cues.map(
    (cue) =>
      `Dialogue: 0,${assTime(cue.startS)},${assTime(cue.startS + cue.durationS)},Default,0,0,0,${cue.text.replace(/\r?\n/g, '\\N')}`,
  )
  return `${[...head, ...lines].join('\n')}\n`
}
