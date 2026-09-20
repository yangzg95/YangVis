<template>
  <div class="tab-page">
    <div class="toolbar">
      <span class="crumb">
        <FolderOutlined /> {{ schema }} <span class="sep">/</span>
        <TableOutlined /> {{ table }}
      </span>
      <span class="spacer" />
      <span v-if="result" class="meta">
        共 {{ result.total }} 行 · {{ result.elapsed_ms }} ms
      </span>
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load()">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <!-- 手输条件栏：回车/点应用生效，文本保留在框里表示它正在生效。
         右键菜单「显示/隐藏筛选 & 排序」可以把整条栏收起来腾空间。 -->
    <div v-if="condBarVisible" class="where-bar">
      <a-input
        v-model:value="whereInput"
        size="small"
        allow-clear
        placeholder="手动输入筛选条件，回车生效，如：status = 1 AND name LIKE '%张%'"
        @keydown.enter="applyWhere"
      >
        <template #prefix><FilterOutlined class="where-icon" /></template>
      </a-input>
      <a-button size="small" :disabled="whereInput.trim() === appliedWhere" @click="applyWhere">
        应用
      </a-button>
    </div>

    <!-- 右键菜单加上去的筛选/排序，以可关闭的标签平铺在表格上方。 -->
    <div v-if="condBarVisible && (filters.length || sorts.length || appliedWhere)" class="active-bar">
      <a-tag
        v-if="appliedWhere"
        class="cond-tag"
        color="orange"
        closable
        @close="clearWhere"
      >
        {{ appliedWhere }}
      </a-tag>
      <a-tag
        v-for="(f, i) in filters"
        :key="`f${i}`"
        class="cond-tag"
        color="blue"
        closable
        @close="removeFilter(i)"
      >
        {{ filterLabel(f) }}
      </a-tag>
      <a-tag
        v-for="(s, i) in sorts"
        :key="`s${i}`"
        class="cond-tag"
        color="green"
        closable
        @close="removeSort(i)"
      >
        {{ s.column }} {{ s.direction === 'asc' ? '↑' : '↓' }}
      </a-tag>
      <a-button size="small" type="link" danger @click="clearConditions">清除全部</a-button>
    </div>

    <!-- 量高容器：scrollY 由它的剩余空间实时算出，窗口/面板怎么变表格都填满。 -->
    <div ref="gridWrapRef" class="grid-wrap">
      <a-table
        class="grid db-grid"
        size="small"
        :data-source="rows"
        :columns="columns"
        :loading="loading"
        :pagination="pagination"
        :scroll="{ x: 'max-content', y: scrollY }"
        row-key="__i"
        @change="onChange"
      >
      <!-- Navicat 式空表：0 行时只留表头 + 空白区，不渲染「暂无数据」占位图。 -->
      <template #emptyText>
        <div class="grid-blank" />
      </template>
      <template #headerCell="{ column }">
        <span class="col-head">
          <span class="col-name">{{ column.title }}</span>
          <span v-if="sortOf(String(column.title))" class="col-sort">
            {{ sortOf(String(column.title)) === 'asc' ? '↑' : '↓' }}<template v-if="sorts.length > 1">{{ sortIndex(String(column.title)) }}</template>
          </span>
          <a-button
            size="small"
            type="text"
            class="col-btn"
            @click.stop="onHeaderMenu($event, String(column.title))"
          >
            <DownOutlined />
          </a-button>
        </span>
        <span
          class="col-resize"
          @mousedown="startResize($event, String(column.key), column.width)"
          @click.stop
          @contextmenu.stop
        />
      </template>
      <template #bodyCell="{ text, record, column }">
        <!-- 可编辑时单击直接进入行内编辑：输入框替换单元格内容，Enter/失焦提交，Esc 取消。 -->
        <a-input
          v-if="editing.open && editing.record === record && editing.column === String(column.title)"
          ref="editInputRef"
          v-model:value="editing.value"
          size="small"
          class="cell-input"
          @keydown="onEditKeydown"
          @blur="commitEdit"
        />
        <span
          v-else
          :class="['cell', {
            selected: selected.record === record && selected.column === String(column.title),
            targeting: menu.open && menu.record === record && menu.column === String(column.title),
            num: typeof text === 'number',
          }]"
          @click="onCellClick(record, String(column.title), text)"
          @dblclick="onCellDblClick(text)"
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
    </div>

    <!-- Navicat 式底部栏：展示产生当前页数据的那条 SELECT（服务端代回字面量后返回）。 -->
    <div v-if="result?.sql" class="sql-bar">
      <span class="sql-bar-label">SQL</span>
      <a-tooltip :title="result.sql" :mouse-enter-delay="0.3">
        <span class="sql-bar-text">{{ result.sql }}</span>
      </a-tooltip>
      <a-tooltip title="复制 SQL">
        <a-button size="small" type="text" class="sql-bar-copy" @click="copyExecutedSql">
          <CopyOutlined />
        </a-button>
      </a-tooltip>
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

    <CellEditor
      v-model:open="editor.open"
      :column="editor.column"
      :text="editor.text"
      :saving="savingCell"
      @save="onEditorSave"
    />

    <a-modal
      v-model:open="customFilter.open"
      title="自定义筛选"
      ok-text="添加"
      cancel-text="取消"
      @ok="submitCustomFilter"
    >
      <a-form layout="vertical">
        <a-form-item label="字段">
          <a-input :value="menu.column" disabled />
        </a-form-item>
        <a-form-item label="条件">
          <a-select v-model:value="customFilter.op" :options="CUSTOM_OP_OPTIONS" />
        </a-form-item>
        <a-form-item v-if="!NO_VALUE_OPS.has(customFilter.op)" label="值">
          <a-input v-model:value="customFilter.value" placeholder="按字符串匹配，数字列会自动转换" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  CopyOutlined,
  DownOutlined,
  ExpandOutlined,
  FilterOutlined,
  FolderOutlined,
  ReloadOutlined,
  TableOutlined,
} from '@ant-design/icons-vue'
import { message as toast, Modal } from 'ant-design-vue'
import {
  opsApi,
  type DbRowFilter,
  type DbRowFilterOp,
  type DbRowKey,
  type DbRowSort,
  type DbRowsResult,
} from '@/api'
import { useAuthStore } from '@/stores/auth'
import GridContextMenu, { type GridMenuEntry } from './GridContextMenu.vue'
import CellViewer from './CellViewer.vue'
import CellEditor from './CellEditor.vue'
import { copyText, csvLine, downloadText, isLongValue, recordValues, toColumns, toRows } from './grid'
import { useColumnResize } from './useColumnResize'
import './grid.css'

