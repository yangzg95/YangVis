/** 编辑器页要打开哪条工作流：列表页跳转前写这里，编辑器页消费。
 *
 * 不用 route query 传，是因为顶栏标签只记路径：点标签切回来时 query 丢了，
 * 「编辑第 5 条」会被误读成「新建」。store 活在内存里，整页刷新后为空，
 * 编辑器页此时退回空白新建，不猜上次在编辑谁。
 */
import { ref } from 'vue'

export interface EditorTarget {
  /** null 表示新建。 */
  id: number | null
  readonly: boolean
  /** 每次「打开」请求都换新值：编辑器页靠它区分是重新装载，还是切标签回来接着改。 */
  nonce: number
}

export const editorTarget = ref<EditorTarget | null>(null)

export function requestEditor(id: number | null, readonly = false) {
  editorTarget.value = { id, readonly, nonce: Date.now() }
}
