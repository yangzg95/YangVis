<template>
  <div class="tab-page">
    <!-- Navicat 式横向工具栏：执行类在左，收藏/历史在中，AI 在右。 -->
    <div class="toolbar">
      <a-select
        v-if="dbType === 'mysql'"
        v-model:value="selectedSchema"
        class="schema-select"
        size="small"
        show-search
        allow-clear
        placeholder="默认库"
        :loading="schemasLoading"
        :options="schemaOptions"
      />
      <a-tooltip
        :title="hasSelection
          ? `只执行选中的部分（Enter）。${gateHint}`
          : `执行（Enter）。选中一段文本时只执行选中部分；${gateHint}`"
      >
        <a-button
          type="primary"
          size="small"
          :loading="running"
          :disabled="!sql.trim()"
          @click="run"
        >
          <CaretRightOutlined v-if="!running" /> {{ hasSelection ? '执行选中' : '执行' }}
        </a-button>
      </a-tooltip>
      <a-tooltip title="停止执行（断开等待；服务端语句由超时配置兜底收尾）">
        <a-button size="small" danger :disabled="!running" @click="stopRun">
          <StopOutlined /> 停止
        </a-button>
      </a-tooltip>
      <a-tooltip v-if="dbType === 'mysql'" title="格式化编辑器里的 SQL（可 Ctrl+Z 撤销）">
        <a-button size="small" :disabled="!sql.trim()" @click="beautify">
          <FormatPainterOutlined /> 美化 SQL
        </a-button>
      </a-tooltip>
      <a-divider type="vertical" />
      <a-tooltip title="把编辑器里的内容存进收藏夹">
        <a-button size="small" :disabled="!sql.trim()" @click="openSave()">
          <StarOutlined /> 收藏
        </a-button>
      </a-tooltip>
      <a-popover
        v-model:open="favOpen"
        trigger="click"
        placement="bottomLeft"
        :overlay-style="{ width: '360px' }"
        @open-change="onFavOpenChange"
      >
        <template #content>
          <div class="fav-panel">
            <a-spin :spinning="favLoading">
              <a-empty
                v-if="!favorites.length"
                :image="simpleEmpty"
                description="暂无收藏，点上方「收藏」存一条"
              />
              <div v-else class="fav-list">
                <div v-for="fav in favorites" :key="fav.id" class="fav-item">
                  <div class="fav-main" @click="applyFavorite(fav)">
                    <div class="fav-title">
                      <span class="fav-name">{{ fav.title }}</span>
                      <a-tag v-if="fav.database_id === null" class="fav-tag" color="blue">通用</a-tag>
                    </div>
                    <div class="fav-preview">{{ fav.content }}</div>
                  </div>
                  <div class="fav-actions">
                    <a-tooltip title="编辑">
                      <a-button size="small" type="text" @click="openSave(fav)">
                        <EditOutlined />
                      </a-button>
                    </a-tooltip>
                    <a-popconfirm
                      title="删除这条收藏？"
                      ok-text="删除"
                      cancel-text="取消"
                      @confirm="removeFavorite(fav)"
                    >
                      <a-tooltip title="删除">
                        <a-button size="small" type="text" danger>
                          <DeleteOutlined />
                        </a-button>
                      </a-tooltip>
                    </a-popconfirm>
                  </div>
                </div>
              </div>
            </a-spin>
          </div>
        </template>
        <a-button size="small">
          <UnorderedListOutlined /> 收藏夹
        </a-button>
      </a-popover>
      <a-popover
        v-model:open="hisOpen"
        trigger="click"
        placement="bottomLeft"
        :overlay-style="{ width: '420px' }"
        @open-change="onHisOpenChange"
      >
        <template #content>
          <div class="his-panel">
            <a-spin :spinning="hisLoading">
              <a-empty
                v-if="!historyItems.length"
                :image="simpleEmpty"
                description="暂无执行记录"
              />
              <div v-else class="his-list">
                <div
                  v-for="item in historyItems"
                  :key="item.id"
                  class="his-item"
                  @click="applyHistory(item)"
                >
                  <span :class="['his-dot', item.success ? 'ok' : 'fail']" />
                  <div class="his-main">
                    <a-tooltip :title="item.command">
                      <div class="his-sql">{{ item.command }}</div>
                    </a-tooltip>
                    <div class="his-meta">
                      {{ new Date(item.created_at).toLocaleString() }}
                      <span v-if="item.verdict === 'forbidden'" class="his-flag">被安全网关拒绝</span>
                      <span v-else-if="!item.success" class="his-flag">执行失败</span>
                      <span v-else-if="item.verdict === 'write'" class="his-flag write">写操作</span>
                    </div>
                  </div>
                </div>
              </div>
            </a-spin>
          </div>
        </template>
        <a-button size="small">
          <HistoryOutlined /> 历史
        </a-button>
      </a-popover>
      <a-divider type="vertical" />
      <a-tooltip title="把当前 SQL（和最近的报错）带给右侧 AI 分析">
        <a-button size="small" :disabled="!canAskAi" @click="askAi">
          <RobotOutlined /> 询问 AI
        </a-button>
      </a-tooltip>
    </div>

    <div ref="editorBoxRef" class="editor">
      <!-- MySQL 用 CodeMirror（关键字 / 表名 / 字段补全）；Redis 命令没有
           方言支持，保持原 textarea。 -->
      <SqlEditor
        v-if="dbType === 'mysql'"
        ref="editorRef"
        v-model="sql"
        :rows="4"
        :height="editorHeight"
        :placeholder="placeholder"
        @run="run"
        @selection-change="hasSelection = $event"
      />
      <div v-else ref="redisBoxRef" class="redis-editor">
        <a-textarea
          v-model:value="sql"
          :rows="4"
          :style="editorHeight ? { height: `${editorHeight}px` } : undefined"
          :placeholder="placeholder"
          @keydown.enter.exact.prevent="run"
          @select="syncRedisSelection"
          @click="syncRedisSelection"
          @keyup="syncRedisSelection"
        />
      </div>
    </div>

    <!-- 编辑器与底部面板之间的拖拽调条：负边距叠在 flex gap 上，不改变布局。 -->
    <div class="row-resizer" @mousedown="startEditorDrag" />

    <a-modal
      v-model:open="favModal.open"
      :title="favModal.editing ? '编辑收藏' : '收藏 SQL'"
      :confirm-loading="favModal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submitFavorite"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="favModal.form.title" placeholder="例如：查看在线会话" />
        </a-form-item>
        <a-form-item label="内容" required>
          <a-textarea v-model:value="favModal.form.content" :rows="4" class="fav-content" />
        </a-form-item>
        <a-form-item label="适用范围">
          <a-radio-group v-model:value="favModal.form.scope">
            <a-radio value="conn">仅当前连接（{{ connName }}）</a-radio>
            <a-radio value="all">所有连接通用</a-radio>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="备注">
          <a-input v-model:value="favModal.form.remark" />
        </a-form-item>
      </a-form>
    </a-modal>

    <a-alert
      v-if="error"
      class="error"
      type="error"
      show-icon
      :message="error"
    />

    <!-- 底部面板（Navicat 式）：每个有输出的语句一个结果页签，外加消息 / 摘要。 -->
    <div class="bottom-panel">
      <a-tabs v-if="batch" v-model:active-key="bottomActive" size="small" class="bottom-tabs">
        <a-tab-pane v-for="tab in resultTabs" :key="tab.key">
          <template #tab>
            <a-tooltip :title="tab.stmt.sql">结果 {{ tab.stmt.index }}</a-tooltip>
          </template>
          <DbResultGrid :result="tab.stmt" />
        </a-tab-pane>

        <a-tab-pane key="messages" tab="消息">
          <div class="pane-scroll">
            <a-table
              size="small"
              :data-source="batch.statements"
              :columns="MESSAGE_COLUMNS"
              :pagination="false"
              row-key="index"
              :custom-row="messageRow"
            >
              <template #bodyCell="{ column, record }">
                <template v-if="column.key === 'sql'">
                  <a-tooltip :title="record.sql" :mouse-enter-delay="0.5">
                    <span class="msg-sql">#{{ record.index }} {{ record.sql }}</span>
                  </a-tooltip>
                </template>
                <template v-else-if="column.key === 'message'">
                  <a-tooltip :title="record.message" :mouse-enter-delay="0.5">
                    <span :class="['msg-text', record.status]">
                      {{ record.status === 'ok' ? `OK，${record.message}` : record.message }}
                    </span>
                  </a-tooltip>
                </template>
                <template v-else-if="column.key === 'elapsed_ms'">
                  {{ (record.elapsed_ms / 1000).toFixed(3) }} 秒
                </template>
              </template>
            </a-table>
          </div>
        </a-tab-pane>

        <a-tab-pane key="summary" tab="摘要">
          <div class="pane-scroll">
            <a-descriptions bordered size="small" :column="2">
              <a-descriptions-item label="已处理的查询">{{ batch.total }}</a-descriptions-item>
              <a-descriptions-item label="开始时间">{{ fmtTime(batch.started_at) }}</a-descriptions-item>
              <a-descriptions-item label="成功">{{ batch.succeeded }}</a-descriptions-item>
              <a-descriptions-item label="结束时间">{{ fmtTime(batch.finished_at) }}</a-descriptions-item>
              <a-descriptions-item label="错误">{{ batch.failed }}</a-descriptions-item>
              <a-descriptions-item label="运行时间">{{ (batch.elapsed_ms / 1000).toFixed(3) }} 秒</a-descriptions-item>
            </a-descriptions>
          </div>
        </a-tab-pane>
      </a-tabs>

      <div v-else-if="!error" class="empty">
        <div>结果会显示在这里。示例：</div>
        <div class="samples">
          <button
            v-for="sample in samples"
            :key="sample"
            type="button"
            class="sample"
            @click="sql = sample"
          >
            {{ sample }}
          </button>
        </div>
      </div>
    </div>

    <!-- Navicat 式底部栏：展示最近一次执行的 SQL，悬停看全文，可一键复制。 -->
    <div v-if="executedSql" class="sql-bar">
      <span class="sql-bar-label">SQL</span>
      <a-tooltip :title="executedSql" :mouse-enter-delay="0.3">
        <span class="sql-bar-text">{{ executedSql }}</span>
      </a-tooltip>
      <a-tooltip title="复制 SQL">
        <a-button size="small" type="text" class="sql-bar-copy" @click="copyExecutedSql">
          <CopyOutlined />
        </a-button>
      </a-tooltip>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Empty, message as toast } from 'ant-design-vue'
