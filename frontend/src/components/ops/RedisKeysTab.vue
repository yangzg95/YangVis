<template>
  <div class="tab-page">
    <div class="toolbar">
      <DatabaseOutlined class="icon" />
      <span class="db-name">db{{ db }}</span>
      <a-input-search
        v-model:value="pattern"
        class="pattern"
        size="small"
        placeholder="key 模式，如 user:*"
        :maxlength="256"
        enter-button
        allow-clear
        @search="applyPattern"
      />
      <span class="spacer" />
      <span v-if="rows.length" class="meta">已加载 {{ rows.length }} 个 · 双击看详情</span>
      <a-tooltip :title="canWrite ? '新建 key' : '当前连接或账号没有写权限'">
        <a-button size="small" type="text" :disabled="!canWrite" @click="openCreate">
          <PlusOutlined />
        </a-button>
      </a-tooltip>
      <a-tooltip :title="canWrite ? '删除选中 key' : '当前连接或账号没有写权限'">
        <a-button
          size="small"
          type="text"
          danger
          :disabled="!canWrite || !selected.length"
          :loading="deleting"
          @click="confirmDeleteSelected"
        >
          <DeleteOutlined />
          <span v-if="selected.length" class="count">{{ selected.length }}</span>
        </a-button>
      </a-tooltip>
      <a-tooltip title="重新扫描">
        <a-button size="small" type="text" :loading="loading" @click="load(true)">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-alert
      v-if="!canWrite"
      class="note"
      type="info"
      show-icon
      message="只读视图：连接未开启「允许写入」，或当前账号没有运维写权限。"
    />

    <div ref="gridWrapRef" class="grid-wrap">
      <a-table
        class="db-grid"
        size="small"
        row-key="key"
        :data-source="rows"
        :columns="columns"
        :pagination="false"
        :loading="loading"
        :scroll="{ x: 'max-content', y: scrollY }"
        :row-selection="canWrite ? rowSelection : undefined"
        :custom-row="customRow"
      >
        <template #bodyCell="{ column, text }">
          <a-tag v-if="column.key === 'key_type'" :color="TYPE_COLORS[text as string] || 'default'">
            {{ text }}
          </a-tag>
          <span v-else>{{ text }}</span>
        </template>
      </a-table>
    </div>

    <div v-if="cursor !== '0'" class="more-bar">
      <a-button size="small" :loading="loading" @click="load(false)">
        <DownOutlined /> 加载更多（SCAN 游标翻页）
      </a-button>
    </div>

    <a-modal
      v-model:open="create.open"
      title="新建 key"
      :confirm-loading="create.loading"
      ok-text="创建"
      cancel-text="取消"
      @ok="submitCreate"
    >
      <a-form layout="vertical" class="create-form">
        <a-form-item label="key" required>
          <a-input v-model:value="create.key" placeholder="如 user:1001:profile" />
        </a-form-item>
        <a-form-item label="类型" required>
          <!-- stream 的条目 id 由服务端生成，手工建没有意义，故不在候选里。 -->
          <a-select v-model:value="create.key_type" :options="CREATE_TYPES" @change="onTypeChange" />
        </a-form-item>

        <a-form-item v-if="create.key_type === 'string'" label="值">
          <a-textarea v-model:value="create.value" :auto-size="{ minRows: 2, maxRows: 8 }" />
        </a-form-item>

        <template v-else-if="create.key_type === 'hash'">
          <a-form-item label="field">
            <a-input v-model:value="create.field" />
          </a-form-item>
          <a-form-item label="value">
            <a-textarea v-model:value="create.value" :auto-size="{ minRows: 2, maxRows: 8 }" />
          </a-form-item>
        </template>

        <a-form-item v-else-if="create.key_type === 'zset'" label="member / score">
          <div class="pair-row">
            <a-input v-model:value="create.value" placeholder="member" />
            <a-input-number
              v-model:value="create.score"
              :controls="false"
              placeholder="score"
              class="score"
            />
          </div>
        </a-form-item>

        <a-form-item v-else :label="create.key_type === 'list' ? '首个元素' : '首个成员'">
          <a-input v-model:value="create.value" />
        </a-form-item>

        <a-form-item label="过期秒数（可选）">
          <a-input-number
            v-model:value="create.ttl"
            :min="1"
            :max="TTL_MAX_SECONDS"
            placeholder="不填即永不过期"
            style="width: 100%"
          />
        </a-form-item>
        <div class="form-hint">
          集合类先给一个元素建出 key，其余元素到 key 详情页继续添加。key 已存在会创建失败。
        </div>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import {
  DatabaseOutlined,
  DeleteOutlined,
  DownOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { Modal, message as toast } from 'ant-design-vue'
import {
  opsApi,
  type RedisKeyCreatePayload,
  type RedisKeyItem,
  type RedisWriteResult,
} from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useGridScrollY } from './useGridScrollY'
import './grid.css'

