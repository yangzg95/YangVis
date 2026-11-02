<template>
  <div class="tab-page">
    <div class="toolbar">
      <span class="crumb">
        <FolderOutlined /> {{ schema }} <span class="sep">/</span>
        <TableOutlined /> {{ table }}
      </span>
      <a-segmented
        v-model:value="view"
        size="small"
        :options="[
          { label: '字段', value: 'columns' },
          { label: countLabel('indexes', '索引'), value: 'indexes' },
          { label: countLabel('foreignKeys', '外键'), value: 'foreignKeys' },
        ]"
      />
      <span class="spacer" />
      <a-tooltip title="查看建表语句">
        <a-button size="small" type="text" @click="openDdl">
          <CodeOutlined />
        </a-button>
      </a-tooltip>
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <div ref="gridWrapRef" class="grid-wrap">
      <!-- 字段 -->
      <a-table
        v-if="view === 'columns'"
        class="grid db-grid"
        size="small"
        :data-source="columns"
        :columns="COLUMN_GRID"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 'max-content', y: scrollY }"
        row-key="name"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span class="col-name">{{ record.name }}</span>
            <a-tag v-if="record.column_key === 'PRI'" color="gold" class="key-tag">PK</a-tag>
            <a-tag v-else-if="record.column_key" class="key-tag">{{ record.column_key }}</a-tag>
          </template>
          <template v-else-if="column.key === 'nullable'">
            {{ record.nullable ? 'YES' : 'NO' }}
          </template>
          <template v-else-if="column.key === 'default'">
            <span :class="{ 'is-null': record.default == null }">
              {{ record.default == null ? 'NULL' : record.default }}
            </span>
          </template>
        </template>
      </a-table>

      <!-- 索引：主键排最前，联合索引按列序展示 -->
      <a-table
        v-else-if="view === 'indexes'"
        class="grid db-grid"
        size="small"
        :data-source="indexes"
        :columns="INDEX_GRID"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 'max-content', y: scrollY }"
        row-key="name"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'name'">
            <span class="col-name">{{ record.name }}</span>
            <a-tag v-if="record.primary" color="gold" class="key-tag">PK</a-tag>
            <a-tag v-else-if="record.unique" class="key-tag">UNIQUE</a-tag>
          </template>
          <template v-else-if="column.key === 'columns'">
            <span class="idx-cols">{{ record.columns.join(', ') }}</span>
          </template>
        </template>
      </a-table>

      <!-- 外键：只读。改外键回查询页签手写 DDL，误删一条约束的代价不该由表单承担。 -->
      <a-table
        v-else
        class="grid db-grid"
        size="small"
        :data-source="foreignKeys"
        :columns="FK_GRID"
        :loading="loading"
        :pagination="false"
        :scroll="{ x: 'max-content', y: scrollY }"
        row-key="name"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'ref'">
            <span class="fk-ref">{{ record.ref_schema }}.{{ record.ref_table }}</span>
            <span class="fk-cols">({{ record.ref_columns.join(', ') }})</span>
          </template>
          <template v-else-if="column.key === 'rules'">
            <a-tag class="rule-tag">UPDATE {{ record.on_update || 'NO ACTION' }}</a-tag>
            <a-tag class="rule-tag">DELETE {{ record.on_delete || 'NO ACTION' }}</a-tag>
          </template>
        </template>
      </a-table>
    </div>

    <a-drawer
      v-model:open="ddl.open"
      :title="`${schema}.${table} 建表语句`"
      placement="right"
      :width="Math.min(760, Math.round(windowWidth * 0.8))"
      :body-style="{ paddingTop: '12px' }"
    >
      <div class="ddl-toolbar">
        <a-button size="small" :disabled="!ddl.text" @click="copyDdl">
          <CopyOutlined /> 复制
        </a-button>
        <a-button size="small" :disabled="!ddl.text || formatting" :loading="formatting" @click="formatDdl">
          <AlignLeftOutlined /> 格式化
        </a-button>
        <span class="ddl-hint">只读；改结构请用「设计表」。</span>
      </div>
      <a-spin :spinning="ddl.loading" wrapper-class-name="ddl-spin">
        <pre v-if="ddl.text" class="ddl-text">{{ ddl.text }}</pre>
        <a-empty v-else-if="!ddl.loading" :description="ddl.error || '没有可读的建表语句'" />
      </a-spin>
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import {
  AlignLeftOutlined,
  CodeOutlined,
  CopyOutlined,
  FolderOutlined,
  ReloadOutlined,
  TableOutlined,
} from '@ant-design/icons-vue'
import { message as toast } from 'ant-design-vue'
import { formatSql } from '@/utils/sqlFormat'
import {
  opsApi,
  type DbColumnItem,
  type DbForeignKeyItem,
  type DbIndexItem,
} from '@/api'
import { copyText } from '@/utils/clipboard'
import { useGridScrollY } from './useGridScrollY'
import './grid.css'

const props = defineProps<{
  connId: number
  schema: string
  table: string
}>()

type StructureView = 'columns' | 'indexes' | 'foreignKeys'

const COLUMN_GRID = [
  { title: '列名', key: 'name', dataIndex: 'name' },
  { title: '类型', key: 'column_type', dataIndex: 'column_type' },
  { title: '可空', key: 'nullable', dataIndex: 'nullable', width: 70 },
  { title: '默认值', key: 'default', dataIndex: 'default' },
  { title: 'Extra', key: 'extra', dataIndex: 'extra' },
  { title: '注释', key: 'comment', dataIndex: 'comment', ellipsis: true },
]