import {
  CaretRightOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  FormatPainterOutlined,
  HistoryOutlined,
  RobotOutlined,
  StarOutlined,
  StopOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons-vue'
import {
  opsApi,
  type DatabaseType,
  type DbBatchResult,
  type DbBatchStatement,
  type DbCompletionTable,
  type DbExecuteResult,
  type OpsAuditItem,
  type SqlFavorite,
} from '@/api'
import DbResultGrid from './DbResultGrid.vue'
import SqlEditor from './SqlEditor.vue'
import { copyText } from './grid'
import { clampSize, readSize, saveSize, startDragResize } from './resizer'
import { storageKeys } from '@/utils/storage'
import { formatSql } from '@/utils/sqlFormat'
import './resizer.css'

const props = defineProps<{
  connId: number
  dbType: DatabaseType
  connName: string
  /** 连接是否开了「允许写入」：只影响提示文案，真正的判定在服务端。 */
  writable?: boolean
  /** 打开页签时带进来的默认库（从库节点「新建查询」或台账默认库）。 */
  schema?: string
  /** 预填进编辑器的 SQL（AI 问答里带过来的）。 */
  initialSql?: string
}>()

const emit = defineEmits<{
  /** 「询问 AI」：把当前 SQL（和最近一次错误、默认库）带到右侧 AI 面板提问。 */
  (e: 'ask-ai', payload: { connId: number; sql: string; error?: string; schema?: string }): void
}>()

const SAMPLES: Record<DatabaseType, string[]> = {
  mysql: ['SHOW TABLES', 'SHOW TABLE STATUS', 'SELECT * FROM information_schema.processlist LIMIT 20'],
  redis: ['INFO memory', 'DBSIZE', 'SCAN 0 COUNT 20'],
}

const sql = ref(props.initialSql ?? '')
const running = ref(false)
const batch = ref<DbBatchResult | null>(null)
const error = ref('')

// 底部 SQL 栏（Navicat 同款）：记最近一次送出去的完整命令，执行失败也保留展示。
const executedSql = ref('')

async function copyExecutedSql() {
  if (await copyText(executedSql.value)) toast.success('SQL 已复制')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

// 默认库：执行时作为连接参数传给后端，SQL 里就不用写库名前缀了。
const selectedSchema = ref<string | undefined>(props.schema)
const schemaOptions = ref<{ value: string; label: string }[]>([])
const schemasLoading = ref(false)

onMounted(async () => {
  if (props.dbType !== 'mysql') return
  schemasLoading.value = true
  try {
    const res = await opsApi.dbSchemas(props.connId)
    schemaOptions.value = res.items
      .filter((item) => item.kind === 'schema')
      .map((item) => ({ value: item.name, label: item.name }))
  } catch {
    // 列表拉不到不阻塞查询：选择器留空，用户还可以手写全限定名。
  } finally {
    schemasLoading.value = false
  }
})

// ---- 编辑器补全（关键字由方言自带，这里只管表名和字段） --------------------------

const editorRef = ref<InstanceType<typeof SqlEditor> | null>(null)

/** 已拉过的 schema 补全缓存：库结构不会秒级变化，来回切默认库不用重拉。 */
const completionCache = new Map<string, DbCompletionTable[]>()

async function loadCompletion(schema: string) {
  if (!schema) return
  let tables = completionCache.get(schema)
  if (!tables) {
    try {
      const res = await opsApi.dbCompletion(props.connId, schema)
      tables = res.items
      completionCache.set(schema, tables)
    } catch {
      // 拉不到补全数据只少个提示，不打扰查询本身。
      return
    }
  }
  editorRef.value?.setSchema(tables)
}

// SqlEditor 是 v-if 挂的，编辑器实例出现、默认库变化两个时机都要灌一次数据。
watch(editorRef, (editor) => {
  if (editor && selectedSchema.value) void loadCompletion(selectedSchema.value)
})

watch(selectedSchema, (schema) => {
  if (schema) void loadCompletion(schema)
})

const samples = computed(() => SAMPLES[props.dbType])
const placeholder = computed(() =>
  props.dbType === 'mysql' ? 'SELECT * FROM orders LIMIT 10' : 'SCAN 0 COUNT 20',
)

/** 执行按钮 tooltip 里的网关说明：随连接的写开关切换，判定本身在服务端。 */
const gateHint = computed(() =>
  props.writable ? '已开启写入，高危命令仍会被拦截' : '只读网关仍然生效',
)

// ---- 编辑器与底部面板的拖拽调高 ----------------------------------------------------
// 拖过一次之后编辑器改用显式高度（px），不再随内容自动伸缩；偏好记 localStorage。

const EDITOR_HEIGHT_KEY = storageKeys.opsDbEditorHeight

const editorBoxRef = ref<HTMLElement | null>(null)
const editorHeight = ref<number | null>(readSize(EDITOR_HEIGHT_KEY, 0) || null)

// 上限取页面高度的一小半而不是写死像素：编辑器再大也不许把结果区压成配角，
// 窗口变矮后已存的高度同样会被这个比例钳回来。
function editorMax(): number {
  const pageH = editorBoxRef.value?.parentElement?.getBoundingClientRect().height ?? 0
  return pageH ? Math.max(120, Math.min(420, Math.round(pageH * 0.45))) : 320
}

/** 超过当前上限就收回来（旧版本可能存了过高的值；窗口变矮也会超限）。 */
function clampEditorHeight() {
  if (editorHeight.value != null && editorHeight.value > editorMax()) {
    editorHeight.value = editorMax()
  }
}

function startEditorDrag(event: MouseEvent) {
  // 还没拖过时编辑器是自动高度，以当前实测高度为起点接着拖。
  const base = editorHeight.value ?? editorBoxRef.value?.getBoundingClientRect().height ?? 96
  // 每个 mousemove 都改高度会连着重排编辑器 + 结果表格（很卡）：
  // 合到一帧里做一次，mouseup 时再落最终值。
  let raf = 0
  let pendingDelta = 0
  const apply = () => {
    editorHeight.value = clampSize(base + pendingDelta, 56, editorMax())
  }
  startDragResize(
    event,
    'y',
    (delta) => {
      pendingDelta = delta
      if (raf) return
      raf = requestAnimationFrame(() => {
        raf = 0
        apply()
      })
    },
    () => {
      if (raf) cancelAnimationFrame(raf)
      apply()
      saveSize(EDITOR_HEIGHT_KEY, editorHeight.value ?? 0)
    },
  )
}

onMounted(async () => {
  // 挂载后量出真实容器高，把旧版本存下的超限高度一次性钳回来。
  await nextTick()
  clampEditorHeight()
  // window resize 不会因自身布局变化触发，没有 ResizeObserver 的循环风险。
  window.addEventListener('resize', clampEditorHeight)
})

onBeforeUnmount(() => window.removeEventListener('resize', clampEditorHeight))

// ---- 选中单条执行 -------------------------------------------------------------------
// 有选区就只执行选中的那一段；CodeMirror 通过 selection-change 事件同步状态，
// Redis 的 textarea 走原生选区 API 现取。

const hasSelection = ref(false)
const redisBoxRef = ref<HTMLElement | null>(null)

function redisTextarea(): HTMLTextAreaElement | null {
  return redisBoxRef.value?.querySelector('textarea') ?? null
}

function syncRedisSelection() {
  const el = redisTextarea()
  hasSelection.value = !!el && el.selectionEnd > el.selectionStart
}

/** 编辑器里当前选中的文本；两种编辑器统一走这一个口子。 */
function currentSelection(): string {
  if (props.dbType === 'mysql') return editorRef.value?.getSelection() ?? ''
  const el = redisTextarea()
  return el ? el.value.slice(el.selectionStart, el.selectionEnd) : ''
}

// ---- 底部面板：结果页签 / 消息 / 摘要 ------------------------------------------------

const bottomActive = ref('messages')

/** 有输出（结果集或文本）的成功句才有对应的结果页签。 */
function hasOutput(stmt: DbBatchStatement): boolean {
  return stmt.status === 'ok' && (stmt.columns.length > 0 || stmt.text != null)
}

const resultTabs = computed(() =>
  (batch.value?.statements ?? [])
    .filter(hasOutput)
    .map((stmt) => ({ key: `result-${stmt.index}`, stmt })),
)

const MESSAGE_COLUMNS = [
  { title: '查询', dataIndex: 'sql', key: 'sql', ellipsis: true },
  { title: '消息', dataIndex: 'message', key: 'message', width: 240, ellipsis: true },
  { title: '查询时间', dataIndex: 'elapsed_ms', key: 'elapsed_ms', width: 110 },
]

/** 消息行点击跳到对应结果页签（Navicat 同款交互）；没输出的行点了不动。 */
function messageRow(record: DbBatchStatement) {
  return {
    class: hasOutput(record) ? 'msg-row-link' : '',
    onClick: () => {
      if (hasOutput(record)) bottomActive.value = `result-${record.index}`
    },
  }
}

function fmtTime(iso: string): string {
  return new Date(iso).toLocaleString()
}

// ---- 执行 / 停止 --------------------------------------------------------------------
// 客户端的「停止」只是断开等待；服务端的执行线程由 MAX_EXECUTION_TIME /
// OPS_CMD_TIMEOUT 兜底收尾，后端不需要改动。

let abortCtl: AbortController | null = null

function stopRun() {
  abortCtl?.abort()
}

async function run() {
  // 有选区就只跑选中的那一段；没选区执行编辑器里的全部内容。
  // 选中的文本本身可以是多句脚本，拆分在后端做，前端原样送。
  const selected = currentSelection().trim()
  const command = (selected || sql.value).trim()
  if (!command || running.value) return
  executedSql.value = command
  running.value = true
  error.value = ''
  batch.value = null
  abortCtl = new AbortController()
  try {
    if (props.dbType === 'mysql') {
      batch.value = await opsApi.executeBatch(
        props.connId, command, selectedSchema.value, { signal: abortCtl.signal },
      )
      activateBottom(batch.value)
    } else {
      const res = await opsApi.execute(props.connId, command, undefined, { signal: abortCtl.signal })
      batch.value = wrapSingleOk(command, res)
      bottomActive.value = 'result-1'
    }
  } catch (err: any) {
    if (err?.name === 'CanceledError' || err?.code === 'ERR_CANCELED') {
      error.value = '已停止执行'
    } else if (props.dbType === 'mysql') {
      // 批量端点抛错是连接级 / 整批级失败（单句失败在 batch.statements 里）。
      error.value = err?.message || '执行失败'
    } else {
      // Redis 失败也包成 batch-of-1，底部面板走同一条渲染路径。
      const message = err?.message || '执行失败'
      batch.value = wrapSingleError(command, message)
      error.value = message
      bottomActive.value = 'messages'
    }
  } finally {
    running.value = false
    abortCtl = null
    // 后端对每次执行（含失败和被拒绝）都留痕，弹层开着就顺手刷新。
    if (hisOpen.value) void loadHistory()
  }
}

/** 执行完落哪个页签：多句落消息页；单句成功直落结果页，失败照旧弹错误条。 */
function activateBottom(result: DbBatchResult) {
  if (result.total === 1) {
    const stmt = result.statements[0]
    if (stmt.status === 'ok') {
      bottomActive.value = 'result-1'
    } else {
      // 被只读网关挡下来也走这里，原因直接展示比弹 toast 更有用。
      error.value = stmt.message
      bottomActive.value = 'messages'
    }
  } else {
    bottomActive.value = 'messages'
  }
}

// ---- Redis 结果包成 batch-of-1 ------------------------------------------------------
// 底部面板一条渲染路径覆盖两种连接，不需要 Redis 特判。

function previewOf(command: string): string {
  const text = command.replace(/\s+/g, ' ').trim()
  return text.length <= 200 ? text : `${text.slice(0, 200)}…`
}

function wrapSingleOk(command: string, res: DbExecuteResult): DbBatchResult {
  const now = new Date().toISOString()
  return {
    statements: [{
      index: 1,
      sql: previewOf(command),
      status: 'ok',
      message: res.columns.length ? `${res.row_count} 行` : '执行完成',
      elapsed_ms: res.elapsed_ms,
      columns: res.columns,
      rows: res.rows,
      row_count: res.row_count,
      truncated: res.truncated,
      text: res.text ?? null,
    }],
    total: 1,
    succeeded: 1,
    failed: 0,
    started_at: now,
    finished_at: now,
    elapsed_ms: res.elapsed_ms,
  }
}

function wrapSingleError(command: string, message: string): DbBatchResult {
  const now = new Date().toISOString()
  return {
    statements: [{
      index: 1,
      sql: previewOf(command),
      status: 'error',
      message,
      elapsed_ms: 0,
      columns: [],
      rows: [],
      row_count: 0,
      truncated: false,
      text: null,
    }],
    total: 1,
    succeeded: 0,
    failed: 1,
    started_at: now,
    finished_at: now,
    elapsed_ms: 0,
  }
}

// ---- 美化 SQL ------------------------------------------------------------------------

function beautify() {
  const text = sql.value
  if (!text.trim()) return
  try {
    // 可撤销性依赖 SqlEditor 的 modelValue watcher：外部赋值是单事务全量
    // 替换，进 CodeMirror 的 undo history，Ctrl+Z 能回到美化前。
    sql.value = formatSql(text)
  } catch {
    // 格式化器对残句 / 方言边角会抛错：不改内容，只提示。
    toast.warning('SQL 无法解析，未做修改')
  }
}

// ---- 询问 AI -------------------------------------------------------------------------

const canAskAi = computed(() => !!sql.value.trim() || !!error.value)

function askAi() {
  emit('ask-ai', {
    connId: props.connId,
    sql: sql.value.trim(),
    error: error.value || undefined,
    schema: selectedSchema.value,
  })
}

// ---- 执行历史 -------------------------------------------------------------------
// 直接复用后端审计表：每次执行都会留一条，这里按当前连接过滤出来回填编辑器。

const historyItems = ref<OpsAuditItem[]>([])
const hisLoading = ref(false)
const hisOpen = ref(false)

function onHisOpenChange(open: boolean) {
  if (open) void loadHistory()
}

async function loadHistory() {
  hisLoading.value = true
  try {
    const res = await opsApi.audit({ target_type: 'database', target_id: props.connId, limit: 50 })
    historyItems.value = res.items
  } finally {
    hisLoading.value = false
  }
}

/** 和收藏一样：只回填编辑器，不直接执行——先看清要跑什么。 */
function applyHistory(item: OpsAuditItem) {
  sql.value = item.command
  hisOpen.value = false
}

// ---- SQL 收藏 ----------------------------------------------------------------

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const favorites = ref<SqlFavorite[]>([])
const favLoading = ref(false)
const favOpen = ref(false)

const favModal = reactive({
  open: false,
  loading: false,
  /** 为 null 表示新建；否则是被编辑的那条收藏。 */
  editing: null as SqlFavorite | null,
  form: { title: '', content: '', scope: 'conn' as 'conn' | 'all', remark: '' },
})

function onFavOpenChange(open: boolean) {
  if (open) void loadFavorites()
}

async function loadFavorites() {
  favLoading.value = true
  try {
    const res = await opsApi.listSqlFavorites(props.connId)
    favorites.value = res.items
  } finally {
    favLoading.value = false
  }
}

/** 打开收藏弹窗：新建时预填编辑器里的内容，编辑时回填整条收藏。 */
function openSave(fav?: SqlFavorite) {
  favModal.editing = fav ?? null
  favModal.form = fav
    ? {
        title: fav.title,
        content: fav.content,
        scope: fav.database_id === null ? 'all' : 'conn',
        remark: fav.remark ?? '',
      }
    : { title: '', content: sql.value.trim(), scope: 'conn', remark: '' }
  favModal.open = true
}

async function submitFavorite() {
  const form = favModal.form
  if (!form.title.trim() || !form.content.trim()) {
    toast.warning('名称和内容不能为空')
    return
  }
  const payload = {
    title: form.title.trim(),
    content: form.content.trim(),
    database_id: form.scope === 'conn' ? props.connId : null,
    remark: form.remark.trim() || null,
  }
  favModal.loading = true
  try {
    if (favModal.editing) await opsApi.updateSqlFavorite(favModal.editing.id, payload)
    else await opsApi.createSqlFavorite(payload)
    favModal.open = false
    toast.success('已保存')
    await loadFavorites()
  } finally {
    favModal.loading = false
  }
}

/** 和示例一样：只填进编辑器，不直接执行——先看清要跑什么。 */
function applyFavorite(fav: SqlFavorite) {
  sql.value = fav.content
  favOpen.value = false
}

async function removeFavorite(fav: SqlFavorite) {
  await opsApi.removeSqlFavorite(fav.id)
  toast.success('已删除')
  await loadFavorites()
}

defineExpose({
  /** 外部（全屏智能问答页）把一段 SQL 填进编辑器：只填不跑，先看清要执行什么。 */
  setSql(text: string) {
    sql.value = text
  },
  /** 编辑器里还有没有内容：工作台关页签前用它决定要不要拦一下确认。 */
  isDirty: () => sql.value.trim().length > 0,
})
</script>

<style scoped>
.tab-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  gap: 8px;
}

/* Navicat 式横向工具栏：窄屏允许换行，执行类按钮始终在最前。 */
.toolbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.schema-select {
  width: 160px;
}

.editor {
  display: flex;
  align-items: flex-start;
  /* 拖出显式高度后不允许 flex 再把它压回去。 */
  flex-shrink: 0;
}

.editor :deep(textarea) {
  font-family: var(--font-mono);
  resize: none;
}

/* Redis 的 textarea 没有组件级 ref 可拿选区，包一层 div 用来 querySelector。 */
.redis-editor {
  flex: 1;
  min-width: 0;
  display: flex;
}

.redis-editor :deep(textarea) {
  flex: 1;
}

/* 编辑器 / 底部面板之间的拖拽条：热区用负边距叠进 flex gap，视觉上不占位。 */
.row-resizer {
  position: relative;
  z-index: 5;
  flex-shrink: 0;
  height: 8px;
  margin: -8px 0;
  display: flex;
  align-items: center;
  cursor: row-resize;
}

.row-resizer::after {
  content: '';
  width: 100%;
  height: 2px;
  border-radius: 1px;
  background: transparent;
  transition: background 0.15s;
}

.row-resizer:hover::after,
body.dragging-row .row-resizer::after {
  background: var(--signal-border, #91caff);
}

.error {
  flex-shrink: 0;
}

/* 底部面板：页签占满剩余空间，滚动在各页签内部。 */
.bottom-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.bottom-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.bottom-tabs :deep(.ant-tabs-nav) {
  margin-bottom: 8px;
}

.bottom-tabs :deep(.ant-tabs-content-holder) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.bottom-tabs :deep(.ant-tabs-content),
.bottom-tabs :deep(.ant-tabs-tabpane) {
  height: 100%;
}

/* 结果网格靠 grid-wrap 的 flex:1 撑满，但 tab-pane 默认是 block，flex:1 会失效
   （表格只剩初始高度，下面一大片空白）。把激活页签改成 flex 列；只选 -active，
   避免盖掉隐藏页签的 display:none。 */
.bottom-tabs :deep(.ant-tabs-tabpane-active) {
  display: flex;
  flex-direction: column;
}

/* 消息 / 摘要页签内部自己滚，结果页签的滚动由表格自身（scroll.y）负责。 */
.pane-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.msg-sql {
  font-family: var(--font-mono);
  font-size: 12px;
}

.msg-text.ok {
  color: #52c41a;
}

.msg-text.error {
  color: #ff4d4f;
}

/* 有结果页签可跳的消息行，光标表明可点。 */
.bottom-tabs :deep(.msg-row-link) {
  cursor: pointer;
}

.empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--text-3);
  font-size: 13px;
}

