/**
 * 创作大纲树的前端纯函数：展平、查找、摘除、同层移动与卷追加。
 *
 * 形状由后端 sanitize_outline 兜底（小说 卷→章 一层、文章扁平段落），
 * 这里只在提交前做交互级改写，不发 cid——新节点的 cid 一律由服务端发放。
 * 与后端约定一致：数组下标不参与寻址，所有操作按 cid 找节点。
 */
import type { OutlineNode, WritingKind } from '@/api/writing'

/** 与后端 outline._CID_RE 同形：p + 16 位小写 hex。 */
const CID_RE = /^p[0-9a-f]{16}$/

export function isCid(value: unknown): value is string {
  return typeof value === 'string' && CID_RE.test(value)
}

export function leafKind(workKind: WritingKind): 'chapter' | 'section' {
  return workKind === 'novel' ? 'chapter' : 'section'
}

export function defaultLeafTitle(workKind: WritingKind): string {
  return workKind === 'novel' ? '未命名章节' : '未命名段落'
}

/** 按阅读顺序展平叶子（跳过卷节点）。 */
export function flattenLeaves(nodes: OutlineNode[]): OutlineNode[] {
  const leaves: OutlineNode[] = []
  for (const node of nodes) {
    if (node.kind === 'volume') {
      leaves.push(...(node.children ?? []))
    } else {
      leaves.push(node)
    }
  }
  return leaves
}

export function findLeaf(nodes: OutlineNode[], cid: string): OutlineNode | undefined {
  return flattenLeaves(nodes).find((node) => node.cid === cid)
}

export function leafTitles(nodes: OutlineNode[]): Array<[string, string]> {
  return flattenLeaves(nodes).map((node) => [node.cid, node.title])
}

/** 节点总数（含卷），用于对照后端 120 上限给提示。 */
export function countNodes(nodes: OutlineNode[]): number {
  return nodes.reduce((sum, node) => sum + 1 + (node.kind === 'volume' ? (node.children?.length ?? 0) : 0), 0)
}

/** 从树里摘掉一个叶子（卷内章或顶层叶子），原地修改；返回是否摘到。 */
export function removeLeaf(nodes: OutlineNode[], cid: string): boolean {
  for (const node of nodes) {
    if (node.kind === 'volume') {
      const children = node.children ?? []
      const index = children.findIndex((child) => child.cid === cid)
      if (index >= 0) {
        children.splice(index, 1)
        return true
      }
    } else if (node.cid === cid) {
      nodes.splice(nodes.indexOf(node), 1)
      return true
    }
  }
  return false
}

/**
 * 同层上移/下移一个叶子。不跨容器（卷内章不挪到别的卷、不混成顶层章）：
 * 跨容器拖拽排序留给 M4，按钮只做无歧义的相邻交换。dir：-1 上移，1 下移。
 * 返回是否发生了移动（已在边界时返回 false）。
 */
export function moveLeaf(nodes: OutlineNode[], cid: string, dir: -1 | 1): boolean {
  for (const node of nodes) {
    const container = node.kind === 'volume' ? (node.children ?? []) : null
    if (container) {
      const index = container.findIndex((child) => child.cid === cid)
      if (index >= 0) return swapAt(container, index, dir)
    }
  }
  const topIndex = nodes.findIndex((node) => node.kind !== 'volume' && node.cid === cid)
  if (topIndex >= 0) return swapAt(nodes, topIndex, dir)
  return false
}

function swapAt(list: OutlineNode[], index: number, dir: -1 | 1): boolean {
  const target = index + dir
  if (target < 0 || target >= list.length) return false
  const [item] = list.splice(index, 1)
  list.splice(target, 0, item)
  return true
}

/** 小说追加一个空卷（无叶子，不建 part）。新节点不带 cid，交给服务端发放。 */
export function appendVolume(nodes: OutlineNode[], title = '未命名卷'): OutlineNode {
  const volume: OutlineNode = {
    cid: '',
    kind: 'volume',
    title,
    synopsis: '',
    target_words: null,
    children: [],
  }
  nodes.push(volume)
  return volume
}

/** 深拷贝一棵大纲树，供「先本地改、再整棵 PUT」的交互使用。 */
export function cloneOutline(nodes: OutlineNode[]): OutlineNode[] {
  return JSON.parse(JSON.stringify(nodes)) as OutlineNode[]
}
