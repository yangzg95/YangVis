/**
 * 创作大纲树的前端纯函数：展平、查找、摘除、同层移动、拖拽落点规划与卷追加。
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

// ---- M4：a-tree 拖拽排序 --------------------------------------------------------

/** a-tree 落点归一化后的三态：before/after=节点缝隙，inside=落到节点本体（章入卷）。 */
export type DropPlacement = 'before' | 'after' | 'inside'

export interface TreeDrop {
  dragCid: string
  dropCid: string
  placement: DropPlacement
}

interface NodeLocation {
  node: OutlineNode
  /** 所在数组：根数组或某卷的 children（树只有两层，与后端 sanitize_outline 同形）。 */
  container: OutlineNode[]
  index: number
}

/** 按 cid 定位节点及其容器（先根层后卷内一层）。 */
function locateNode(nodes: OutlineNode[], cid: string): NodeLocation | null {
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index]
    if (node.cid === cid) return { node, container: nodes, index }
    if (node.kind === 'volume') {
      const children = node.children ?? []
      const childIndex = children.findIndex((child) => child.cid === cid)
      if (childIndex >= 0) {
        return { node: children[childIndex], container: children, index: childIndex }
      }
    }
  }
  return null
}

interface DropPlan {
  from: NodeLocation
  toContainer: OutlineNode[]
  toIndex: number
}

/**
 * 把一次拖拽换算成「从原容器摘掉、插进目标容器某处」的计划，非法落点给 null。
 * 规则与后端 sanitize_outline 收窄后的树形状对齐：
 * - 文章只有扁平段落：只接受根层缝隙，inside 一律非法；
 * - 卷只能在根层重排，不允许嵌套（嵌套卷会被后端清洗成章，属于毁数据，前置拦掉）；
 * - 章可同卷/跨卷挪动（落到另一章的缝隙即进它所在容器）；落卷上=入卷，
 *   落根层卷/章缝隙=提为顶层章；章上不允许 inside（章没有 children）。
 */
function planDrop(nodes: OutlineNode[], workKind: WritingKind, drop: TreeDrop): DropPlan | null {
  // cid 形状由后端 sanitize_outline 保证，这里只按 cid 寻址（与 moveLeaf 同约定）。
  if (!drop.dragCid || !drop.dropCid || drop.dragCid === drop.dropCid) return null
  const from = locateNode(nodes, drop.dragCid)
  const target = locateNode(nodes, drop.dropCid)
  if (!from || !target) return null

  if (workKind === 'article') {
    if (drop.placement === 'inside') return null
    if (from.container !== nodes || target.container !== nodes) return null
    return finalizePlan(from, nodes, target.index + (drop.placement === 'after' ? 1 : 0))
  }

  if (from.node.kind === 'volume') {
    if (drop.placement === 'inside') return null
    if (target.container !== nodes) return null
    return finalizePlan(from, nodes, target.index + (drop.placement === 'after' ? 1 : 0))
  }

  if (drop.placement === 'inside') {
    if (target.node.kind !== 'volume') return null
    const toContainer = target.node.children ?? []
    return finalizePlan(from, toContainer, toContainer.length)
  }

  if (target.node.kind === 'volume') {
    if (target.container !== nodes) return null
    return finalizePlan(from, nodes, target.index + (drop.placement === 'after' ? 1 : 0))
  }

  // 章对章缝隙：目标章在哪个容器就进哪个容器，天然支持跨卷。
  return finalizePlan(
    from,
    target.container,
    target.index + (drop.placement === 'after' ? 1 : 0),
  )
}

/** 同容器摘除会让后续下标前移，先校正；校正后与原位重合就是 no-op。 */
function finalizePlan(
  from: NodeLocation,
  toContainer: OutlineNode[],
  rawIndex: number,
): DropPlan | null {
  let toIndex = rawIndex
  if (from.container === toContainer && from.index < rawIndex) toIndex -= 1
  if (toIndex < 0 || toIndex > toContainer.length) return null
  if (from.container === toContainer && toIndex === from.index) return null
  return { from, toContainer, toIndex }
}

/** a-tree allowDrop 用：非法落点直接显示禁止光标；drop 事件里会再兜一道同样的判断。 */
export function canDropNode(nodes: OutlineNode[], workKind: WritingKind, drop: TreeDrop): boolean {
  return planDrop(nodes, workKind, drop) !== null
}

/** 按拖拽计划原地改树；非法或原地未动返回 false（调用方据此跳过保存请求）。 */
export function applyNodeDrop(
  nodes: OutlineNode[],
  workKind: WritingKind,
  drop: TreeDrop,
): boolean {
  const plan = planDrop(nodes, workKind, drop)
  if (!plan) return false
  const [item] = plan.from.container.splice(plan.from.index, 1)
  plan.toContainer.splice(plan.toIndex, 0, item)
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
