/** a-table 的 scroll.y 必须给具体像素表头才能固定，但容器高度随窗口/面板
 *  变化：实测容器剩余空间 = 容器高 − 表头 − 分页栏，尺寸一变就重算。
 *  从 DbDataTab 提取，DbStructureTab / RedisKeyTab 等无分页表格共用。 */
import { nextTick, onBeforeUnmount, onMounted, ref, type Ref } from 'vue'

export function useGridScrollY(
  gridWrapRef: Ref<HTMLElement | null>,
  opts: { pagination?: boolean } = {},
) {
  // 无分页的表格不能把「分页栏还没渲染」的 48px 经验值一直让出来。
  const hasPagination = opts.pagination ?? true
  const scrollY = ref(480)

  /** 元素自身高度加上下外边距（分页栏的 margin 也要让出来）。 */
  function outerHeight(el: HTMLElement): number {
    const style = getComputedStyle(el)
    return el.offsetHeight + parseFloat(style.marginTop) + parseFloat(style.marginBottom)
  }

  function measureScrollY() {
    const wrap = gridWrapRef.value
    if (!wrap) return
    const thead = wrap.querySelector<HTMLElement>('.ant-table-thead')
    const pager = hasPagination ? wrap.querySelector<HTMLElement>('.ant-pagination') : null
    // 数据还没回来、分页栏尚未渲染时用经验值兜底，避免把表格压成一褶。
    const occupied =
      (thead ? thead.offsetHeight : 40) +
      (hasPagination ? (pager ? outerHeight(pager) : 48) : 0)
    scrollY.value = Math.max(160, wrap.clientHeight - occupied)
  }

  const gridObserver = new ResizeObserver(() => measureScrollY())

  // 容器可能是 v-if 后渲染的（数据回来才出现），mount 时还不存在——
  // 每次测量前补挂观察，元素换人先卸旧的。
  let observed: HTMLElement | null = null

  function ensureObserved() {
    const wrap = gridWrapRef.value
    if (!wrap || wrap === observed) return
    if (observed) gridObserver.unobserve(observed)
    gridObserver.observe(wrap)
    observed = wrap
  }

  onMounted(async () => {
    await nextTick()
    ensureObserved()
    measureScrollY()
  })

  onBeforeUnmount(() => gridObserver.disconnect())

  // 表头是数据回来后才渲染的，容器尺寸不变、observer 不会触发——
  // 调用方在数据加载完成后（nextTick 之后）要再补一次 measureScrollY()。
  return { scrollY, measureScrollY: () => { ensureObserved(); measureScrollY() } }
}
