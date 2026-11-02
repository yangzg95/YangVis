<template>
  <div class="dbm">
    <header class="dbm-head">
      <button
        v-if="pane !== 'connections'"
        type="button"
        class="head-btn"
        aria-label="返回上一级"
        @click="goBack"
      >
        <ArrowLeftOutlined />
      </button>
      <div class="head-main">
        <div class="head-title">{{ title }}</div>
        <div v-if="subtitle" class="head-sub">{{ subtitle }}</div>
      </div>
      <button v-if="conn" type="button" class="ai-btn" @click="openAi">
        <RobotOutlined />
        <span>AI 问答</span>
      </button>
    </header>

    <div class="dbm-body">
      <!-- 连接列表 -->
      <template v-if="pane === 'connections'">
        <div class="filters">
          <a-input
            v-model:value="keyword"
            allow-clear
            placeholder="搜索名称或地址"
            @change="onKeywordChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input>
          <a-select v-model:value="typeFilter" :options="TYPE_OPTIONS" @change="loadConnections" />
        </div>

        <div v-if="error" class="error-box">
          <WarningOutlined />
          <span>{{ error }}</span>
          <a-button size="small" @click="loadConnections">重试</a-button>
        </div>

        <a-spin :spinning="loading">
          <a-empty v-if="!loading && !connections.length" :image="simpleEmpty" description="暂无连接" />
          <div v-else class="card-list">
            <div
              v-for="item in connections"
              :key="item.id"
              class="conn-card"
              role="button"
              tabindex="0"
              @click="openConn(item)"
              @keydown.enter="openConn(item)"
            >
              <div class="card-head">
                <span class="conn-name">{{ item.name }}</span>
                <a-tag :color="item.db_type === 'mysql' ? 'blue' : 'orange'">
                  {{ item.db_type === 'mysql' ? 'MySQL' : 'Redis' }}
                </a-tag>
              </div>
              <div class="conn-addr">{{ connAddress(item) }}</div>
              <div class="conn-meta">
                <span :class="['state-dot', item.last_check_ok ? 'ok' : 'fail']" />
                <span>{{ item.last_check_ok ? '连接正常' : item.last_checked_at ? '连接异常' : '未检测' }}</span>
                <span class="dot-sep">·</span>
                <span>{{ item.writable ? '连接可写' : '只读连接' }}</span>
              </div>
              <div v-if="item.remark" class="conn-remark">{{ item.remark }}</div>
              <div v-if="item.password_error" class="conn-error">凭据无法解密</div>
              <div class="card-foot">
                <span class="foot-hint">手机端只读浏览</span>
                <span class="foot-action" @click.stop="openAiFrom(item)">
                  <RobotOutlined /> AI 问答
                </span>
              </div>
            </div>
          </div>
        </a-spin>
      </template>

      <!-- 库 / Redis db 列表 -->
      <template v-else-if="pane === 'schemas'">
        <div v-if="error" class="error-box">
          <WarningOutlined />
          <span>{{ error }}</span>
          <a-button size="small" @click="loadSchemas">重试</a-button>
        </div>
        <a-spin :spinning="loading">
          <a-empty v-if="!loading && !schemas.length" :image="simpleEmpty" description="没有可浏览的库" />
          <div v-else class="card-list">
            <button
              v-for="item in schemas"
              :key="item.name"
              type="button"
              class="obj-card"
              @click="openSchema(item)"
            >
              <span class="obj-icon"><DatabaseOutlined /></span>
              <span class="obj-main">
                <span class="obj-name">{{ schemaLabel(item) }}</span>
                <span v-if="item.object_count !== null && item.object_count !== undefined" class="obj-meta">
                  {{ item.kind === 'redisdb' ? `${item.object_count} 个 key` : `${item.object_count} 张表` }}
                </span>
              </span>
              <RightOutlined class="obj-arrow" />
            </button>
          </div>
        </a-spin>
      </template>

      <!-- 表列表 -->
      <template v-else-if="pane === 'tables'">
        <div class="filters">
          <a-input v-model:value="tableKeyword" allow-clear placeholder="搜索表名或注释">
            <template #prefix><SearchOutlined /></template>
          </a-input>
        </div>

        <div v-if="error" class="error-box">
          <WarningOutlined />
          <span>{{ error }}</span>
          <a-button size="small" @click="loadTables">重试</a-button>
        </div>

        <a-spin :spinning="loading">
          <a-empty v-if="!loading && !filteredTables.length" :image="simpleEmpty" description="没有匹配的表" />
          <div v-else class="card-list">
            <button
              v-for="item in filteredTables"
              :key="item.name"
              type="button"
              class="obj-card"
              @click="openTable(item)"
            >
              <span class="obj-icon"><TableOutlined /></span>
              <span class="obj-main">
                <span class="obj-name">{{ item.name }}</span>
                <span class="obj-meta">
                  {{ item.table_type === 'VIEW' ? '视图' : item.engine || '表' }}
                  <template v-if="item.rows_estimate !== null && item.rows_estimate !== undefined">
                    · 约 {{ item.rows_estimate }} 行
                  </template>
                </span>
                <span v-if="item.comment" class="obj-comment">{{ item.comment }}</span>
              </span>
              <RightOutlined class="obj-arrow" />
            </button>
          </div>
        </a-spin>
      </template>

      <!-- 表详情：数据 / 结构 -->
      <template v-else-if="pane === 'table'">
        <a-segmented v-model:value="tableTab" :options="TABLE_TABS" block class="segmented" />

        <div v-if="tabError" class="error-box">
          <WarningOutlined />
          <span>{{ tabError }}</span>
          <a-button size="small" @click="tableTab === 'data' ? loadRows(true) : loadColumns()">重试</a-button>
        </div>

        <template v-if="tableTab === 'data'">
          <a-spin :spinning="rowsLoading">
            <a-empty v-if="!rowsLoading && !rows.length" :image="simpleEmpty" description="这张表没有数据" />
            <div v-else class="row-list">
              <div v-for="(row, rowIndex) in rows" :key="rowIndex" class="row-card">
                <div class="row-head">
                  <span>第 {{ rowIndex + 1 }} 行</span>
                  <button
                    v-if="rowColumns.length > ROW_PREVIEW_COLS"
                    type="button"
                    class="row-toggle"
                    @click="toggleRow(rowIndex)"
                  >
                    {{ expandedRows[rowIndex] ? '收起' : `展开全部 ${rowColumns.length} 列` }}
                  </button>
                </div>
                <div
                  v-for="(col, colIndex) in visibleColumns(rowIndex)"
                  :key="col"
                  class="cell-line"
                >
                  <span class="cell-name">{{ col }}</span>
                  <span :class="['cell-value', { 'is-null': row[colIndex] === null }]">
                    {{ formatCell(row[colIndex]) }}
                  </span>
                </div>
              </div>
            </div>
          </a-spin>

          <div v-if="rows.length" class="load-more">
            <span class="load-meta">已加载 {{ rows.length }} / {{ total }} 行</span>
            <a-button
              v-if="rows.length < total"
              size="small"
              :loading="rowsLoading"
              @click="loadRows(false)"
            >
              加载更多
            </a-button>
          </div>
        </template>

        <a-spin v-else :spinning="columnsLoading">
          <a-empty v-if="!columnsLoading && !columns.length" :image="simpleEmpty" description="没有列信息" />
          <div v-else class="col-list">
            <div v-for="col in columns" :key="col.name" class="col-card">
              <div class="col-head">
                <span class="col-name">{{ col.name }}</span>
                <a-tag v-if="col.column_key === 'PRI'" color="red">主键</a-tag>
                <a-tag v-else-if="col.column_key === 'UNI'" color="blue">唯一</a-tag>
                <a-tag v-else-if="col.column_key === 'MUL'" color="default">索引</a-tag>
              </div>
              <div class="col-type">{{ col.column_type }}</div>
              <div class="col-meta">
                <span>{{ col.nullable ? '可空' : '非空' }}</span>
                <template v-if="col.default !== null && col.default !== undefined">
                  <span class="dot-sep">·</span>
                  <span>默认 {{ col.default }}</span>
                </template>
                <template v-if="col.extra">
                  <span class="dot-sep">·</span>
                  <span>{{ col.extra }}</span>
                </template>
              </div>
              <div v-if="col.comment" class="col-comment">{{ col.comment }}</div>
            </div>
          </div>
        </a-spin>
      </template>

      <!-- Redis 键列表 -->
      <template v-else-if="pane === 'redisKeys'">
        <div class="filters">
          <a-input-search
            v-model:value="keyPattern"
            allow-clear
            placeholder="key 模式，如 user:*"
            :maxlength="256"
            enter-button="扫描"
            @search="loadKeys(true)"
          />
        </div>

        <div v-if="error" class="error-box">
          <WarningOutlined />
          <span>{{ error }}</span>
          <a-button size="small" @click="loadKeys(true)">重试</a-button>
        </div>

        <a-spin :spinning="keysLoading">
          <a-empty v-if="!keysLoading && !keys.length" :image="simpleEmpty" description="没有匹配的 key" />
          <div v-else class="card-list">
            <button
              v-for="item in keys"
              :key="item.key"
              type="button"
              class="obj-card key-card"
              @click="openKey(item)"
            >
              <span class="obj-main">
                <span class="obj-name key-name">{{ item.key }}</span>
                <span class="obj-meta">{{ item.key_type }}</span>
              </span>
              <RightOutlined class="obj-arrow" />
            </button>
          </div>
        </a-spin>

        <div v-if="keys.length" class="load-more">
          <span class="load-meta">已加载 {{ keys.length }} 个</span>
          <a-button v-if="!keysDone" size="small" :loading="keysLoading" @click="loadKeys(false)">
            继续扫描
          </a-button>
          <span v-else class="load-meta">已扫描完</span>
        </div>
      </template>

      <!-- Redis 键详情 -->
      <template v-else-if="pane === 'redisKey'">
        <div v-if="error" class="error-box">
          <WarningOutlined />
          <span>{{ error }}</span>
          <a-button size="small" @click="loadKeyDetail">重试</a-button>
        </div>
        <a-spin :spinning="keyLoading">
          <template v-if="redisKey">
            <div class="key-meta">
              <a-tag color="orange">{{ redisKey.key_type }}</a-tag>
              <span>TTL：{{ ttlText(redisKey.ttl) }}</span>
              <span v-if="redisKey.truncated" class="truncated">内容已截断</span>
            </div>

            <pre v-if="typeof redisKey.value === 'string'" class="key-string">{{ redisKey.value }}</pre>

            <div
              v-else-if="Array.isArray(redisKey.value) && redisKey.value.length && Array.isArray(redisKey.value[0])"
              class="pair-list"
            >
              <div v-for="(pair, i) in redisKey.value" :key="i" class="pair-row">
                <span class="pair-field">{{ formatCell(pairItems(pair)[0]) }}</span>
                <span class="pair-value">{{ formatCell(pairItems(pair)[1]) }}</span>
              </div>
            </div>

            <div v-else-if="Array.isArray(redisKey.value)" class="element-list">
              <div v-for="(item, i) in redisKey.value" :key="i" class="element-row">
                <span class="element-index">{{ i + 1 }}</span>
                <span class="element-value">{{ formatCell(item) }}</span>
              </div>
            </div>

            <pre v-else class="key-string">{{ jsonText(redisKey.value) }}</pre>
          </template>
        </a-spin>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 数据库手机版：连接 → 库/键空间 → 表/键 → 数据、结构或值。
 *
 * 刻意不复用桌面工作台的 DbConnectionTree / DbQueryTab / DbResultGrid：
 * 那些组件依赖双击、右键、拖拽分栏和宽表格，手机上没法操作。这里只沿用
 * opsApi 的数据契约，交互全部换成整屏钻取 + 卡片式展示。
 *
 * 手机端一律只读：不提供 SQL 编辑器、不开放写入口，执行与改数据请用电脑。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Empty } from 'ant-design-vue'
