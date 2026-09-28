<template>
  <div class="tab-page">
    <div class="toolbar">
      <span class="crumb">
        <FolderOutlined /> {{ schema }} <span class="sep">/</span>
        <TableOutlined /> {{ table }}
        <span v-if="engine" class="engine">{{ engine }}</span>
      </span>
      <span class="spacer" />
      <a-tooltip title="丢弃未保存的修改，重新读一次当前结构">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-alert
      v-if="!canWrite"
      class="gate-alert"
      type="info"
      show-icon
      message="只读模式"
      description="设计表要执行 ALTER：连接台账需开启「允许写入」，当前账号还需持有运维写权限。二者缺一时这里只看得结构，改不了。"
    />

    <div class="form-wrap">
      <div class="section-head">
        <span class="section-title">字段</span>
        <a-button size="small" :disabled="!canWrite" @click="addColumn">
          <PlusOutlined /> 新增列
        </a-button>
      </div>
      <a-table
        class="db-grid design-grid"
        size="small"
        :data-source="rows"
        :columns="COLUMN_GRID"
        :loading="loading"
        :pagination="false"
        row-key="key"
        :scroll="{ x: 'max-content' }"
      >
        <template #bodyCell="{ column, record, index }">
          <template v-if="column.key === 'seq'">{{ index + 1 }}</template>

          <template v-else-if="column.key === 'name'">
            <a-input
              v-model:value="record.name"
              size="small"
              :disabled="!canWrite"
              :maxlength="64"
              placeholder="列名"
            />
          </template>

          <template v-else-if="column.key === 'column_type'">
            <a-auto-complete
              v-model:value="record.column_type"
              size="small"
              :disabled="!canWrite"
              :options="typeOptions(record.column_type)"
              :maxlength="128"
              placeholder="varchar(64)"
            />
          </template>

          <template v-else-if="column.key === 'primary'">
            <a-checkbox
              :checked="record.primary"
              :disabled="!canWrite"
              @change="(e: any) => togglePrimary(record, e.target.checked)"
            />
          </template>

          <template v-else-if="column.key === 'nullable'">
            <a-checkbox v-model:checked="record.nullable" :disabled="!canWrite" />
          </template>

          <template v-else-if="column.key === 'default'">
            <div class="default-cell">
              <a-checkbox v-model:checked="record.has_default" :disabled="!canWrite" />
              <a-input
                v-model:value="record.default"
                size="small"
                :disabled="!canWrite || !record.has_default"
                placeholder="NULL"
              />
            </div>
          </template>

          <template v-else-if="column.key === 'auto_increment'">
            <a-checkbox v-model:checked="record.auto_increment" :disabled="!canWrite" />
          </template>

          <template v-else-if="column.key === 'on_update'">
            <a-checkbox v-model:checked="record.on_update" :disabled="!canWrite" />
          </template>

          <template v-else-if="column.key === 'comment'">
            <a-input
              v-model:value="record.comment"
              size="small"
              :disabled="!canWrite"
              :maxlength="1024"
              placeholder=""
            />
          </template>

          <template v-else-if="column.key === 'ops'">
            <a-button
              size="small"
              type="text"
              :disabled="!canWrite || index === 0"
              @click="move(index, -1)"
            >
              <ArrowUpOutlined />
            </a-button>
            <a-button
              size="small"
              type="text"
              :disabled="!canWrite || index === rows.length - 1"
              @click="move(index, 1)"
            >
              <ArrowDownOutlined />
            </a-button>
            <a-button size="small" type="text" danger :disabled="!canWrite" @click="drop(index)">
              <DeleteOutlined />
            </a-button>
          </template>
        </template>
      </a-table>

      <div class="section-head">
        <span class="section-title">索引</span>
        <a-button size="small" :disabled="!canWrite" @click="addIndex">
          <PlusOutlined /> 新增索引
        </a-button>
      </div>
      <a-table
        class="db-grid design-grid"
        size="small"
        :data-source="indexRows"
        :columns="INDEX_GRID"
        :pagination="false"
        row-key="key"
        :scroll="{ x: 'max-content' }"
      >
        <template #bodyCell="{ column, record, index }">
          <template v-if="column.key === 'name'">
            <a-input
              v-model:value="record.name"
              size="small"
              :disabled="!canWrite || record.readonly"
              :maxlength="64"
              placeholder="索引名"
            />
          </template>
          <template v-else-if="column.key === 'unique'">
            <a-checkbox v-model:checked="record.unique" :disabled="!canWrite || record.readonly" />
          </template>
          <template v-else-if="column.key === 'columns'">
            <a-select
              v-model:value="record.columns"
              size="small"
              mode="multiple"
              :disabled="!canWrite || record.readonly"
              :options="columnOptions"
              :max-tag-count="4"
              class="idx-select"
              placeholder="选择列（顺序即索引前缀顺序）"
            />
          </template>
          <template v-else-if="column.key === 'note'">
            <span class="idx-note">{{ record.readonly ? `${record.index_type} 索引，请到查询控制台改` : '' }}</span>
          </template>
          <template v-else-if="column.key === 'ops'">
            <a-button
              size="small"
              type="text"
              danger
              :disabled="!canWrite || record.readonly"
              @click="dropIndex(index)"
            >
              <DeleteOutlined />
            </a-button>
          </template>
        </template>
      </a-table>

      <div class="table-comment">
        <span class="section-title">表注释</span>
        <a-input
          v-model:value="tableComment"
          size="small"
          :disabled="!canWrite"
          :maxlength="2048"
          placeholder=""
        />
      </div>
    </div>

    <div class="foot">
      <a-button
        type="primary"
        :disabled="!canWrite || loading"
        :loading="previewing"
        @click="openPreview"
      >
        <ThunderboltOutlined /> 预览变更
      </a-button>
      <span class="foot-hint">
        语句由服务端拿当前结构算出，执行前逐条确认。外键与生成列不在这里改。
      </span>
    </div>

    <a-modal
      v-model:open="preview.open"
      title="将要执行的 ALTER"
      :width="Math.min(860, Math.round(windowWidth * 0.9))"
      :ok-text="preview.phase === 'done' ? '完成' : '确认执行'"
      :cancel-text="preview.phase === 'done' ? '' : '取消'"
      :ok-button-props="{
        danger: hasDestructive,
        loading: preview.phase === 'running',
        disabled: preview.phase !== 'ready' && preview.phase !== 'done',
      }"
      :mask-closable="preview.phase !== 'running'"
      @ok="onOk"
    >
      <template v-if="preview.phase === 'loading'">
        <a-spin tip="正在与当前表结构做对比…" />
      </template>
      <template v-else-if="preview.phase === 'error'">
        <a-alert type="error" show-icon :message="preview.error" />
      </template>
      <template v-else-if="preview.phase === 'empty'">
        <a-empty :image="simpleEmpty" description="结构没有变化，没有要执行的语句" />
      </template>
      <template v-else>
        <a-alert
          v-if="preview.warnings.length"
          class="warn-alert"
          type="warning"
          show-icon
          message="执行前须知"
        >
          <template #description>
            <ul class="warn-list">
              <li v-for="(w, i) in preview.warnings" :key="i">{{ w }}</li>
            </ul>
          </template>
        </a-alert>

        <ol class="stmt-list">
          <li
            v-for="(row, i) in previewRows"
            :key="i"
            :class="{ destructive: row.action.destructive, failed: row.result?.status === 'error' }"
          >
            <div class="stmt-head">
              <a-tag :color="row.action.destructive ? 'red' : 'blue'">
                {{ KIND_LABEL[row.action.kind] }}
              </a-tag>
              <span class="stmt-note">{{ row.action.note }}</span>
              <span v-if="row.result" class="stmt-status">
                {{ row.result.status === 'ok' ? `成功 ${row.result.elapsed_ms}ms` : '失败' }}
              </span>
            </div>
            <code class="stmt-sql">{{ row.action.sql }}</code>
            <div v-if="row.result?.status === 'error'" class="stmt-error">
              {{ row.result.message }}
            </div>
          </li>
        </ol>

        <div v-if="preview.phase === 'done'" class="stmt-summary">
          成功 {{ preview.succeeded }} 条，失败 {{ preview.failed }} 条。
          失败的语句多半是预览之后表结构被别人改过——刷新后重新预览一次。
        </div>
      </template>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Empty, message as toast } from 'ant-design-vue'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  DeleteOutlined,
  FolderOutlined,
  PlusOutlined,
  ReloadOutlined,
  TableOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons-vue'
