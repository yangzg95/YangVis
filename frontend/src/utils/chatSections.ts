/**
 * 聊天消息气泡的切段与待确认项对位：
 * - 助手气泡按 timeline 原顺序切成「过程组 / 正文段 / 确认卡」交替渲染；
 * - 待确认项按 message_id 对位到气泡，时间线里已就地渲染的确认卡不再重复挂载。
 *
 * 这里只放纯逻辑（不依赖页面 ref），页面与气泡组件把当前会话的状态当参数传进来。
 */
import type { ChatAttachment, Citation, OpsAction, TimelineEntry } from '@/api'
import { splitMessageSegments, type MessageSegment } from '@/utils/messageSegments'
import { capCache } from '@/utils/cacheLimit'

export interface Bubble {
  key: string
  /** 落库后的消息 id；流式刚结束才有。手动改图要拿它调更新接口。 */
  id?: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  /** 思考与工具调用的交错时间线；用户消息恒无此字段，纯问答也为空。 */
  timeline?: TimelineEntry[]
  /** 用户提问时带的附件；助手消息恒为空。 */
  attachments?: ChatAttachment[]
  /** 这条提问的回答失败过，界面上给出重试入口。 */
  failed?: boolean
}

// ---- 助手气泡的交错切段 ------------------------------------------------------
// timeline 按到达顺序混着 reason/step/llm/text/confirm 五类条目，渲染时切成
// 交替的三种段：
// - trace：连续的过程条目（思考 + 工具 + 模型计时），交给 ThinkingTrace；
// - text：连续正文条目的拼接，走 markdown（落定后再切 mermaid 图卡）；
// - confirm：写命令提议，就地插确认卡。
export type AssistantSection = {
  key: string
  type: 'trace' | 'text' | 'confirm'
  /** trace 段：组内的原始 timeline 条目（ThinkingTrace 自己适配成节点）。 */
  entries?: TimelineEntry[]
  /** text 段：这段正文的完整文本。 */
  text?: string
  /** confirm 段：对位到的待确认项；对位不到时不在原地渲染，走 actionsOf 兜底。 */
  action?: OpsAction | null
  actionId?: number
  /** 流式中只有最后一段是活跃的：trace 段展开转圈，text 段走整段流式渲染。 */
  streaming?: boolean
  /** 本段之前各正文段累计的子段数：图卡保存按「全文第 i 段」回写，要跨段累计。 */
  segmentBase?: number
}