const props = defineProps<{
  connId: number
  schema: string
  table: string
  /** 连接的「允许写入」开关；与用户 ops_write 通行证一起决定写操作是否可用。 */
  writable: boolean
}>()

const auth = useAuthStore()
// 写操作的前台置灰依据；真正的闸门在后端（writable × can_ops_write 双判定）。
const canEdit = computed(() => props.writable && auth.canOpsWrite)
const condBarVisible = ref(true)

const result = ref<DbRowsResult | null>(null)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(100)
const error = ref('')

const filters = ref<DbRowFilter[]>([])
const sorts = ref<DbRowSort[]>([])

/** 手输条件：whereInput 是框里的草稿，appliedWhere 是真正生效的那一份。 */
const whereInput = ref('')
const appliedWhere = ref('')

const rows = computed(() => (result.value ? toRows(result.value) : []))
// toColumns 给初始宽，列头拖拽的覆盖值由 useColumnResize 叠上去；
// 按「连接.库.表」持久化，重开页签宽度不丢。
const baseColumns = computed(() => (result.value ? toColumns(result.value) : []))
const { columns, startResize } = useColumnResize(
  baseColumns,
  `t:${props.connId}:${props.schema}:${props.table}`,
)

// scroll.y 必须给具体像素表头才能固定，但容器高度随窗口/面板变化，
// 所以实测容器剩余空间 = 容器高 − 表头 − 分页栏，尺寸一变就重算。
const gridWrapRef = ref<HTMLElement | null>(null)
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
  const pager = wrap.querySelector<HTMLElement>('.ant-pagination')
  // 数据还没回来、分页栏尚未渲染时用经验值兜底，避免把表格压成一褶。
  const occupied = (thead ? thead.offsetHeight : 40) + (pager ? outerHeight(pager) : 48)
  scrollY.value = Math.max(160, wrap.clientHeight - occupied)
}