const props = defineProps<{
  connId: number
  db: number
  /** 连接的「允许写入」开关；与用户 ops_write 通行证一起决定写操作是否可用。 */
  writable: boolean
}>()

const emit = defineEmits<{ (e: 'open-key', key: string): void }>()

const auth = useAuthStore()
const canWrite = computed(() => props.writable && auth.canOpsWrite)

const TYPE_COLORS: Record<string, string> = {
  string: 'blue',
  list: 'green',
  set: 'orange',
  zset: 'purple',
  hash: 'cyan',
  stream: 'magenta',
}

const CREATE_TYPES: { label: string; value: RedisKeyCreatePayload['key_type'] }[] = [
  { label: 'string', value: 'string' },
  { label: 'list', value: 'list' },
  { label: 'set', value: 'set' },
  { label: 'zset', value: 'zset' },
  { label: 'hash', value: 'hash' },
]

const TTL_MAX_SECONDS = 3_153_600_000
// 一页 SCAN 取的 key 数：与后端 MAX_PAGE_SIZE 对齐，再多就是自找的卡顿。
const PAGE_SIZE = 100

const rows = ref<RedisKeyItem[]>([])
const cursor = ref('0')
const pattern = ref('')
const applied = ref('')
const loading = ref(false)
const deleting = ref(false)
const selected = ref<string[]>([])

const gridWrapRef = ref<HTMLElement | null>(null)
const { scrollY, measureScrollY } = useGridScrollY(gridWrapRef, { pagination: false })

const columns = [
  { title: 'key', dataIndex: 'key', key: 'key', ellipsis: true },
  { title: '类型', dataIndex: 'key_type', key: 'key_type', width: 110 },
]

const rowSelection = computed(() => ({
  selectedRowKeys: selected.value,
  onChange: (keys: (string | number)[]) => {
    selected.value = keys.map(String)
  },
}))

function customRow(record: RedisKeyItem) {
  // 双击打开详情：单击留给勾选框，否则想选几个 key 会先弹出一堆页签。
  return {
    onDblclick: () => emit('open-key', record.key),
    style: { cursor: 'pointer' },
  }
}

/** SCAN 不保证不重复，翻页追加时按 key 去重。 */
function mergeInto(existing: RedisKeyItem[], fresh: RedisKeyItem[]): RedisKeyItem[] {
  const seen = new Set(existing.map((item) => item.key))
  return [...existing, ...fresh.filter((item) => !seen.has(item.key))]
}

async function load(reset: boolean) {
  loading.value = true
  try {
    const res = await opsApi.redisKeys(
      props.connId,
      props.db,
      reset ? '0' : cursor.value,
      PAGE_SIZE,
      applied.value || undefined,
    )
    rows.value = reset ? [...res.keys] : mergeInto(rows.value, res.keys)
    cursor.value = res.cursor
    if (reset) selected.value = []
    await nextTick()
    measureScrollY()
  } catch (err: any) {
    toast.error(err?.message || '加载失败')
  } finally {
    loading.value = false
  }
}

function applyPattern() {
  applied.value = pattern.value.trim()
  void load(true)
}