import {
  opsApi,
  type DbAlterAction,
  type DbAlterKind,
  type DbBatchStatement,
  type DbTableDefUpdate,
} from '@/api'
import { useAuthStore } from '@/stores/auth'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const props = defineProps<{
  connId: number
  schema: string
  table: string
  /** 台账上的连接写开关；与用户的运维写权限叠加。 */
  writable: boolean
}>()

const auth = useAuthStore()
const canWrite = computed(() => props.writable && auth.canOpsWrite)

/** 一列的编辑态。``origin_name`` 是它在当前表里的名字：改名靠它对上号，新列为空。 */
interface ColumnRow {
  key: string
  origin_name: string
  name: string
  column_type: string
  nullable: boolean
  has_default: boolean
  default: string
  auto_increment: boolean
  on_update: boolean
  primary: boolean
  comment: string
}

interface IndexRow {
  key: string
  name: string
  unique: boolean
  columns: string[]
  /** 非 BTREE 索引（FULLTEXT / SPATIAL）服务端只会生成普通 ADD KEY，改它等于偷偷换类型。 */
  readonly: boolean
  index_type: string
}

const COLUMN_GRID = [
  { title: '#', key: 'seq', width: 44 },
  { title: '列名', key: 'name', width: 170 },
  { title: '类型', key: 'column_type', width: 180 },
  { title: '主键', key: 'primary', width: 56 },
  { title: 'NULL', key: 'nullable', width: 60 },
  { title: '默认值', key: 'default', width: 150 },
  { title: '自增', key: 'auto_increment', width: 56 },
  { title: 'ON UPDATE', key: 'on_update', width: 96 },
  { title: '注释', key: 'comment', width: 200 },
  { title: '', key: 'ops', width: 104 },
]