const gridObserver = new ResizeObserver(() => measureScrollY())

const pagination = computed(() => ({
  current: page.value,
  pageSize: pageSize.value,
  total: result.value?.total ?? 0,
  showSizeChanger: true,
  showQuickJumper: true,
  pageSizeOptions: ['50', '100', '200', '500'],
  size: 'small' as const,
  showTotal: (total: number) => `共 ${total} 行`,
}))

async function load(targetPage = page.value, targetSize = pageSize.value) {
  loading.value = true
  error.value = ''
  try {
    const res = await opsApi.dbRows(
      props.connId,
      props.schema,
      props.table,
      targetPage,
      targetSize,
      filters.value,
      sorts.value,
      appliedWhere.value,
    )
    result.value = res
    page.value = res.page
    pageSize.value = res.page_size
    // 分页栏首次渲染出来后才占高度，等量一下再重算表格体高度。
    await nextTick()
    measureScrollY()
  } catch (err: any) {
    error.value = err?.message || '加载失败'
    toast.error(error.value)
  } finally {
    loading.value = false
  }
}

function onChange(pag: { current?: number; pageSize?: number }) {
  // 改了每页条数就回到第一页，否则当前页码可能直接超出范围。
  const size = pag.pageSize ?? pageSize.value
  const target = size !== pageSize.value ? 1 : pag.current ?? 1
  load(target, size)
}

onMounted(async () => {
  if (gridWrapRef.value) gridObserver.observe(gridWrapRef.value)
  await nextTick()
  measureScrollY()
  load(1, pageSize.value)
})

onBeforeUnmount(() => gridObserver.disconnect())

// ---- 筛选 & 排序 ---------------------------------------------------------------

const OP_TEXT: Record<DbRowFilterOp, string> = {
  eq: '=',
  ne: '!=',
  like: 'LIKE',
  not_like: 'NOT LIKE',
  lt: '<',
  lte: '<=',
  gt: '>',
  gte: '>=',
  is_null: 'IS NULL',
  is_not_null: 'IS NOT NULL',
}

const NO_VALUE_OPS = new Set<DbRowFilterOp>(['is_null', 'is_not_null'])

const CUSTOM_OP_OPTIONS = (Object.keys(OP_TEXT) as DbRowFilterOp[]).map((op) => ({
  value: op,
  label: OP_TEXT[op],
}))

function filterLabel(f: DbRowFilter): string {
  if (NO_VALUE_OPS.has(f.op)) return `${f.column} ${OP_TEXT[f.op]}`
  const value = f.op === 'like' || f.op === 'not_like' ? `%${f.value ?? ''}%` : (f.value ?? '')
  return `${f.column} ${OP_TEXT[f.op]} ${value}`
}

/** 条件一变就回第一页：旧页码在新结果集里多半没意义。 */
function addFilter(filter: DbRowFilter) {
  filters.value = [...filters.value, filter]
  load(1, pageSize.value)
}

function removeFilter(index: number) {
  filters.value = filters.value.filter((_, i) => i !== index)
  load(1, pageSize.value)
}

function setSort(column: string, direction: 'asc' | 'desc') {
  const rest = sorts.value.filter((s) => s.column !== column)
  sorts.value = [...rest, { column, direction }]
  load(1, pageSize.value)
}

function removeSort(index: number) {
  sorts.value = sorts.value.filter((_, i) => i !== index)
  load(1, pageSize.value)
}

const sortOf = (column: string) => sorts.value.find((s) => s.column === column)?.direction

/** 多列排序时在表头标注第几键（1 起），单列排序不标。 */
const sortIndex = (column: string) => sorts.value.findIndex((s) => s.column === column) + 1

function clearConditions() {
  filters.value = []
  sorts.value = []
  whereInput.value = ''
  appliedWhere.value = ''
  load(1, pageSize.value)
}

/** 手输条件：回车/应用时生效并回第一页；校验失败（只读网关或语法错）由 load 弹错。 */
function applyWhere() {
  appliedWhere.value = whereInput.value.trim()
  load(1, pageSize.value)
}

