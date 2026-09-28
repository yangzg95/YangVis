<template>
  <!-- 量高容器：scrollY 由它的剩余空间实时算出（与数据页签同一模式）。 -->
  <div ref="gridWrapRef" class="grid-wrap">
    <a-table
      v-if="result.columns.length"
      class="grid db-grid"
      size="small"
      :data-source="rows"
      :columns="columns"
      :pagination="false"
      :scroll="{ x: 'max-content', y: scrollY }"
      row-key="__i"
    >
      <!-- Navicat 式空表：0 行时只留表头 + 空白区，不渲染「暂无数据」占位图。 -->
      <template #emptyText>
        <div class="grid-blank" />
      </template>
      <template #headerCell="{ column }">
        <span>{{ column.title }}</span>
        <span
          class="col-resize"
          @mousedown="startResize($event, String(column.key), column.width)"
          @click.stop
          @contextmenu.stop
        />
      </template>
      <template #bodyCell="{ text, record, column }">
        <span
          :class="['cell', {
            selected: selected.record === record && selected.column === String(column.title),
            targeting: menu.open && menu.record === record && menu.column === String(column.title),
            num: typeof text === 'number',
          }]"
          @click="selectCell(record, String(column.title))"
          @dblclick="copyCell(text)"
          @contextmenu.prevent="onCellMenu($event, String(column.title), text, record)"
        >
          <span v-if="text === null || text === undefined" class="null">(Null)</span>
          <template v-else>{{ text }}</template>
          <span
            v-if="isLongValue(text)"
            class="cell-expand"
            title="查看完整内容"
            @click.stop="openViewer(String(column.title), text)"
          >
            <ExpandOutlined />
          </span>
        </span>
      </template>
    </a-table>
    <pre v-else class="text">{{ result.text ?? '(empty)' }}</pre>
    <div class="meta">
      {{ result.row_count }} 行 · {{ result.elapsed_ms }} ms
      <span v-if="result.truncated"> · 结果已截断（上限见服务端配置）</span>
    </div>

    <GridContextMenu
      :open="menu.open"
      :x="menu.x"
      :y="menu.y"
      :entries="menuEntries"
      @pick="onMenuPick"
      @close="menu.open = false"
    />

    <CellViewer v-model:open="viewer.open" :column="viewer.column" :text="viewer.text" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { message as toast } from 'ant-design-vue'
import { ExpandOutlined } from '@ant-design/icons-vue'
import GridContextMenu, { type GridMenuEntry } from './GridContextMenu.vue'
import CellViewer from './CellViewer.vue'
import {
  copyText,
  csvLine,
  downloadText,
  EXPORT_LABEL,
  EXPORT_MIME,
  EXPORT_ORDER,
  EXPORT_SUFFIX,
  exportText,
  isLongValue,
  recordValues,
  toColumns,
  toRows,
  type ExportFormat,
} from './grid'
import { useColumnResize } from './useColumnResize'
import './grid.css'

/** 单个结果集：DbExecuteResult / DbBatchStatement 结构上都满足这个形状。 */
export interface ResultGridData {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  elapsed_ms: number
  text?: string | null
}

const props = defineProps<{
  result: ResultGridData
}>()

const rows = computed(() => toRows(props.result))
// toColumns 给初始宽，列头拖拽的覆盖值由 useColumnResize 叠上去；
// 结果集没有稳定来源，按列名签名持久化（同一条查询重跑宽度还在）。
const baseColumns = computed(() => toColumns(props.result))
const { columns, startResize, autoFit } = useColumnResize(
  baseColumns,
  () => `q:${props.result.columns.join('|')}`,
)

// scroll.y 必须给具体像素表头才能固定，但容器高度随窗口/面板变化，
// 所以实测容器剩余空间 = 容器高 − 表头 − 底部行数条，尺寸一变就重算。
const gridWrapRef = ref<HTMLElement | null>(null)
const scrollY = ref(320)

function computeScrollY(): number | null {
  const wrap = gridWrapRef.value
  if (!wrap) return null
  const thead = wrap.querySelector<HTMLElement>('.ant-table-thead')
  const meta = wrap.querySelector<HTMLElement>('.meta')
  const occupied = (thead ? thead.offsetHeight : 40) + (meta ? meta.offsetHeight : 0)
  // 下限别定太高：面板被编辑器压矮时，过大的下限会把底部行数条挤出可视区。
  return Math.max(72, wrap.clientHeight - occupied)
}

function measureScrollY() {
  const px = computeScrollY()
  if (px != null) scrollY.value = px
}

// 拖拽编辑器调条时容器逐帧变化：scroll.y（prop）每变一次固定头表格就整套
// 重渲染（很卡）；推迟到松手量又让表体定格、看着像被拖着走。折中：拖拽途中
// 直接改表体 DOM 的 max-height 跟上容器（零重渲染），松手再同步一次 prop。
function liveFitBody() {
  const px = computeScrollY()
  if (px == null) return
  const body = gridWrapRef.value?.querySelector<HTMLElement>('.ant-table-body')
  if (body) body.style.maxHeight = `${px}px`
}

let measureRaf = 0
let waitingMouseUp = false

function scheduleMeasure() {
  if (document.body.classList.contains('dragging-row')) {
    liveFitBody()
    if (!waitingMouseUp) {
      waitingMouseUp = true
      window.addEventListener('mouseup', () => {
        waitingMouseUp = false
        scheduleMeasure()
      }, { once: true })
    }
    return
  }
  if (measureRaf) return
  measureRaf = requestAnimationFrame(() => {
    measureRaf = 0
    measureScrollY()
  })
}