export function assistantSections(
  msg: Bubble,
  actionById: Map<number, OpsAction>,
  streamingKey: string | null,
): AssistantSection[] {
  const sections: AssistantSection[] = []
  let traceBuf: TimelineEntry[] = []
  let textBuf: string[] = []
  let traceSeq = 0
  let textSeq = 0
  let confirmSeq = 0

  const flushTrace = () => {
    if (!traceBuf.length) return
    const entries = traceBuf
    traceBuf = []
    // 只剩「模型往返计时点」的过程组不展示：纯问答末尾那颗 llm 节点没有工具
    // 也没有思考内容，露一个折叠框纯属噪音。
    if (!entries.some((e) => e.type !== 'llm')) return
    sections.push({
      key: `${msg.key}:tr${traceSeq++}`,
      type: 'trace',
      entries,
    })
  }
  const flushText = () => {
    if (!textBuf.length) return
    const text = textBuf.join('')
    textBuf = []
    if (text) sections.push({ key: `${msg.key}:tx${textSeq++}`, type: 'text', text })
  }

  for (const entry of msg.timeline ?? []) {
    if (entry.type === 'reason' || entry.type === 'step' || entry.type === 'llm') {
      // 过程条目连续收进同一组：ReAct 循环里「工具返回 → 思考 → 再调工具」
      // 中间没有正文，本就是同一轮排查；组只在遇到正文/确认卡时断开，保证每组
      // 后面紧跟的就是这一轮解说文字，而不是把整条回答的过程全堆到气泡头部。
      flushText()
      traceBuf.push(entry)
    } else if (entry.type === 'text') {
      flushTrace()
      textBuf.push(entry.detail ?? '')
    } else if (entry.type === 'confirm') {
      flushTrace()
      flushText()
      sections.push({
        key: `${msg.key}:cf${confirmSeq++}`,
        type: 'confirm',
        actionId: entry.action_id ?? -1,
      })
    }
  }
  flushTrace()
  flushText()

  // 没有时间线（老消息/纯问答）、或整条时间线里没有正文（只思考调工具，后端
  // 补了固定话术）：用整段 content 兜底成一个正文段，保证结论一定有地方渲染。
  if (!sections.some((s) => s.type === 'text') && msg.content) {
    sections.push({ key: `${msg.key}:content`, type: 'text', text: msg.content })
  }

  for (const sec of sections) {
    if (sec.type === 'confirm') {
      sec.action = actionById.get(sec.actionId ?? -1) ?? null
    }
  }

  // 活跃态只给最后一段：模型转到下一段（思考→工具→正文）后，前一组就地落定，
  // ThinkingTrace 随即自动折叠，不必等整条回答结束。
  if (msg.key === streamingKey && sections.length) {
    sections[sections.length - 1].streaming = true
  }

  // 子段全局序号基数：图卡保存按全文 splitMessageSegments 的下标回写
  // （markdown 段也占下标），基数 = 之前各正文段的子段数之和。
  let segmentBase = 0
  for (const sec of sections) {
    if (sec.type !== 'text' || !sec.text) continue
    // 活跃正文段流式时走整段渲染、不切子段，也不可能有排在它后面的段需要基数。
    if (sec.streaming) break
    sec.segmentBase = segmentBase
    segmentBase += segmentsOfText(sec.key, sec.text).length
  }
  return sections
}

/**
 * 待确认项 → 该气泡末尾要挂的卡片。
 *
 * 已在时间线里就地渲染的确认卡不再挂气泡末尾，避免同一条命令出现两张卡。
 */
export function actionsOf(
  msg: Bubble,
  actions: OpsAction[],
  streamingKey: string | null,
  tailKey: string | null,
): OpsAction[] {
  const inline = new Set(
    (msg.timeline ?? []).filter((e) => e.type === 'confirm').map((e) => e.action_id),
  )
  return actions.filter((a) => {
    if (inline.has(a.id)) return false
    if (a.message_id != null) return a.message_id === msg.id
    // message_id 没回填（整轮回答没落库就断了：停止/刷新/断线）：宁可挂到最后
    // 一条助手气泡，也不能让它谁都不匹配——这条命令在界面上根本不存在。
    return msg.key === streamingKey || msg.key === tailKey
  })
}

// 正文段 → mermaid 子段的缓存：与 renderCached 同理，文本没变就不重新切分，
// 避免流式期间每个 token 都把已经落定的正文段重切一遍。
const sectionSegCache = new Map<string, { text: string; segments: MessageSegment[] }>()

function segmentsOfText(key: string, text: string): MessageSegment[] {
  const hit = sectionSegCache.get(key)
  if (hit && hit.text === text) return hit.segments
  const segments = splitMessageSegments(text)
  sectionSegCache.set(key, { text, segments })
  capCache(sectionSegCache, 200)
  return segments
}

/** 模板用：子段附上它在整条消息里的全局子段序号（非图段用不到，给 -1）。 */
export function sectionParts(
  sec: AssistantSection,
): { seg: MessageSegment; globalIndex: number }[] {
  return segmentsOfText(sec.key, sec.text ?? '').map((seg, i) => ({
    seg,
    globalIndex: seg.kind === 'mermaid' ? (sec.segmentBase ?? 0) + i : -1,
  }))
}

/** 消息列表整体换掉时清空分段缓存（与 streamRender 的 HTML 缓存一起清）。 */
export function clearSectionCache(): void {
  sectionSegCache.clear()
}