function clearWhere() {
  whereInput.value = ''
  appliedWhere.value = ''
  load(1, pageSize.value)
}

// ---- 右键菜单 ------------------------------------------------------------------

const menu = reactive({
  open: false,
  x: 0,
  y: 0,
  /** cell = 单元格右键（筛选带值）；header = 列头按钮（只有排序和自定义筛选）。 */
  mode: 'cell' as 'cell' | 'header',
  column: '',
  value: null as unknown,
  record: null as Record<string, unknown> | null,
})

const customFilter = reactive({
  open: false,
  op: 'eq' as DbRowFilterOp,
  value: '',
})

/** 菜单项里展示的值：截断防长串把菜单撑爆。 */
function shortValue(value: unknown): string {
  const text = value == null ? '' : String(value)
  return text.length > 24 ? `${text.slice(0, 24)}…` : text
}

const menuEntries = computed<GridMenuEntry[]>(() => {
  const col = menu.column
  const sorted = sorts.value.some((s) => s.column === col)

  // 列头按钮：只给排序和自定义筛选，没有单元格值可用。
  if (menu.mode === 'header') {
    return [
      { key: 'sort:asc', label: '升序排序' },
      { key: 'sort:desc', label: '降序排序' },
      { key: 'sort:clear', label: '移除该列排序', disabled: !sorted },
      { key: 'sort:clear-all', label: '移除所有排序', disabled: !sorts.value.length },
      { key: 'dh', divider: true },
      { key: 'filter:custom', label: '添加筛选…' },
    ]
  }

  const value = menu.value
  const isNull = value === null || value === undefined
  const shown = `'${shortValue(value)}'`
  // 写操作项按 Navicat 的顺序放在菜单头部；没权限时置灰而不是藏掉，
  // 让「为什么点不了」一眼可见（连接没开写开关，或账号没有写通行证）。
  const locked = !canEdit.value
  return [
    { key: 'edit:empty', label: '设置为空白字符串', disabled: locked || value === '' },
    { key: 'edit:null', label: '设置为 NULL', disabled: locked || isNull },
    { key: 'edit:open', label: '在单元格编辑器中编辑', disabled: locked },
    { key: 'd0', divider: true },
    { key: 'edit:delete-row', label: '删除记录', danger: true, disabled: locked },
    { key: 'd1', divider: true },
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
    { key: 'paste', label: '粘贴', disabled: locked },
    { key: 'd2', divider: true },
    {
      key: 'export',
      label: '保存数据为…',
      children: [
        { key: 'export:csv', label: 'CSV（当前页）' },
        { key: 'export:json', label: 'JSON（当前页）' },
      ],
    },
    { key: 'd3', divider: true },
    { key: 'toggle-cond', label: condBarVisible.value ? '隐藏筛选 & 排序栏' : '显示筛选 & 排序栏' },
    {
      key: 'filter',
      label: '筛选',
      children: [
        { key: 'filter:eq', label: isNull ? `${col} IS NULL` : `${col} = ${shown}` },
        { key: 'filter:ne', label: isNull ? `${col} IS NOT NULL` : `${col} != ${shown}` },
        { key: 'filter:like', label: `${col} 类似 %${shortValue(value)}%`, disabled: isNull },
        { key: 'filter:not_like', label: `${col} 不类似 %${shortValue(value)}%`, disabled: isNull },
        { key: 'filter:lt', label: `${col} < ${shown}`, disabled: isNull },
        { key: 'filter:gt', label: `${col} > ${shown}`, disabled: isNull },
        { key: 'd4', divider: true },
        { key: 'filter:custom', label: '自定义筛选…' },
      ],
    },
    {
      key: 'sort',
      label: '排序',
      children: [
        { key: 'sort:asc', label: `${col} 升序` },
        { key: 'sort:desc', label: `${col} 降序` },
        { key: 'sort:clear', label: '移除该列排序', disabled: !sorted },
      ],
    },
    { key: 'd5', divider: true },
    {
      key: 'clear-all',
      label: '移除所有筛选 & 排序',
      disabled: !filters.value.length && !sorts.value.length && !appliedWhere.value,
    },
    { key: 'refresh', label: '刷新' },
  ]
})