import {
  ArrowLeftOutlined,
  DatabaseOutlined,
  RightOutlined,
  RobotOutlined,
  SearchOutlined,
  TableOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import {
  opsApi,
  type DatabaseType,
  type DbColumnItem,
  type DbSchemaItem,
  type DbTableItem,
  type OpsDatabase,
  type RedisKeyDetail,
  type RedisKeyItem,
} from '@/api'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

/** 数据卡片默认只摊前 6 列，宽表靠「展开全部」按需看。 */
const ROW_PREVIEW_COLS = 6
const ROW_PAGE_SIZE = 50
const KEY_PAGE_SIZE = 100

const TYPE_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'redis', label: 'Redis' },
]
const TABLE_TABS = [
  { value: 'data', label: '数据' },
  { value: 'structure', label: '结构' },
]

type Pane = 'connections' | 'schemas' | 'tables' | 'table' | 'redisKeys' | 'redisKey'

const router = useRouter()

const pane = ref<Pane>('connections')
const conn = ref<OpsDatabase | null>(null)
const schema = ref('')
const table = ref<DbTableItem | null>(null)
const redisDb = ref(0)

const loading = ref(false)
const error = ref('')

// ---- 连接列表 ----------------------------------------------------------------
const connections = ref<OpsDatabase[]>([])
const keyword = ref('')
const typeFilter = ref<'' | DatabaseType>('')