const INDEX_GRID = [
  { title: '索引名', key: 'name', width: 200 },
  { title: '唯一', key: 'unique', width: 56 },
  { title: '列（按顺序）', key: 'columns', width: 360 },
  { title: '', key: 'note' },
  { title: '', key: 'ops', width: 56 },
]

const KIND_LABEL: Record<DbAlterKind, string> = {
  'column-add': '新增列',
  'column-drop': '删除列',
  'column-modify': '修改列',
  'column-rename': '列改名',
  primary: '主键',
  'index-add': '新建索引',
  'index-drop': '删除索引',
  comment: '表注释',
}

const TYPE_SUGGESTIONS = [
  'int', 'bigint', 'smallint', 'tinyint', 'decimal(10,2)',
  'varchar(64)', 'char(36)', 'text', 'mediumtext', 'longtext', 'json',
  'date', 'datetime', 'timestamp', 'time', 'blob',
]

function typeOptions(current: string) {
  const needle = (current || '').toLowerCase()
  return TYPE_SUGGESTIONS.filter((t) => !needle || t.includes(needle)).map((t) => ({ value: t }))
}

const rows = ref<ColumnRow[]>([])
const indexRows = ref<IndexRow[]>([])
const tableComment = ref('')
const engine = ref('')
const loading = ref(false)
const previewing = ref(false)

/** 新增行的 key 只用随机数区分，不与服务器上的列名发生关系。 */
let nonce = 0
function nextKey(prefix: string) {
  nonce += 1
  return `${prefix}-${Date.now().toString(36)}-${nonce}`
}

const columnOptions = computed(() =>
  rows.value.filter((r) => r.name.trim()).map((r) => ({ value: r.name.trim(), label: r.name.trim() }))
)

const hasDestructive = computed(() => preview.actions.some((a) => a.destructive))

const preview = reactive<{
  open: boolean
  phase: 'loading' | 'error' | 'empty' | 'ready' | 'running' | 'done'
  error: string
  actions: DbAlterAction[]
  warnings: string[]
  results: DbBatchStatement[]
  succeeded: number
  failed: number
}>({
  open: false,
  phase: 'loading',
  error: '',
  actions: [],
  warnings: [],
  results: [],
  succeeded: 0,
  failed: 0,
})

/** 预览列表与执行结果同序（服务端保持语句顺序，被拒的句子也占位）。 */
const previewRows = computed(() =>
  preview.actions.map((action, i) => ({ action, result: preview.results[i] }))
)