.samples {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 8px;
}

/* 例子点一下只是填进编辑器，不直接执行——让用户先看清要跑什么。 */
.sample {
  padding: 5px 10px;
  border: 1px solid #d9d9d9;
  border-radius: 6px;
  background: #fafafa;
  color: var(--signal-text);
  font-family: var(--font-mono);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.sample:hover {
  border-color: var(--signal-border);
  background: var(--signal-bg);
}

.fav-content :deep(textarea),
.fav-content {
  font-family: var(--font-mono);
}

/* 收藏夹弹层：整行可点（填入编辑器），右侧悬停出编辑 / 删除。 */
.fav-panel {
  max-height: 360px;
  overflow-y: auto;
}

.fav-item {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  padding: 6px 8px;
  border-radius: 6px;
  transition: background 0.15s;
}

.fav-item:hover {
  background: #f5f5f5;
}

.fav-main {
  flex: 1;
  min-width: 0;
  cursor: pointer;
}

.fav-title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.fav-name {
  font-size: 13px;
  font-weight: 500;
}

.fav-tag {
  margin-inline-end: 0;
  line-height: 16px;
  font-size: 11px;
}

.fav-preview {
  margin-top: 2px;
  color: var(--text-3);
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fav-actions {
  flex-shrink: 0;
  visibility: hidden;
}

.fav-item:hover .fav-actions {
  visibility: visible;
}

/* 执行历史弹层：整行可点（回填编辑器），左侧圆点标成功 / 失败。 */
.his-panel {
  max-height: 360px;
  overflow-y: auto;
}

.his-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;
}

.his-item:hover {
  background: #f5f5f5;
}

.his-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  margin-top: 5px;
  border-radius: 50%;
}

.his-dot.ok {
  background: #52c41a;
}

.his-dot.fail {
  background: #ff4d4f;
}

.his-main {
  flex: 1;
  min-width: 0;
}

.his-sql {
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.his-meta {
  margin-top: 2px;
  color: var(--text-3);
  font-size: 11px;
}

.his-flag {
  margin-left: 6px;
  color: #ff4d4f;
}

.his-flag.write {
  color: #fa8c16;
}
</style>