// ---- 库 / 表 -----------------------------------------------------------------
const schemas = ref<DbSchemaItem[]>([])
const tables = ref<DbTableItem[]>([])
const tableKeyword = ref('')

// ---- 表详情 ------------------------------------------------------------------
const tableTab = ref<'data' | 'structure'>('data')
const columns = ref<DbColumnItem[]>([])
const columnsLoading = ref(false)
const rows = ref<unknown[][]>([])
const rowColumns = ref<string[]>([])
const rowsLoading = ref(false)
const rowPage = ref(1)
const total = ref(0)
const tabError = ref('')
const expandedRows = ref<Record<number, boolean>>({})

// ---- Redis -------------------------------------------------------------------
const keys = ref<RedisKeyItem[]>([])
const keyPattern = ref('')
const keysLoading = ref(false)
const keysDone = ref(false)
let keyCursor = '0'
const redisKey = ref<RedisKeyDetail | null>(null)
const keyLoading = ref(false)
const activeKey = ref('')

// 每类请求一个递增序号：手机上手速快，旧请求晚回来会覆盖新视图。
const seq = { conn: 0, schema: 0, table: 0, rows: 0, keys: 0, key: 0 }

let keywordTimer: ReturnType<typeof setTimeout> | null = null

onMounted(loadConnections)
onBeforeUnmount(() => {
  if (keywordTimer) clearTimeout(keywordTimer)
})