function announce(result: RedisWriteResult, message: string) {
  const cmd = result.command || ''
  toast.success(cmd.length > 90 ? `${message}：${cmd.slice(0, 90)}…` : `${message}：${cmd}`)
}

function confirmDeleteSelected() {
  const keys = [...selected.value]
  if (!keys.length) return
  Modal.confirm({
    title: '删除 key',
    content: `确定删除选中的 ${keys.length} 个 key 吗？删除后不可恢复。前三个：${keys
      .slice(0, 3)
      .join('、')}${keys.length > 3 ? '…' : ''}`,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    async onOk() {
      deleting.value = true
      try {
        const res = await opsApi.redisKeysDelete(props.connId, props.db, keys)
        announce(res, `已删除 ${res.deleted} 个`)
        const gone = new Set(keys)
        rows.value = rows.value.filter((item) => !gone.has(item.key))
        selected.value = []
      } catch (err: any) {
        toast.error(err?.message || '删除失败')
      } finally {
        deleting.value = false
      }
    },
  })
}

// ---- 新建 key ---------------------------------------------------------------------

const create = reactive({
  open: false,
  loading: false,
  key: '',
  key_type: 'string' as RedisKeyCreatePayload['key_type'],
  value: '',
  field: '',
  score: null as number | null,
  ttl: null as number | null,
})

function openCreate() {
  create.open = true
}

function onTypeChange() {
  create.score = null
}

function createInvalidReason(): string {
  if (!create.key.trim()) return '请填写 key'
  if (create.key_type === 'hash' && !create.field) return '请填写 field'
  if (create.key_type === 'zset' && create.score == null) return '请填写 score'
  // string 允许空值（SET key ""）；集合类至少要给一个元素才建得出 key。
  if (create.key_type !== 'string' && !create.value) return '请填写初始值'
  return ''
}

/** 表单 → 后端认的载荷：集合类统一成「一个初始元素」，形态按类型对齐服务层。 */
function createPayload(key: string): RedisKeyCreatePayload {
  const base = { db: props.db, key, ttl: create.ttl }
  switch (create.key_type) {
    case 'hash':
      return { ...base, key_type: 'hash', value: [[create.field, create.value]] }
    case 'zset':
      return { ...base, key_type: 'zset', value: [[create.value, String(create.score)]] }
    case 'list':
    case 'set':
      return { ...base, key_type: create.key_type, value: [create.value] }
    default:
      return { ...base, key_type: 'string', value: create.value }
  }
}

async function submitCreate() {
  const reason = createInvalidReason()
  if (reason) {
    toast.warning(reason)
    return
  }
  const key = create.key.trim()
  create.loading = true
  try {
    const res = await opsApi.redisKeyCreate(props.connId, createPayload(key))
    announce(res, '已创建')
    create.open = false
    create.key = ''
    create.value = ''
    create.field = ''
    create.ttl = null
    // 建完直接跳到详情页继续加元素，省一次「先找到它」的扫描。
    emit('open-key', key)
    // 列表重扫第一页：新建的 key 该看得见，不然用户以为没建成。
    void load(true)
  } catch (err: any) {
    toast.error(err?.message || '创建失败')
  } finally {
    create.loading = false
  }
}

onMounted(() => load(true))
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

.icon {
  color: var(--text-3);
}

.db-name {
  font-family: var(--font-mono);
  font-size: 13px;
}

.pattern {
  width: 240px;
}

.spacer {
  flex: 1;
}

.meta,
.count {
  color: var(--text-3);
  font-size: 12px;
}

.count {
  margin-left: 4px;
}

.note {
  margin-bottom: 8px;
}

.grid-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.more-bar {
  display: flex;
  justify-content: center;
  padding: 6px 0 2px;
}

.create-form :deep(.ant-form-item) {
  margin-bottom: 12px;
}

.pair-row {
  display: flex;
  gap: 8px;
}

.pair-row .score {
  width: 120px;
}

.form-hint {
  color: var(--text-3);
  font-size: 12px;
}
</style>