async function load() {
  loading.value = true
  try {
    const def = await opsApi.dbTableDef(props.connId, props.schema, props.table)
    const pk = new Set(def.primary_key.map((c) => c.toLowerCase()))
    rows.value = def.columns.map((c) => ({
      key: nextKey('c'),
      origin_name: c.name,
      name: c.name,
      column_type: c.column_type,
      nullable: c.nullable,
      has_default: c.default != null,
      // 表达式默认值（CURRENT_TIMESTAMP）读回来是原文，直接进输入框，用户不动它就原样生成。
      default: c.default == null ? '' : String(c.default),
      auto_increment: (c.extra || '').toUpperCase().includes('AUTO_INCREMENT'),
      on_update: (c.extra || '').toUpperCase().includes('ON UPDATE'),
      primary: pk.has(c.name.toLowerCase()),
      comment: c.comment || '',
    }))
    indexRows.value = def.indexes.map((i) => ({
      key: nextKey('i'),
      name: i.name,
      unique: i.unique,
      columns: [...i.columns],
      readonly: (i.index_type || '').toUpperCase() !== 'BTREE',
      index_type: i.index_type || '',
    }))
    tableComment.value = def.comment || ''
    engine.value = def.engine || ''
  } catch (err: any) {
    toast.error(err?.message || '读取表结构失败')
  } finally {
    loading.value = false
  }
}

function addColumn() {
  rows.value.push({
    key: nextKey('c'),
    origin_name: '',
    name: '',
    column_type: 'varchar(64)',
    nullable: true,
    has_default: false,
    default: '',
    auto_increment: false,
    on_update: false,
    primary: false,
    comment: '',
  })
}

function addIndex() {
  indexRows.value.push({
    key: nextKey('i'),
    name: '',
    unique: false,
    columns: [],
    readonly: false,
    index_type: 'BTREE',
  })
}

function drop(index: number) {
  rows.value.splice(index, 1)
}

function dropIndex(index: number) {
  indexRows.value.splice(index, 1)
}

function move(index: number, delta: number) {
  const target = index + delta
  if (target < 0 || target >= rows.value.length) return
  const list = rows.value
  ;[list[index], list[target]] = [list[target], list[index]]
  rows.value = [...list]
}

function togglePrimary(row: ColumnRow, checked: boolean) {
  row.primary = checked
  // 主键列必须 NOT NULL，界面上先把钩子改掉，省得执行时才被服务端纠正。
  if (checked) row.nullable = false
}