// ---- 连接列表 ----------------------------------------------------------------

async function loadConnections() {
  const mine = ++seq.conn
  loading.value = true
  error.value = ''
  try {
    const res = await opsApi.listDatabases({
      keyword: keyword.value.trim() || undefined,
      db_type: typeFilter.value || undefined,
    })
    if (mine !== seq.conn) return
    connections.value = res.items
  } catch {
    if (mine !== seq.conn) return
    error.value = '连接列表加载失败'
    connections.value = []
  } finally {
    if (mine === seq.conn) loading.value = false
  }
}

function onKeywordChange() {
  if (keywordTimer) clearTimeout(keywordTimer)
  keywordTimer = setTimeout(loadConnections, 300)
}

function openConn(item: OpsDatabase) {
  conn.value = item
  schema.value = ''
  table.value = null
  void loadSchemas()
}

// ---- 库 / Redis db -----------------------------------------------------------

async function loadSchemas() {
  if (!conn.value) return
  const mine = ++seq.schema
  pane.value = 'schemas'
  loading.value = true
  error.value = ''
  try {
    const res = await opsApi.dbSchemas(conn.value.id)
    if (mine !== seq.schema) return
    schemas.value = res.items
  } catch {
    if (mine !== seq.schema) return
    error.value = '库列表加载失败'
    schemas.value = []
  } finally {
    if (mine === seq.schema) loading.value = false
  }
}

function schemaLabel(item: DbSchemaItem) {
  return item.kind === 'redisdb' ? item.name.replace(/^db/, 'DB ') : item.name
}

function openSchema(item: DbSchemaItem) {
  schema.value = item.name
  if (conn.value?.db_type === 'redis') {
    redisDb.value = Number(item.name.replace(/^db/, '')) || 0
    void loadKeys(true)
  } else {
    void loadTables()
  }
}

