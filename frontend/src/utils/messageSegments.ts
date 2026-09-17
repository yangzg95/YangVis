/**
 * 把一条助手消息按 ```mermaid 围栏切成「markdown 段 / 图段」。
 *
 * 聊天消息的主流仍是 markdown，只有 mermaid 代码块需要升级成可交互的图卡片
 * （预览、编辑、导出）。切分之后两种段各走各的渲染：markdown 段照旧走
 * markdown-it，图段交给 MermaidCard。
 *
 * 流式期间的未闭合围栏不当作图：整段退回 markdown，渲染成普通代码块——
 * 等闭合 ``` 到达，下一次切分自然会把它升级成卡片，用户看到的就是
 * 「代码块长成了一张图」。
 */

export interface MessageSegment {
  kind: 'markdown' | 'mermaid'
  /** markdown 段是原文（直接进 markdown-it）；mermaid 段是围栏里的图源码。 */
  text: string
}

const FENCE_OPEN = /^\s*```\s*mermaid\s*$/i
const FENCE_CLOSE = /^\s*```\s*$/

export function splitMessageSegments(content: string): MessageSegment[] {
  const segments: MessageSegment[] = []
  let mdLines: string[] = []
  let mermaidLines: string[] | null = null

  const flushMarkdown = () => {
    // 空段直接丢：围栏前后的空行没有渲染价值，留着只会多出一块空白。
    if (mdLines.length && mdLines.join('\n').trim()) {
      segments.push({ kind: 'markdown', text: mdLines.join('\n') })
    }
    mdLines = []
  }

  for (const line of content.split('\n')) {
    if (mermaidLines === null) {
      if (FENCE_OPEN.test(line)) {
        flushMarkdown()
        mermaidLines = []
      } else {
        mdLines.push(line)
      }
    } else if (FENCE_CLOSE.test(line)) {
      segments.push({ kind: 'mermaid', text: mermaidLines.join('\n') })
      mermaidLines = null
    } else {
      mermaidLines.push(line)
    }
  }

  if (mermaidLines !== null) {
    // 未闭合的围栏：连同开围栏那行一起退回 markdown 段。
    mdLines.push('```mermaid', ...mermaidLines)
  }
  flushMarkdown()
  return segments
}

/**
 * 把第 ``index`` 个图段的源码换成 ``newSource``，重建整条消息的文本。
 *
 * markdown 段原样保留，图段重新包上围栏。空段在切分时被丢过一轮，所以重建
 * 结果和原文未必逐字节相同（围栏前后的空行会少几个）——这对渲染和模型
 * 阅读都没有影响。
 */
export function replaceMermaidSegment(
  content: string,
  index: number,
  newSource: string,
): string {
  return splitMessageSegments(content)
    .map((segment, i) =>
      segment.kind === 'mermaid'
        ? `\`\`\`mermaid\n${i === index ? newSource : segment.text}\n\`\`\``
        : segment.text,
    )
    .join('\n')
}
