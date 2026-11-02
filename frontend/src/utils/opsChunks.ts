/**
 * 运维问答的「步骤 / 思考」就地折叠。
 *
 * 一条回答里可能有多波「执行命令」，每波后面紧跟一段解说文字。折叠按波就地收：
 * 只要有 ≥2 条连续步骤就默认收成一行「已执行 N 步」，点开看完整列表；单条命令
 * 不折叠（折叠它省不了空间，还把命令藏起来）。正文块不参与折叠，阅读流不受影响。
 * 这里只做纯函数折叠，展开状态由调用方持有，便于组件外单独测试。
 */

export const STEP_COLLAPSE_THRESHOLD = 1

export interface FoldChunk {
  type: 'step' | 'text' | 'reason' | 'confirm'
  /** step 时是命令摘要，text 时是这段文字，reason 时是这段推理，confirm 时留空。 */
  detail: string
  /** confirm 时指向 actions 里的那条待确认项。 */
  actionId?: number
  /** step 时的完整命令：detail 已被 toolLabel 截断，悬停看全文。 */
  full?: string
}

export type FoldedChunk<T extends FoldChunk, A> = T & {
  index: number
  /** 折叠后的一行摘要（正文块恒为 false）。 */
  summary: boolean
  /** 本组连续步骤的条数（摘要行文案用）。 */
  runCount: number
  /** 本组折叠状态的键：一条回答里多组命令各自独立展开/收起。 */
  runKey: string
  expanded: boolean
  /** confirm 条目对位的待确认项（已按 id 查好，查不到就不渲染）。 */
  action?: A
}

/**
 * 把一条回答的 chunks 摊平成模板可直接渲染的行。
 *
 * @param key 气泡的稳定键，参与折叠状态键的拼装（多组命令不能互相串台）。
 * @param expandedKeys 当前展开的折叠键集合。
 * @param actionById 待确认项索引；confirm 行据此挂到对应卡片上。
 */
export function foldChunks<T extends FoldChunk, A>(
  chunks: T[],
  key: string,
  expandedKeys: Set<string>,
  actionById: Map<number, A>,
  threshold: number = STEP_COLLAPSE_THRESHOLD,
): FoldedChunk<T, A>[] {
  const out: FoldedChunk<T, A>[] = []
  let i = 0
  let runIndex = 0
  while (i < chunks.length) {
    const chunk = chunks[i]
    if (chunk.type === 'text' || chunk.type === 'confirm') {
      out.push({
        ...chunk,
        index: i,
        summary: false,
        runCount: 0,
        runKey: '',
        expanded: false,
        action: chunk.type === 'confirm' ? actionById.get(chunk.actionId ?? -1) : undefined,
      })
      i += 1
      continue
    }
    // 收一段「同类型的连续 chunk」为一组：一组命令，或一段思考。正文和确认卡片
    // 把组隔开，所以每组对应模型的一轮动作，后面紧跟的解说文字留在原位——折叠必须
    // 按组就地收，若把整条回答的步骤全堆到顶部，各组命令就和它们各自的解说被拆散了。
    const runType = chunk.type // 'step' | 'reason'
    let j = i
    while (j < chunks.length && chunks[j].type === runType) j += 1
    const runCount = j - i
    const runKey = `${key}:${runType}:${runIndex}`
    // 命令：≥2 条才折叠（单条命令藏起来省不了空间）；思考：整段可折叠。
    const collapsible = runType === 'step' ? runCount > threshold : true
    const expanded = expandedKeys.has(runKey)
    if (collapsible && !expanded) {
      // 折叠态：整组收成一行摘要，就地待在这组原本的位置。
      out.push({ ...chunk, index: i, summary: true, runCount, runKey, expanded })
    } else {
      // 展开态（或本组不折叠）：可折叠时先给一行「收起」摘要，再逐条列出。
      if (collapsible) {
        out.push({ ...chunk, index: i, summary: true, runCount, runKey, expanded })
      }
      for (let k = i; k < j; k += 1) {
        out.push({ ...chunks[k], index: k, summary: false, runCount, runKey, expanded })
      }
    }
    runIndex += 1
    i = j
  }
  return out
}
