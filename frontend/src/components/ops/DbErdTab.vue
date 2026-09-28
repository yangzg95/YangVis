<template>
  <div class="tab-page">
    <div class="toolbar">
      <span class="crumb">
        <ApartmentOutlined /> {{ schema }} 实体关系
      </span>
      <a-select
        v-model:value="picked"
        class="table-filter"
        mode="multiple"
        size="small"
        placeholder="全部有关系的表"
        :max-tag-count="2"
        :options="tableOptions"
        :filter-option="filterByLabel"
        option-filter-prop="label"
      />
      <span class="spacer" />
      <a-button-group size="small">
        <a-button :disabled="scale <= MIN_SCALE" @click="zoom(-1)">
          <ZoomOutOutlined />
        </a-button>
        <a-button class="scale-text" @click="resetZoom">{{ Math.round(scale * 100) }}%</a-button>
        <a-button :disabled="scale >= MAX_SCALE" @click="zoom(1)">
          <ZoomInOutlined />
        </a-button>
      </a-button-group>
      <a-tooltip title="下载 SVG">
        <a-button size="small" type="text" :disabled="!svg" @click="downloadSvg">
          <DownloadOutlined />
        </a-button>
      </a-tooltip>
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-alert
      v-if="erd?.truncated"
      class="note"
      type="info"
      show-icon
      :message="`表太多，图里只画了前 ${MAX_TABLES} 张（按参与外键关系优先）。用上面的筛选框指定想看的表。`"
    />

    <div ref="canvasRef" class="canvas">
      <a-spin v-if="loading && !erd" class="state" />
      <div v-else-if="error" class="state error">
        <div class="error-text">{{ error }}</div>
        <pre v-if="source" class="error-source">{{ source }}</pre>
      </div>
      <div v-else-if="!erd || !erd.tables.length" class="state">这个库里没有表</div>
      <!-- securityLevel: 'strict' 下 mermaid 会转义标签里的 HTML，v-html 不等于把
           库名/表名原样塞进 DOM。 -->
      <div v-else class="diagram" :style="{ transform: `scale(${scale})` }" v-html="svg" />
    </div>

    <div v-if="relations.length" class="legend">
      <a-tooltip v-for="r in relations" :key="r.name" :title="`${r.from_table}.${r.from_columns.join(',')} → ${r.to_table}.${r.to_columns.join(',')}`">
        <button type="button" class="chip" @click="openTable(r.from_table)">
          {{ r.from_table }} → {{ r.to_table }}
        </button>
      </a-tooltip>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import {
  ApartmentOutlined,
  DownloadOutlined,
  ReloadOutlined,
  ZoomInOutlined,
  ZoomOutOutlined,
} from '@ant-design/icons-vue'
import { message as toast } from 'ant-design-vue'
import { opsApi, type DbErd, type DbErdRelation, type DbErdTable } from '@/api'

const props = defineProps<{
  connId: number
  schema: string
}>()

/** 点击图下方的关系条时，请求打开那张表的数据页签。 */
const emit = defineEmits<{ openTable: [table: string] }>()

// 与服务端 _ERD_MAX_TABLES 一致，只用于提示文案。
const MAX_TABLES = 40
const MIN_SCALE = 0.3
const MAX_SCALE = 2.5

const erd = ref<DbErd | null>(null)
const loading = ref(false)
const error = ref('')
const svg = ref('')
const source = ref('')
/** 选中的表；空数组表示「按服务端的默认选取」（参与外键关系者优先）。 */
const picked = ref<string[]>([])

const scale = ref(1)
const canvasRef = ref<HTMLElement | null>(null)

const tableOptions = computed(() =>
  (erd.value?.tables ?? []).map((t) => ({ label: t.name, value: t.name })),
)

const relations = computed<DbErdRelation[]>(() => erd.value?.relations ?? [])

function filterByLabel(input: string, option: { label?: unknown }) {
  return String(option.label ?? '').toLowerCase().includes(input.toLowerCase())
}

// mermaid 含布局引擎，体积不小：只有真的打开 ER 页签才拉这个 chunk。
type Mermaid = typeof import('mermaid')['default']
let mermaidPromise: Promise<Mermaid> | null = null

function loadMermaid(): Promise<Mermaid> {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then((mod) => {
      mod.default.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'default',
      })
      return mod.default
    })
  }
  return mermaidPromise
}

let renderSeq = 0

/**
 * erDiagram 的属性类型只认简单标识符，`varchar(255) unsigned` 这类带括号/空格的
 * 原文会让解析器直接报错，所以只取基础类型名。
 */
function baseType(columnType: string): string {
  return (columnType.split('(')[0] || 'text').trim().split(/\s+/)[0] || 'text'
}

/** 实体名含非标识符字符（空格、中文、连字符）时用 mermaid 的引号形式。 */
function entityId(name: string): string {
  return /^\w+$/.test(name) ? name : `"${name.replace(/["\\\n]/g, '')}"`
}