function onCellMenu(event: MouseEvent, column: string, value: unknown, record: Record<string, unknown>) {
  selectCell(record, column)
  menu.mode = 'cell'
  menu.column = column
  menu.value = value
  menu.record = record
  menu.x = event.clientX
  menu.y = event.clientY
  menu.open = true
}

/** 列头按钮：菜单贴在按钮下沿，比鼠标位置好看。 */
function onHeaderMenu(event: MouseEvent, column: string) {
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  menu.mode = 'header'
  menu.column = column
  menu.value = null
  menu.record = null
  menu.x = rect.left
  menu.y = rect.bottom + 4
  menu.open = true
}

async function copyOrToast(text: string, what: string) {
  if (await copyText(text)) toast.success(`${what}已复制`)
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

/** 底部 SQL 栏的复制按钮。 */
function copyExecutedSql() {
  if (result.value?.sql) void copyOrToast(result.value.sql, 'SQL ')
}

// Navicat 式单元格交互：左键选中（高亮），双击复制全文，右键出菜单。
const selected = reactive({
  record: null as Record<string, unknown> | null,
  column: '',
})

function selectCell(record: Record<string, unknown>, column: string) {
  selected.record = record
  selected.column = column
}

/** 只读时双击单元格 = 复制全文（右键菜单那套复制仍然保留）。 */
function copyCell(value: unknown) {
  if (value === null || value === undefined) return
  void copyOrToast(String(value), '内容')
}

/**
 * 单元格单击：可编辑（连接开了写开关 + 账号有写通行证）时直接进入行内编辑；
 * 只读时只是选中（Navicat 式），复制挪到双击和右键菜单。
 */
function onCellClick(record: Record<string, unknown>, column: string, value: unknown) {
  if (canEdit.value) startEdit(record, column, value)
  else selectCell(record, column)
}

/** 双击复制只在只读时生效：可编辑时第一击已进行内编辑，第二击落在输入框上。 */
function onCellDblClick(value: unknown) {
  if (!canEdit.value) copyCell(value)
}

// 长内容的「展开」弹窗：一份实例复用，内容是点开那一刻快照下来的。
const viewer = reactive({ open: false, column: '', text: '' })

function openViewer(column: string, value: unknown) {
  viewer.column = column
  viewer.text = value == null ? '' : String(value)
  viewer.open = true
}

// ---- 行级写（改值 / 删除记录） --------------------------------------------------
//
// 前端不拼 SQL：行定位靠把「整行原值」回传给服务端，服务端有主键取主键、
// 没主键退化为全列 NULL 安全匹配；新值原样传输，类型交给 MySQL 隐式转换。

/** 整行原值 → {列名: 值} 的行定位映射。 */
function rowKey(record: Record<string, unknown>): DbRowKey {
  const cols = result.value?.columns ?? []
  const values = recordValues(record, cols.length)
  return Object.fromEntries(cols.map((name, i) => [name, values[i] ?? null]))
}

const savingCell = ref(false)

/** 提交一个单元格的新值。成功后就地补丁本页数据，不整页刷新（滚动位置不动）。 */
async function submitEdit(
  record: Record<string, unknown>,
  column: string,
  value: unknown,
): Promise<boolean> {
  if (!result.value || !record) return false
  savingCell.value = true
  try {
    await opsApi.updateRow(props.connId, props.schema, props.table, rowKey(record), {
      [column]: value,
    })
    const ci = result.value.columns.indexOf(column)
    const ri = Number(record.__i)
    if (ci >= 0 && result.value.rows[ri]) result.value.rows[ri][ci] = value
    toast.success('已更新')
    return true
  } catch (err: any) {
    toast.error(err?.message || '更新失败')
    return false
  } finally {
    savingCell.value = false
  }
}

/** 删除记录：先弹确认（摘要前几个列值帮助确认没点错行），成功后刷新当前页。 */
function confirmDeleteRow(record: Record<string, unknown>) {
  const cols = result.value?.columns ?? []
  const values = recordValues(record, cols.length)
  const summary = cols
    .slice(0, 4)
    .map((name, i) => `${name}=${values[i] == null ? 'NULL' : shortValue(values[i])}`)
    .join('，')
  Modal.confirm({
    title: '删除记录',
    content: `确定从 ${props.schema}.${props.table} 删除这条记录吗？（${summary}${cols.length > 4 ? '…' : ''}）`,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    async onOk() {
      try {
        await opsApi.deleteRow(props.connId, props.schema, props.table, rowKey(record))
        toast.success('已删除')
        await load()
      } catch (err: any) {
        toast.error(err?.message || '删除失败')
      }
    },
  })
}

/** 粘贴：读系统剪贴板写进当前单元格，浏览器拒了就让用户去授权。 */
async function pasteInto(record: Record<string, unknown> | null, column: string) {
  if (!record) return
  try {
    const text = await navigator.clipboard.readText()
    await submitEdit(record, column, text)
  } catch {
    toast.error('读取剪贴板失败，请在浏览器里允许剪贴板访问')
  }
}

/** 「保存数据为…」：导出当前页（服务端分页，导的是正在看的这一页）。 */
function exportCsv() {
  if (!result.value) return
  const lines = [csvLine(result.value.columns), ...result.value.rows.map((r) => csvLine(r))]
  downloadText(`${props.table}.csv`, lines.join('\r\n'), 'text/csv')
}

function exportJson() {
  if (!result.value) return
  const cols = result.value.columns
  const data = result.value.rows.map((row) =>
    Object.fromEntries(cols.map((name, i) => [name, row[i] ?? null])),
  )
  downloadText(`${props.table}.json`, JSON.stringify(data, null, 2), 'application/json')
}

// ---- 行内编辑（可编辑时单击单元格进入） --------------------------------------------

const editing = reactive({
  open: false,
  record: null as Record<string, unknown> | null,
  column: '',
  value: '',
})
const editInputRef = ref()

function startEdit(record: Record<string, unknown>, column: string, value: unknown) {
  if (!canEdit.value) return
  editing.record = record
  editing.column = column
  editing.value = value == null ? '' : String(value)
  editing.open = true
  void nextTick(() => editInputRef.value?.focus())
}

function cancelEdit() {
  editing.open = false
}

/** Enter 提交、Esc 取消；中文输入法组词中的 Enter 不能算提交。 */
function onEditKeydown(event: KeyboardEvent) {
  if (event.isComposing) return
  if (event.key === 'Enter') {
    event.preventDefault()
    void commitEdit()
  } else if (event.key === 'Escape') {
    event.preventDefault()
    cancelEdit()
  }
}

async function commitEdit() {
  if (!editing.open) return
  const { record, column, value } = editing
  editing.open = false
  if (!record || !result.value) return
  // 没改动的提交（点进去又直接回车/点走）不发请求。
  const ci = result.value.columns.indexOf(column)
  const original = ci >= 0 ? result.value.rows[Number(record.__i)]?.[ci] : undefined
  if (value === (original == null ? '' : String(original))) return
  // 行内编辑只能给出字符串；设为 NULL / 空白字符串走右键菜单。
  await submitEdit(record, column, value)
}

// ---- 单元格编辑器弹窗 -----------------------------------------------------------

const editor = reactive({
  open: false,
  column: '',
  text: null as string | null,
  record: null as Record<string, unknown> | null,
})

function openEditor(record: Record<string, unknown> | null, column: string, value: unknown) {
  editor.record = record
  editor.column = column
  editor.text = value == null ? null : String(value)
  editor.open = true
}

async function onEditorSave(value: string | null) {
  if (!editor.record) {
    editor.open = false
    return
  }
  if (await submitEdit(editor.record, editor.column, value)) editor.open = false
}

function onMenuPick(key: string) {
  const col = menu.column
  const value = menu.value
  const isNull = value === null || value === undefined
  const text = isNull ? '' : String(value)

  if (key === 'edit:empty') return void submitEdit(menu.record!, col, '')
  if (key === 'edit:null') return void submitEdit(menu.record!, col, null)
  if (key === 'edit:open') return openEditor(menu.record, col, value)
  if (key === 'edit:delete-row') return confirmDeleteRow(menu.record!)
  if (key === 'paste') return void pasteInto(menu.record, col)
  if (key === 'export:csv') return exportCsv()
  if (key === 'export:json') return exportJson()
  if (key === 'toggle-cond') {
    condBarVisible.value = !condBarVisible.value
    return
  }

  if (key === 'copy') return void copyOrToast(text, '内容')
  if (key === 'copy-name') return void copyOrToast(col, '字段名')
  if (key === 'copy-csv' || key === 'copy-json') {
    const cols = result.value?.columns ?? []
    const values = menu.record ? recordValues(menu.record, cols.length) : []
    if (key === 'copy-csv') return void copyOrToast(csvLine(values), 'CSV ')
    const obj = Object.fromEntries(cols.map((name, i) => [name, values[i] ?? null]))
    return void copyOrToast(JSON.stringify(obj, null, 2), 'JSON ')
  }

  if (key === 'filter:custom') {
    customFilter.op = 'eq'
    customFilter.value = isNull ? '' : String(value)
    customFilter.open = true
    return
  }
  if (key.startsWith('filter:')) {
    const op = key.slice(7) as DbRowFilterOp
    if (isNull && op === 'eq') return addFilter({ column: col, op: 'is_null' })
    if (isNull && op === 'ne') return addFilter({ column: col, op: 'is_not_null' })
    return addFilter({ column: col, op, value: text })
  }

  if (key === 'sort:asc') return setSort(col, 'asc')
  if (key === 'sort:desc') return setSort(col, 'desc')
  if (key === 'sort:clear') {
    sorts.value = sorts.value.filter((s) => s.column !== col)
    return load(1, pageSize.value)
  }
  if (key === 'sort:clear-all') {
    sorts.value = []
    return load(1, pageSize.value)
  }

  if (key === 'clear-all') return clearConditions()
  if (key === 'refresh') return void load()
}

function submitCustomFilter() {
  const op = customFilter.op
  addFilter({
    column: menu.column,
    op,
    value: NO_VALUE_OPS.has(op) ? null : customFilter.value,
  })
  customFilter.open = false
}
</script>

<style scoped>
.tab-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* flex 基底用全局 .toolbar（style.css），ops 面板内保持更紧凑的间距。 */
.toolbar {
  gap: 8px;
  margin-bottom: 0;
  padding-bottom: 8px;
}

.crumb {
  font-size: 13px;
  color: rgba(0, 0, 0, 0.65);
}

.sep {
  margin: 0 4px;
  color: rgba(0, 0, 0, 0.25);
}

.spacer {
  flex: 1;
}

.meta {
  color: var(--text-3);
  font-size: 12px;
}

.where-bar {
  display: flex;
  gap: 8px;
  padding-bottom: 8px;
}

.where-bar :deep(input) {
  font-family: var(--font-mono);
  font-size: 12px;
}

.where-icon {
  color: var(--text-3);
}

.active-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  padding-bottom: 8px;
}