// ---- 表列表 ------------------------------------------------------------------

async function loadTables() {
  if (!conn.value) return
  const mine = ++seq.table
  pane.value = 'tables'
  tableKeyword.value = ''
  loading.value = true
  error.value = ''
  try {
    const res = await opsApi.dbTables(conn.value.id, schema.value)
    if (mine !== seq.table) return
    tables.value = res.items
  } catch {
    if (mine !== seq.table) return
    error.value = '表列表加载失败'
    tables.value = []
  } finally {
    if (mine === seq.table) loading.value = false
  }
}

const filteredTables = computed(() => {
  const kw = tableKeyword.value.trim().toLowerCase()
  if (!kw) return tables.value
  return tables.value.filter(
    (item) =>
      item.name.toLowerCase().includes(kw) || (item.comment ?? '').toLowerCase().includes(kw),
  )
})

function openTable(item: DbTableItem) {
  table.value = item
  tableTab.value = 'data'
  columns.value = []
  rows.value = []
  rowColumns.value = []
  rowPage.value = 1
  total.value = 0
  expandedRows.value = {}
  pane.value = 'table'
  tabError.value = ''
  void loadColumns()
  void loadRows(true)
}

async function loadColumns() {
  if (!conn.value || !table.value) return
  const mine = ++seq.table
  columnsLoading.value = true
  tabError.value = ''
  try {
    const res = await opsApi.dbColumns(conn.value.id, schema.value, table.value.name)
    if (mine !== seq.table) return
    columns.value = res.items
  } catch {
    if (mine !== seq.table) return
    tabError.value = '列信息加载失败'
  } finally {
    if (mine === seq.table) columnsLoading.value = false
  }
}

async function loadRows(reset: boolean) {
  if (!conn.value || !table.value) return
  if (reset) {
    rowPage.value = 1
    expandedRows.value = {}
  }
  const mine = ++seq.rows
  rowsLoading.value = true
  tabError.value = ''
  try {
    const res = await opsApi.dbRows(
      conn.value.id,
      schema.value,
      table.value.name,
      rowPage.value,
      ROW_PAGE_SIZE,
    )
    if (mine !== seq.rows) return
    rowColumns.value = res.columns
    rows.value = reset ? res.rows : [...rows.value, ...res.rows]
    total.value = res.total
    if (res.rows.length) rowPage.value += 1
  } catch {
    if (mine !== seq.rows) return
    tabError.value = '数据加载失败'
  } finally {
    if (mine === seq.rows) rowsLoading.value = false
  }
}

function visibleColumns(rowIndex: number) {
  const list = rowColumns.value
  return expandedRows.value[rowIndex] ? list : list.slice(0, ROW_PREVIEW_COLS)
}

function toggleRow(rowIndex: number) {
  expandedRows.value = { ...expandedRows.value, [rowIndex]: !expandedRows.value[rowIndex] }
}

// ---- Redis 键 ----------------------------------------------------------------

async function loadKeys(reset: boolean) {
  if (!conn.value) return
  if (reset) {
    keys.value = []
    keyCursor = '0'
    keysDone.value = false
    pane.value = 'redisKeys'
  }
  const mine = ++seq.keys
  keysLoading.value = true
  error.value = ''
  try {
    const res = await opsApi.redisKeys(
      conn.value.id,
      redisDb.value,
      reset ? '0' : keyCursor,
      KEY_PAGE_SIZE,
      keyPattern.value.trim() || undefined,
    )
    if (mine !== seq.keys) return
    keys.value = reset ? res.keys : [...keys.value, ...res.keys]
    keyCursor = res.cursor
    keysDone.value = res.cursor === '0'
  } catch {
    if (mine !== seq.keys) return
    error.value = 'key 扫描失败'
  } finally {
    if (mine === seq.keys) keysLoading.value = false
  }
}

async function openKey(item: RedisKeyItem) {
  activeKey.value = item.key
  redisKey.value = null
  pane.value = 'redisKey'
  await loadKeyDetail()
}

