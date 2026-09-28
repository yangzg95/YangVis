// 列宽拖拽：toColumns 按内容算出的是初始宽（夹在 96–320），用户在列头右缘
// 拖动后，这一列以拖拽值为准。拖拽值还要同时写成 td/th 的 max-width 内联
// 样式——grid.css 里有 320px 的列宽 clamp，不写内联的话拖宽的列会被压回去。
//
// 传入 storageKey 时覆盖值按「表格标识 → {列key: 宽度}」持久化到
// localStorage（松手才写），刷新/重开页签后拖好的宽度还在。查询结果页
// 同一个组件实例会换结果集，key 允许传 getter，读取/落库都按当下取值。

import { computed, reactive, type ComputedRef } from 'vue'
import { clampSize, startDragResize } from './resizer'
import { storageKeys } from '@/utils/storage'

interface GridColumn {
  key: string
  width?: number
  [key: string]: unknown
}

function readAll(): Record<string, Record<string, number>> {
  try {
    return JSON.parse(localStorage.getItem(storageKeys.opsDbColWidths) || '{}')
  } catch {
    return {}
  }
}

export function useColumnResize(
  base: ComputedRef<GridColumn[]>,
  storageKey?: string | (() => string),
) {
  const keyOf = typeof storageKey === 'function' ? storageKey : () => storageKey

  // 按列 key 记覆盖值：刷新/翻页后列 key 不变，拖好的宽度留得住。
  const overrides = reactive<Record<string, number>>({ ...readAll()[keyOf() ?? ''] })

  const columns = computed(() =>
    base.value.map((col) => {
      const width = overrides[col.key]
      if (!width) return col
      const style = { maxWidth: `${width}px` }
      return {
        ...col,
        width,
        customHeaderCell: () => ({ style }),
        customCell: () => ({ style }),
      }
    }),
  )

  function persist() {
    const key = keyOf()
    if (!key) return
    const all = readAll()
    all[key] = { ...overrides }
    localStorage.setItem(storageKeys.opsDbColWidths, JSON.stringify(all))
  }

  function startResize(event: MouseEvent, key: string, width?: number) {
    event.stopPropagation()
    const startWidth = overrides[key] ?? width ?? 96
    startDragResize(
      event,
      'x',
      (delta) => {
        overrides[key] = clampSize(startWidth + delta, 60, 800)
      },
      persist,
    )
  }

  /**
   * 「自适应列宽」：丢掉手动拖出来的覆盖值，回到 toColumns 按内容算出的那一档。
   * 不传 key 就是把这张表的所有列一起还原。
   */
  function autoFit(key?: string) {
    if (key) delete overrides[key]
    else for (const col of base.value) delete overrides[col.key]
    persist()
  }

  return { columns, startResize, autoFit }
}