.cond-tag {
  margin-inline-end: 0;
  font-family: var(--font-mono);
  font-size: 12px;
}

.grid-wrap {
  flex: 1;
  min-height: 0;
  /* 纵向滚动由表格自身（scroll.y）负责，外层不再滚，避免出现双滚动条。 */
  overflow: hidden;
}

/* 占满整个单元格，右键点在空白处也能命中；菜单打开时高亮目标格。
   左键是选中（Navicat 式），光标统一用表格十字。 */
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

/* 行内编辑框：与 .cell 同样的负边距手法撑满整个单元格。 */
.cell-input {
  display: block;
  margin: -5px -12px;
  width: calc(100% + 24px);
  font-family: var(--font-mono);
  font-size: 12px;
}

.cell.targeting {
  background: var(--signal-bg, #e6f4ff);
  outline: 1px solid var(--signal-border, #91caff);
}

.null {
  color: rgba(0, 0, 0, 0.3);
  font-style: italic;
}

/* 列头：字段名 + 排序态标记 + 操作按钮，按钮平时收起、悬停露出。 */
.col-head {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  max-width: 100%;
}

.col-name {
  overflow: hidden;
  text-overflow: ellipsis;
}

.col-sort {
  flex-shrink: 0;
  color: var(--signal-text, #1677ff);
  font-size: 11px;
}

.col-btn {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  min-width: 18px;
  padding: 0;
  font-size: 10px;
  color: var(--text-3);
}

.col-head:hover .col-btn {
  color: var(--signal-text, #1677ff);
}
</style>