async function loadKeyDetail() {
  if (!conn.value) return
  const mine = ++seq.key
  keyLoading.value = true
  error.value = ''
  try {
    const res = await opsApi.redisKeyDetail(conn.value.id, redisDb.value, activeKey.value)
    if (mine !== seq.key) return
    redisKey.value = res
  } catch {
    if (mine !== seq.key) return
    error.value = 'key 内容加载失败'
  } finally {
    if (mine === seq.key) keyLoading.value = false
  }
}

// ---- 导航与展示 --------------------------------------------------------------

function goBack() {
  error.value = ''
  if (pane.value === 'redisKey') {
    pane.value = 'redisKeys'
    redisKey.value = null
    return
  }
  if (pane.value === 'redisKeys' || pane.value === 'tables') {
    pane.value = 'schemas'
    return
  }
  if (pane.value === 'table') {
    pane.value = 'tables'
    return
  }
  conn.value = null
  schema.value = ''
  pane.value = 'connections'
  if (!connections.value.length) void loadConnections()
}

const title = computed(() => {
  switch (pane.value) {
    case 'connections':
      return '数据库'
    case 'schemas':
      return conn.value?.name ?? '数据库'
    case 'tables':
      return schema.value
    case 'table':
      return table.value?.name ?? '表'
    case 'redisKeys':
      return `DB ${redisDb.value}`
    case 'redisKey':
      return activeKey.value
  }
})

const subtitle = computed(() => {
  if (pane.value === 'connections' || !conn.value) return ''
  if (pane.value === 'schemas' || pane.value === 'tables') {
    return `${conn.value.db_type} · ${conn.value.host}:${conn.value.port}`
  }
  return `${conn.value.name} · ${schema.value}`
})

function connAddress(item: OpsDatabase) {
  const suffix =
    item.db_type === 'mysql'
      ? item.db_name
        ? `/${item.db_name}`
        : ''
      : item.db_name
        ? `/db${item.db_name}`
        : ''
  return `${item.host}:${item.port}${suffix}`
}

function openAi() {
  if (!conn.value) return
  void router.push({ name: 'OpsDatabaseChat', params: { id: conn.value.id } })
}

function openAiFrom(item: OpsDatabase) {
  void router.push({ name: 'OpsDatabaseChat', params: { id: item.id } })
}

function formatCell(value: unknown): string {
  if (value === null) return 'NULL'
  if (value === undefined) return ''
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return String(value)
}

/** Redis 的 hash/zset 元素是两元数组，模板里不写 TS 断言，统一从这里取。 */
function pairItems(pair: unknown): unknown[] {
  return Array.isArray(pair) ? pair : []
}

function jsonText(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function ttlText(ttl: number) {
  if (ttl === -1) return '永不过期'
  if (ttl === -2) return 'key 不存在'
  return `${ttl} 秒`
}
</script>

<style scoped>
/* 项目没有全局盒模型重置：整页高度 + padding 的元素在 content-box 下会撑出
   一截，这里统一 border-box。 */
.dbm,
.dbm * {
  box-sizing: border-box;
}

.dbm {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--paper);
}

.dbm-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  padding: 8px 10px;
  background: var(--surface);
  border-bottom: 1px solid var(--hairline);
}

.head-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-2);
  font-size: var(--fs-lg);
  cursor: pointer;
}

.head-btn:active {
  background: var(--surface-2);
}

.head-main {
  flex: 1;
  min-width: 0;
}