/** 连线标签用「外键列 → 主键列」，比约束名更能说明两个实体为什么相连。 */
function relationLabel(r: DbErdRelation): string {
  return `${r.from_columns.join(',')}→${r.to_columns.join(',')}`.replace(/["\\]/g, '')
}

/**
 * 基数只在能确定时才画 1:1：单列外键且该列自带主键或唯一约束，才是「一边只能一个」。
 * 其余一律一挂多——联合外键的 COLUMN_KEY 只标在首列上，猜成 1:1 比猜成 1:N 更糟。
 */
function cardinality(table: DbErdTable, columns: string[]): string {
  if (columns.length !== 1) return '||--o{'
  const hit = table.columns.find((c) => c.name === columns[0])
  return hit && (hit.key === 'PRI' || hit.key === 'UNI') ? '||--||' : '||--o{'
}

function buildSource(tables: DbErdTable[], rels: DbErdRelation[]): string {
  const byName = new Map(tables.map((t) => [t.name, t]))
  const lines: string[] = ['erDiagram']
  for (const t of tables) {
    const attrs = t.columns
      .map((c) => `    ${baseType(c.column_type)} ${c.name}${c.key ? ` "${keyComment(c.key)}"` : ''}`)
      .join('\n')
    lines.push(`    ${entityId(t.name)} {`)
    if (attrs) lines.push(attrs)
    lines.push('    }')
  }
  for (const r of rels) {
    const from = byName.get(r.from_table)
    if (!from || !byName.has(r.to_table)) continue
    lines.push(
      `    ${entityId(r.to_table)} ${cardinality(from, r.from_columns)} ${entityId(r.from_table)} : "${relationLabel(r)}"`,
    )
  }
  return lines.join('\n')
}

// mermaid 的 keys 段只认 PK/FK/UK；MUL（普通索引）不是键，留空。
function keyComment(columnKey: string): string {
  if (columnKey === 'PRI') return 'PK'
  if (columnKey === 'UNI') return 'UK'
  return ''
}

/** 关系里出现过的表优先——孤立表画在图上只是一堆没有连线的方块。 */
function defaultSelection(): string[] {
  const names = new Set<string>()
  for (const r of relations.value) {
    names.add(r.from_table)
    names.add(r.to_table)
  }
  return [...names]
}

function renderTables(): DbErdTable[] {
  const all = erd.value?.tables ?? []
  const selected = picked.value.length ? new Set(picked.value) : new Set(defaultSelection())
  const inRelation = (t: DbErdTable) => relations.value.some((r) => r.from_table === t.name || r.to_table === t.name)
  return all.filter((t) => selected.has(t.name) || (!picked.value.length && !inRelation(t) && all.length <= 8))
}

function renderRelations(tables: DbErdTable[]): DbErdRelation[] {
  const names = new Set(tables.map((t) => t.name))
  return relations.value.filter((r) => names.has(r.from_table) && names.has(r.to_table))
}

async function draw() {
  const tables = renderTables()
  const rels = renderRelations(tables)
  source.value = buildSource(tables, rels)
  if (!tables.length) {
    svg.value = ''
    error.value = ''
    return
  }
  error.value = ''
  try {
    const mermaid = await loadMermaid()
    const id = `db-erd-${++renderSeq}`
    try {
      const { svg: rendered } = await mermaid.render(id, source.value)
      svg.value = rendered
    } catch (err) {
      document.getElementById(id)?.remove()
      svg.value = ''
      error.value = err instanceof Error ? err.message.split('\n')[0] : String(err)
    }
  } catch (err: any) {
    svg.value = ''
    error.value = err?.message || '渲染引擎加载失败'
  }
}

async function load() {
  loading.value = true
  try {
    erd.value = await opsApi.dbErd(props.connId, props.schema)
    picked.value = []
    await draw()
  } catch (err: any) {
    error.value = err?.message || '加载失败'
    toast.error(error.value)
  } finally {
    loading.value = false
  }
}

watch(picked, () => void draw())

function zoom(direction: number) {
  scale.value = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Math.round((scale.value + direction * 0.2) * 10) / 10))
}

function resetZoom() {
  scale.value = 1
}

function downloadSvg() {
  const blob = new Blob([svg.value], { type: 'image/svg+xml' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `erd-${props.schema}-${Date.now()}.svg`
  anchor.click()
  URL.revokeObjectURL(url)
}

function openTable(name: string) {
  emit('openTable', name)
}

onMounted(load)
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
  flex-wrap: wrap;
}

.crumb {
  font-size: 13px;
  color: rgba(0, 0, 0, 0.65);
  white-space: nowrap;
}

.table-filter {
  min-width: 220px;
  max-width: 420px;
  flex: 1;
}

.spacer {
  flex: 1;
}

.scale-text {
  min-width: 56px;
  font-variant-numeric: tabular-nums;
}

.note {
  margin-bottom: 8px;
}

.canvas {
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--hairline, #f0f0f0);
  border-radius: 8px;
  background: #fff;
}

/* 缩放以左上角为基准，配合 canvas 的滚动条看大图时不会把图甩出可视区。 */
.diagram {
  display: inline-block;
  min-width: 100%;
  padding: 12px;
  transform-origin: top left;
}

.state {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px 16px;
  color: var(--text-3);
  font-size: 13px;
}

.state.error {
  flex-direction: column;
  align-items: stretch;
}

.error-text {
  color: #ff4d4f;
}

.error-source {
  margin: 8px 0 0;
  padding: 8px;
  max-height: 200px;
  overflow: auto;
  background: var(--paper, #fafafa);
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  flex-shrink: 0;
  padding-top: 8px;
  max-height: 88px;
  overflow: auto;
}

.chip {
  border: 1px solid var(--hairline, #f0f0f0);
  border-radius: 12px;
  padding: 1px 8px;
  background: #fff;
  color: var(--text-2, rgba(0, 0, 0, 0.65));
  font-family: var(--font-mono);
  font-size: 12px;
  cursor: pointer;
}

.chip:hover {
  border-color: var(--signal-border, #91caff);
  color: var(--signal-text, #0958d9);
}
</style>