const INDEX_GRID = [
  { title: '索引名', key: 'name', dataIndex: 'name' },
  { title: '列', key: 'columns', dataIndex: 'columns', ellipsis: true },
  { title: '类型', key: 'index_type', dataIndex: 'index_type', width: 96 },
  { title: '基数', key: 'cardinality', dataIndex: 'cardinality', width: 96 },
  { title: '注释', key: 'comment', dataIndex: 'comment', ellipsis: true },
]

const FK_GRID = [
  { title: '约束名', key: 'name', dataIndex: 'name' },
  { title: '本表列', key: 'columns', customRender: ({ record }: any) => record.columns.join(', ') },
  { title: '引用', key: 'ref' },
  { title: '级联规则', key: 'rules' },
]

const view = ref<StructureView>('columns')

const columns = ref<DbColumnItem[]>([])
const indexes = ref<DbIndexItem[]>([])
const foreignKeys = ref<DbForeignKeyItem[]>([])

const loading = ref(false)

const counts = reactive<Record<StructureView, number | null>>({
  columns: null,
  indexes: null,
  foreignKeys: null,
})

function countLabel(key: StructureView, text: string) {
  const n = counts[key]
  return n == null ? text : `${text} (${n})`
}

// scroll.y 实测容器剩余空间（无分页），写死像素会在窗口变矮时顶出容器。
const gridWrapRef = ref<HTMLElement | null>(null)
const { scrollY, measureScrollY } = useGridScrollY(gridWrapRef, { pagination: false })

// 抽屉宽度跟着视口收窄，窄屏下不留出一条压不住的窄缝。
const windowWidth = ref(window.innerWidth)
function onResize() {
  windowWidth.value = window.innerWidth
}

/**
 * 结构页签一次拉齐列、索引、外键：后端复用同一条只读连接，比拆三次请求
 * 少两次建连；切换视图纯前端渲染，不再补请求。
 */
async function load() {
  loading.value = true
  try {
    const res = await opsApi.dbTableStructure(props.connId, props.schema, props.table)
    columns.value = res.columns
    indexes.value = res.indexes
    foreignKeys.value = res.foreign_keys
    counts.columns = res.columns.length
    counts.indexes = res.indexes.length
    counts.foreignKeys = res.foreign_keys.length
    // 表头随数据渲染出来后才占高度，等量一下再重算表格体高度。
    await nextTick()
    measureScrollY()
  } catch (err: any) {
    toast.error(err?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

// ---- 建表语句 ---------------------------------------------------------------

const ddl = reactive({ open: false, loading: false, text: '', error: '' })
let ddlLoaded = false

async function openDdl() {
  ddl.open = true
  if (ddlLoaded) return
  ddl.loading = true
  ddl.error = ''
  try {
    const res = await opsApi.dbTableDdl(props.connId, props.schema, props.table)
    ddl.text = res.ddl
    ddlLoaded = true
  } catch (err: any) {
    ddl.error = err?.message || '读取失败'
  } finally {
    ddl.loading = false
  }
}

async function copyDdl() {
  if (await copyText(ddl.text)) toast.success('已复制建表语句')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

// 格式化整段 DDL 是同步 CPU 活，大表下会卡主线程；推迟到 300ms 后再跑，
//
// 期间按钮显示 loading，连续点击也只会落一次。
const formatting = ref(false)
let formatTimer: ReturnType<typeof setTimeout> | null = null

function formatDdl() {
  if (formatTimer) clearTimeout(formatTimer)
  formatting.value = true
  formatTimer = setTimeout(() => {
    formatTimer = null
    try {
      // 只格式化展示，取回的原文仍是 SHOW CREATE TABLE 的输出。
      ddl.text = formatSql(ddl.text)
    } catch {
      toast.warning('这段语句没能格式化，保持原样')
    } finally {
      formatting.value = false
    }
  }, 300)
}

onMounted(() => {
  void load()
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  if (formatTimer) clearTimeout(formatTimer)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.tab-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* 纵向节奏与 .crumb/.sep/.spacer 走全局 .tab-page 规则（style.css）。 */

.grid-wrap {
  flex: 1;
  min-height: 0;
  /* 纵向滚动由表格自身（scroll.y）负责，外层不再滚，避免出现双滚动条。 */
  overflow: hidden;
}

.col-name {
  font-weight: 500;
  margin-right: 6px;
}

.key-tag {
  font-size: var(--fs-xs);
  line-height: 16px;
  padding: 0 4px;
}

.is-null {
  color: var(--text-3);
  font-style: italic;
}

.idx-cols {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.fk-ref {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  font-weight: 500;
}

.fk-cols {
  margin-left: 4px;
  color: var(--text-3);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
}

.rule-tag {
  font-size: var(--fs-xs);
}

.ddl-toolbar {
  display: flex;
  align-items: center;
  gap: var(--tool-gap);
  margin-bottom: var(--tool-gap);
}

.ddl-hint {
  margin-left: auto;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.ddl-text {
  margin: 0;
  padding: var(--grid-pad-inline);
  border: 1px solid var(--grid-line);
  border-radius: var(--tool-radius);
  background: var(--chrome);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