const gridObserver = new ResizeObserver(scheduleMeasure)

onMounted(() => {
  if (gridWrapRef.value) gridObserver.observe(gridWrapRef.value)
  measureScrollY()
})

// 结果集换掉时表格和行数条重新渲染，等一拍再量。
watch(() => props.result, () => nextTick(measureScrollY))

onBeforeUnmount(() => {
  gridObserver.disconnect()
  if (measureRaf) cancelAnimationFrame(measureRaf)
})

// ---- 结果表格的右键菜单（只读复制） ------------------------------------------------
//
// 自定义 SQL 的结果集没有稳定来源，筛选/排序拼不回原语句，这里只提供复制类操作。

const menu = reactive({
  open: false,
  x: 0,
  y: 0,
  column: '',
  value: null as unknown,
  record: null as Record<string, unknown> | null,
})

const menuEntries = computed<GridMenuEntry[]>(() => [
  { key: 'copy', label: '复制' },
  { key: 'copy-name', label: '复制字段名称' },
  {
    key: 'copy-as',
    label: '复制为',
    children: [
      { key: 'copy-csv', label: 'CSV（整行）' },
      { key: 'copy-json', label: 'JSON（整行）' },
    ],
  },
  { key: 'd0', divider: true },
  {
    key: 'export',
    label: '保存数据为…',
    // 查询结果没有可靠的落点表名，INSERT 只在数据页签提供。
    children: EXPORT_ORDER.filter((fmt) => fmt !== 'insert').map((fmt) => ({
      key: `export:${fmt}`,
      label: `${EXPORT_LABEL[fmt]}（整个结果集）`,
    })),
  },
  { key: 'd1', divider: true },
  { key: 'fit:all', label: '自适应列宽' },
])

function onCellMenu(event: MouseEvent, column: string, value: unknown, record: Record<string, unknown>) {
  selectCell(record, column)
  menu.column = column
  menu.value = value
  menu.record = record
  menu.x = event.clientX
  menu.y = event.clientY
  menu.open = true
}

async function copyOrToast(text: string, what: string) {
  if (await copyText(text)) toast.success(`${what}已复制`)
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

// Navicat 式单元格交互：左键只选中（高亮），双击复制全文，右键出菜单。
const selected = reactive({
  record: null as Record<string, unknown> | null,
  column: '',
})

function selectCell(record: Record<string, unknown>, column: string) {
  selected.record = record
  selected.column = column
}

/** 双击单元格 = 复制全文（右键菜单那套复制仍然保留）。 */
function copyCell(value: unknown) {
  if (value === null || value === undefined) return
  void copyOrToast(String(value), '内容')
}

// 长内容的「展开」弹窗：一份实例复用，内容是点开那一刻快照下来的。
const viewer = reactive({ open: false, column: '', text: '' })

function openViewer(column: string, value: unknown) {
  viewer.column = column
  viewer.text = value == null ? '' : String(value)
  viewer.open = true
}

/**
 * 「保存数据为…」：导的是整个结果集（服务端已按上限截断，
 * 与数据页导「当前页」不同——这里没有分页）。
 */
function exportAs(format: ExportFormat) {
  downloadText(
    `query-result.${EXPORT_SUFFIX[format]}`,
    exportText(format, props.result),
    EXPORT_MIME[format],
  )
}

function onMenuPick(key: string) {
  const isNull = menu.value === null || menu.value === undefined
  if (key === 'copy') return void copyOrToast(isNull ? '' : String(menu.value), '内容')
  if (key === 'copy-name') return void copyOrToast(menu.column, '字段名')
  if (key === 'copy-csv' || key === 'copy-json') {
    const cols = props.result.columns
    const values = menu.record ? recordValues(menu.record, cols.length) : []
    if (key === 'copy-csv') return void copyOrToast(csvLine(values), 'CSV ')
    const obj = Object.fromEntries(cols.map((name, i) => [name, values[i] ?? null]))
    return void copyOrToast(JSON.stringify(obj, null, 2), 'JSON ')
  }
  if (key.startsWith('export:')) return exportAs(key.slice(7) as ExportFormat)
  if (key === 'fit:all') return autoFit()
  if (key === 'fit:all') return autoFit()
}
</script>

<style scoped>
/* 纵向滚动由表格自身（scroll.y）负责，外层不再滚，避免出现双滚动条。 */
.grid-wrap {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 占满整个单元格，右键点在空白处也能命中；菜单打开时高亮目标格。
   左键是选中（Navicat 式），光标用表格十字。 */
.cell {
  display: block;
  margin: -5px -12px;
  padding: 5px 12px;
  cursor: cell;
}

/* 选中格：信号色描边 + 浅底，比悬停重一档，一眼定位当前格。 */
.cell.selected {
  background: var(--signal-bg, #e6f4ff);
  outline: 2px solid var(--signal-border, #91caff);
  outline-offset: -2px;
}

.cell.targeting {
  background: var(--signal-bg, #e6f4ff);
  outline: 1px solid var(--signal-border, #91caff);
}

.null {
  color: rgba(0, 0, 0, 0.3);
  font-style: italic;
}

.text {
  flex: 1;
  min-height: 0;
  margin: 0;
  padding: 8px;
  border-radius: 6px;
  background: #fafafa;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  overflow: auto;
}

.meta {
  flex-shrink: 0;
  padding-top: 4px;
  color: var(--text-3);
  font-size: 12px;
}
</style>