/** 表单校验只做「明显不对」的那几项：剩下的是服务端的活（它才是生成语句的人）。 */
function validate(): string {
  const names = new Set<string>()
  for (const row of rows.value) {
    const name = row.name.trim()
    if (!name) return '有列没填列名'
    if (!row.column_type.trim()) return `列「${name}」没填类型`
    if (/[;'`\\]|--/.test(name)) return `列名「${name}」含有不能进 DDL 的字符`
    if (names.has(name.toLowerCase())) return `列名「${name}」重复`
    names.add(name.toLowerCase())
  }
  if (!rows.value.length) return '至少要留一列'
  const indexNames = new Set<string>()
  for (const item of indexRows.value) {
    // 非 BTREE 索引改不了，也就没什么可校验的。
    if (item.readonly) continue
    if (!item.name.trim()) return '有索引没填名字'
    if (!item.columns.length) return `索引「${item.name}」没选列`
    const key = item.name.trim().toLowerCase()
    if (indexNames.has(key)) return `索引名「${item.name}」重复`
    indexNames.add(key)
    for (const c of item.columns) {
      if (!names.has(c.toLowerCase())) return `索引「${item.name}」引用了不在列清单里的「${c}」`
    }
  }
  return ''
}

function payload(): DbTableDefUpdate {
  return {
    columns: rows.value.map((r) => {
      // 输入框里的 NULL 表达的是 SQL NULL，不是字符串 'NULL'。
      const isNull = /^null$/i.test(r.default.trim())
      return {
        name: r.name.trim(),
        origin_name: r.origin_name || null,
        column_type: r.column_type.trim(),
        nullable: r.nullable,
        auto_increment: r.auto_increment,
        on_update_current_timestamp: r.on_update,
        has_default: r.has_default,
        default: r.has_default ? (isNull ? null : r.default) : null,
        comment: r.comment,
      }
    }),
    primary_key: rows.value.filter((r) => r.primary).map((r) => r.name.trim()),
    // 非 BTREE 索引原样带上（表单里改不动它）：不带的意思是「期望定义里没有它」，
    // 服务端会生成 DROP——那就等于把全文索引偷偷删了。
    indexes: indexRows.value.map((i) => ({
      name: i.name.trim(),
      unique: i.unique,
      columns: [...i.columns],
    })),
    comment: tableComment.value,
  }
}

async function openPreview() {
  const problem = validate()
  if (problem) {
    toast.warning(problem)
    return
  }
  preview.open = true
  preview.phase = 'loading'
  preview.error = ''
  preview.results = []
  preview.succeeded = 0
  preview.failed = 0
  previewing.value = true
  try {
    const res = await opsApi.dbTableDefPreview(props.connId, props.schema, props.table, payload())
    preview.actions = res.actions
    preview.warnings = res.warnings
    preview.phase = res.actions.length ? 'ready' : 'empty'
  } catch (err: any) {
    preview.phase = 'error'
    preview.error = err?.message || '预览失败'
  } finally {
    previewing.value = false
  }
}

async function applyChanges() {
  preview.phase = 'running'
  try {
    const res = await opsApi.dbTableDefApply(
      props.connId,
      props.schema,
      props.table,
      preview.actions.map((a) => a.sql)
    )
    preview.results = res.statements
    preview.succeeded = res.succeeded
    preview.failed = res.failed
    preview.phase = 'done'
    if (res.failed === 0) {
      toast.success(`已执行 ${res.succeeded} 条语句，表结构已更新`)
      await load()
    } else {
      toast.warning(`${res.succeeded} 条成功，${res.failed} 条失败，逐条原因见列表`)
    }
  } catch (err: any) {
    preview.phase = 'error'
    preview.error = err?.message || '执行失败'
  }
}

function onOk() {
  if (preview.phase === 'done' || preview.phase === 'empty') {
    preview.open = false
    return
  }
  if (preview.phase === 'ready') void applyChanges()
  // 执行中不许关（modal 的 ok 按钮此时已禁用）。
}

// 弹窗宽度跟着视口收窄，窄屏下语句不会被折成一长条。
const windowWidth = ref(window.innerWidth)
function onResize() {
  windowWidth.value = window.innerWidth
}

onMounted(() => {
  void load()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => window.removeEventListener('resize', onResize))
</script>

<style scoped>
.tab-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

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

.engine {
  margin-left: 8px;
  color: var(--text-3);
  font-size: 12px;
}

.spacer {
  flex: 1;
}

.gate-alert {
  margin-bottom: 8px;
}

.form-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.section-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 6px;
}

.section-title {
  font-size: 12px;
  font-weight: 500;
  color: rgba(0, 0, 0, 0.6);
}

.design-grid :deep(.ant-table-cell) {
  padding: 4px 6px !important;
}

.default-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.idx-select {
  width: 100%;
  min-width: 180px;
}

.idx-note {
  color: var(--text-3);
  font-size: 12px;
}

.table-comment {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 14px 0 4px;
}

.table-comment .ant-input {
  max-width: 520px;
}

.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--hairline, #f0f0f0);
}

.foot-hint {
  color: var(--text-3);
  font-size: 12px;
}

.warn-alert {
  margin-bottom: 10px;
}

.warn-list {
  margin: 0;
  padding-left: 18px;
}

.stmt-list {
  margin: 0;
  padding-left: 0;
  list-style: none;
  counter-reset: stmt;
}

.stmt-list li {
  margin-bottom: 8px;
  padding: 8px 10px;
  border: 1px solid var(--hairline, #f0f0f0);
  border-radius: 6px;
  background: var(--paper, #fafafa);
}

.stmt-list li.destructive {
  border-color: #ffccc7;
  background: #fff2f0;
}

.stmt-list li.failed {
  border-color: #ffccc7;
}

.stmt-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.stmt-note {
  color: var(--text-3);
  font-size: 12px;
}

.stmt-status {
  margin-left: auto;
  font-size: 12px;
}

.stmt-sql {
  display: block;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

.stmt-error {
  margin-top: 4px;
  color: #cf1322;
  font-size: 12px;
  word-break: break-all;
}

.stmt-summary {
  color: var(--text-3);
  font-size: 12px;
}
</style>