.head-title {
  overflow: hidden;
  color: var(--text-1);
  font-size: var(--fs-base);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.head-sub {
  margin-top: 1px;
  overflow: hidden;
  color: var(--text-3);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ai-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  flex-shrink: 0;
  padding: 0 10px;
  border: 1px solid var(--signal-border);
  border-radius: var(--radius-md);
  background: var(--signal-bg);
  color: var(--signal-text);
  font-size: var(--fs-sm);
  cursor: pointer;
}

.ai-btn:active {
  background: var(--signal-bg-strong);
}

.dbm-body {
  flex: 1;
  min-height: 0;
  padding: 10px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.filters :deep(.ant-select) {
  width: 92px;
  flex-shrink: 0;
}

.error-box {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-md);
  background: var(--danger-bg);
  color: var(--danger);
  font-size: var(--fs-sm);
}

.error-box span {
  flex: 1;
  min-width: 0;
}

.card-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.conn-card,
.obj-card {
  display: block;
  width: 100%;
  padding: 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.conn-card:active,
.obj-card:active {
  background: var(--surface-2);
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.conn-name {
  overflow: hidden;
  color: var(--text-1);
  font-size: var(--fs-lg);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-addr {
  margin-top: 4px;
  color: var(--text-2);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--fs-sm);
  word-break: break-all;
}

.conn-meta {
  display: flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.state-dot {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: 50%;
}

.state-dot.ok {
  background: var(--success);
}

.state-dot.fail {
  background: var(--danger);
}

.dot-sep {
  color: var(--text-3);
}

.conn-remark {
  margin-top: 6px;
  color: var(--text-2);
  font-size: var(--fs-xs);
}

.conn-error {
  margin-top: 6px;
  color: var(--danger);
  font-size: var(--fs-xs);
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
  padding-top: 9px;
  border-top: 1px dashed var(--hairline);
}

.foot-hint {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.foot-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  color: var(--signal-text);
  font-size: var(--fs-xs);
}

.obj-card {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 12px;
}

.obj-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  border-radius: var(--radius-md);
  background: var(--signal-bg);
  color: var(--signal-text);
  font-size: var(--fs-lg);
}

.obj-main {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.obj-name {
  overflow: hidden;
  color: var(--text-1);
  font-size: var(--fs-base);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.key-name {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-weight: 400;
}

.obj-meta {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.obj-comment {
  overflow: hidden;
  color: var(--text-2);
  font-size: var(--fs-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.obj-arrow {
  flex-shrink: 0;
  color: var(--text-3);
}

.segmented {
  margin-bottom: 10px;
}

.row-list,
.col-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.row-card,
.col-card {
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
}

.row-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.row-toggle {
  padding: 2px 4px;
  border: 0;
  background: transparent;
  color: var(--signal-text);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.cell-line {
  display: flex;
  gap: 8px;
  padding: 3px 0;
  border-bottom: 1px dashed var(--hairline);
  font-size: var(--fs-sm);
}

.cell-line:last-child {
  border-bottom: 0;
}

.cell-name {
  width: 34%;
  flex-shrink: 0;
  overflow: hidden;
  color: var(--text-3);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cell-value {
  flex: 1;
  min-width: 0;
  color: var(--text-1);
  word-break: break-word;
  white-space: pre-wrap;
}

.cell-value.is-null {
  color: var(--text-3);
  font-style: italic;
}

.load-more {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 10px;
  padding: 2px 2px 10px;
}

.load-meta {
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.col-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.col-name {
  overflow: hidden;
  color: var(--text-1);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--fs-base);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.col-type {
  margin-top: 3px;
  color: var(--text-2);
  font-size: var(--fs-xs);
}

.col-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 4px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.col-comment {
  margin-top: 4px;
  color: var(--text-2);
  font-size: var(--fs-xs);
}

.key-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
  color: var(--text-2);
  font-size: var(--fs-xs);
}

.truncated {
  color: var(--warning);
}

.key-string {
  margin: 0;
  padding: 12px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  color: var(--text-1);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: var(--fs-sm);
  white-space: pre-wrap;
  word-break: break-word;
}

.pair-list,
.element-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}

.pair-row,
.element-row {
  display: flex;
  gap: 8px;
  padding: 9px 12px;
  border-bottom: 1px dashed var(--hairline);
  font-size: var(--fs-sm);
}

.pair-row:last-child,
.element-row:last-child {
  border-bottom: 0;
}

.pair-field {
  width: 36%;
  flex-shrink: 0;
  color: var(--text-3);
  word-break: break-word;
}

.pair-value,
.element-value {
  flex: 1;
  min-width: 0;
  color: var(--text-1);
  word-break: break-word;
}

.element-index {
  width: 28px;
  flex-shrink: 0;
  color: var(--text-3);
}

/* 手机断点内不再横向挤两个筛选控件间距，触控目标统一放大一档。 */
@media (max-width: 480px) {
  .dbm-body {
    padding: 8px;
  }
  .filters :deep(.ant-select) {
    width: 84px;
  }
}
</style>
