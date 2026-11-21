/**
 * 顶栏标签的路径识别：静态菜单叶 + 按 id 展开的动态标签。
 *
 * 从 App.vue 抽出来做纯函数（静态叶由调用方传入），标签体系的核心规则
 * ——哪些路径有标签、动态标签叫什么——才钉得住单测。
 */
import {
  EditOutlined,
  FieldTimeOutlined,
  PlayCircleOutlined,
  SwapOutlined,
  VideoCameraOutlined,
} from '@ant-design/icons-vue'

export interface TabLeaf {
  path: string
  title: string
  icon: unknown
}

/** 与 router 里的 /workflows/runs/:id(\\d+) 同一形状。 */
export const RUN_TAB_PREFIX = '/workflows/runs/'

/** 与 router 里的 /workflows/:id(\\d+)/runs 同一形状：一条流程的整页运行记录。 */
const RUNS_PAGE_RE = /^\/workflows\/(\d+)\/runs$/

/** 与 router 里的 /workflows/compare/:a(\\d+)/:b(\\d+)? 同一形状：双运行对比。 */
const COMPARE_RE = /^\/workflows\/compare\/(\d+)(?:\/(\d+))?$/

/** 与 router 里的 /office/videos/:id(\\d+)/workbench 同一形状：一条片子的工作台。 */
const VIDEO_WORKBENCH_RE = /^\/office\/videos\/(\d+)\/workbench$/

/** 与 router 里的 /studio/works/:id(\\d+)/workbench 同一形状：一部创作作品的工作台。 */
const STUDIO_WORKBENCH_RE = /^\/studio\/works\/(\d+)\/workbench$/

/**
 * 这个路径对应哪一枚标签：先查静态菜单叶，再认五类按 id 展开的动态标签——
 * 运行现场（`/workflows/runs/<id>`）、流程的运行记录（`/workflows/<id>/runs`）、
 * 双运行对比（`/workflows/compare/<a>[/<b>]`）、短视频工作台
 * （`/office/videos/<id>/workbench`）与创作工作台
 * （`/studio/works/<id>/workbench`）；都对不上就没有标签。
 */
export function leafForPath(path: string, staticLeaves: TabLeaf[]): TabLeaf | undefined {
  const known = staticLeaves.find((leaf) => leaf.path === path)
  if (known) return known
  if (path.startsWith(RUN_TAB_PREFIX)) {
    const id = path.slice(RUN_TAB_PREFIX.length)
    return /^\d+$/.test(id)
      ? { path, title: `运行 #${id}`, icon: PlayCircleOutlined }
      : undefined
  }
  const runsPage = RUNS_PAGE_RE.exec(path)
  if (runsPage) {
    return { path, title: `运行记录 #${runsPage[1]}`, icon: FieldTimeOutlined }
  }
  const compare = COMPARE_RE.exec(path)
  if (compare) {
    const [, a, b] = compare
    return {
      path,
      title: b ? `运行对比 #${a} / #${b}` : `运行对比 #${a}`,
      icon: SwapOutlined,
    }
  }
  const workbench = VIDEO_WORKBENCH_RE.exec(path)
  if (workbench) {
    return { path, title: `短视频 #${workbench[1]}`, icon: VideoCameraOutlined }
  }
  const studio = STUDIO_WORKBENCH_RE.exec(path)
  if (studio) {
    return { path, title: `创作 #${studio[1]}`, icon: EditOutlined }
  }
  return undefined
}
