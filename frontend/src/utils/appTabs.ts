/**
 * 顶栏标签的路径识别：静态菜单叶 + 按 id 展开的动态标签。
 *
 * 从 App.vue 抽出来做纯函数（静态叶由调用方传入），标签体系的核心规则
 * ——哪些路径有标签、动态标签叫什么——才钉得住单测。
 */
import { PlayCircleOutlined } from '@ant-design/icons-vue'

export interface TabLeaf {
  path: string
  title: string
  icon: unknown
}

/** 与 router 里的 /workflows/runs/:id(\\d+) 同一形状。 */
export const RUN_TAB_PREFIX = '/workflows/runs/'

/**
 * 这个路径对应哪一枚标签：先查静态菜单叶，再认运行现场
 * （`/workflows/runs/<数字 id>` 一枚一枚按运行展开）；都对不上就没有标签。
 */
export function leafForPath(path: string, staticLeaves: TabLeaf[]): TabLeaf | undefined {
  const known = staticLeaves.find((leaf) => leaf.path === path)
  if (known) return known
  if (!path.startsWith(RUN_TAB_PREFIX)) return undefined
  const id = path.slice(RUN_TAB_PREFIX.length)
  return /^\d+$/.test(id)
    ? { path, title: `运行 #${id}`, icon: PlayCircleOutlined }
    : undefined
}
