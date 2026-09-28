<template>
  <div class="tab-page">
    <div class="toolbar">
      <KeyOutlined class="icon" />
      <a-tooltip :title="detail?.key || rkey">
        <span class="key-name">{{ rkey }}</span>
      </a-tooltip>
      <template v-if="detail">
        <a-tag :color="TYPE_COLORS[detail.key_type] || 'default'">{{ detail.key_type }}</a-tag>
        <span class="ttl">{{ ttlText }}</span>
      </template>
      <span class="spacer" />
      <template v-if="detail && detail.key_type !== 'none'">
        <a-tooltip :title="canEdit ? '设置过期时间' : '当前连接或账号没有写权限'">
          <a-popover
            v-model:open="ttlOpen"
            trigger="click"
            placement="bottomRight"
            :disabled="!canEdit"
          >
            <template #content>
              <div class="ttl-pop">
                <a-input-number
                  v-model:value="ttlSeconds"
                  :min="1"
                  :max="TTL_MAX_SECONDS"
                  placeholder="过期秒数"
                  style="width: 150px"
                />
                <div class="pop-hint">常用：60 秒 / 3600 秒 / 86400 秒</div>
                <div class="pop-actions">
                  <a-button
                    type="primary"
                    size="small"
                    :loading="saving"
                    :disabled="!ttlSeconds"
                    @click="applyExpire"
                  >
                    设置过期
                  </a-button>
                  <a-button size="small" :loading="saving" @click="cancelTtl">取消过期</a-button>
                </div>
              </div>
            </template>
            <a-button size="small" type="text" :disabled="!canEdit">
              <FieldTimeOutlined />
            </a-button>
          </a-popover>
        </a-tooltip>
        <a-tooltip :title="canEdit ? '删除 key' : '当前连接或账号没有写权限'">
          <a-button size="small" type="text" danger :disabled="!canEdit" @click="confirmDeleteKey">
            <DeleteOutlined />
          </a-button>
        </a-tooltip>
      </template>
      <a-tooltip title="刷新">
        <a-button size="small" type="text" :loading="loading" @click="load">
          <ReloadOutlined />
        </a-button>
      </a-tooltip>
    </div>

    <a-alert
      v-if="detail?.truncated"
      class="note"
      type="warning"
      show-icon
      message="集合较大，只显示了前一部分元素。列表按下标删除，同值的其它元素不受影响。"
    />

    <a-spin :spinning="loading" class="body">
      <a-empty v-if="detail?.key_type === 'none'" description="key 不存在或已过期" />

      <template v-else-if="detail">
        <!-- string：原文展示，改值切到文本框 -->
        <template v-if="detail.key_type === 'string'">
          <div v-if="stringEditing" class="string-edit">
            <a-textarea
              v-model:value="stringDraft"
              :auto-size="{ minRows: 4, maxRows: 14 }"
              class="string-input"
            />
            <div class="edit-actions">
              <a-button type="primary" size="small" :loading="saving" @click="saveString">
                保存
              </a-button>
              <a-button size="small" :disabled="saving" @click="stringEditing = false">
                取消
              </a-button>
              <span class="hint">保存只替换值，原有 TTL 保持不变</span>
            </div>
          </div>
          <template v-else>
            <div class="string-wrap"><pre class="string-value">{{ detail.value }}</pre></div>
            <div v-if="canEdit" class="row-actions">
              <a-button size="small" @click="startStringEdit">
                <EditOutlined /> 改值
              </a-button>
            </div>
          </template>
        </template>

        <template v-else>
          <!-- 添加元素：类型决定给哪几个输入框，提交的是后端生成的那条命令 -->
          <div v-if="canEdit && addable" class="add-bar">
            <a-input
              v-if="detail.key_type === 'hash'"
              v-model:value="addForm.field"
              size="small"
              placeholder="field"
              class="add-field"
            />
            <a-input
              v-model:value="addForm.value"
              size="small"
              :placeholder="ADD_VALUE_PLACEHOLDER[detail.key_type] || 'value'"
              class="add-value"
            />
            <a-input-number
              v-if="detail.key_type === 'zset'"
              v-model:value="addForm.score"
              size="small"
              :controls="false"
              placeholder="score"
              class="add-score"
            />
            <a-select
              v-if="detail.key_type === 'list'"
              v-model:value="addForm.position"
              size="small"
              :options="POSITION_OPTIONS"
              class="add-position"
            />
            <a-button size="small" type="primary" :loading="saving" @click="submitAdd">
              <PlusOutlined /> 添加
            </a-button>
          </div>

          <div ref="gridWrapRef" class="grid-wrap">
            <!-- list / set：单列，带序号 -->
            <a-table
              v-if="detail.key_type === 'list' || detail.key_type === 'set'"
              class="db-grid"
              size="small"
              :data-source="listRows"
              :columns="listColumns"
              :pagination="false"
              :scroll="{ x: 'max-content', y: scrollY }"
              row-key="__i"
            >
              <template v-if="withOps" #bodyCell="{ column, record, text }">
                <span v-if="column.key === 'ops'" class="row-del" @click="deleteElement(record)">
                  删除
                </span>
                <span v-else>{{ text }}</span>
              </template>
            </a-table>

            <!-- hash / zset / stream：两列 -->
            <a-table
              v-else
              class="db-grid"
              size="small"
              :data-source="pairRows"
              :columns="pairColumns"
              :pagination="false"
              :scroll="{ x: 'max-content', y: scrollY }"
              row-key="__i"
            >
              <template v-if="withOps" #bodyCell="{ column, record, text }">
                <span v-if="column.key === 'ops'" class="row-del" @click="deleteElement(record)">
                  删除
                </span>
                <span v-else>{{ text }}</span>
              </template>
            </a-table>
          </div>
        </template>
      </template>
    </a-spin>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import {
  DeleteOutlined,
  EditOutlined,
  FieldTimeOutlined,
  KeyOutlined,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { Modal, message as toast } from 'ant-design-vue'
import {
  opsApi,
  type RedisElementAddPayload,
  type RedisKeyType,
  type RedisKeyDetail,
  type RedisWriteResult,
} from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useGridScrollY } from './useGridScrollY'
import './grid.css'

const props = defineProps<{
  connId: number
  db: number
  rkey: string
  /** 连接的「允许写入」开关；与用户 ops_write 通行证一起决定写操作是否可用。 */
  writable: boolean
}>()

const auth = useAuthStore()
// 前台置灰的依据；真正的闸门在后端（writable × can_ops_write 双判定）。
const canEdit = computed(() => props.writable && auth.canOpsWrite)

const TYPE_COLORS: Record<string, string> = {
  string: 'blue',
  list: 'green',
  set: 'orange',
  zset: 'purple',
  hash: 'cyan',
  stream: 'magenta',
}

const PAIR_HEADERS: Record<string, [string, string]> = {
  hash: ['field', 'value'],
  zset: ['member', 'score'],
  stream: ['id', 'fields'],
}

const ADD_VALUE_PLACEHOLDER: Record<string, string> = {
  list: '元素值',
  set: '成员',
  zset: 'member',
  hash: 'value',
}

const POSITION_OPTIONS = [
  { label: '尾部 RPUSH', value: 'tail' },
  { label: '头部 LPUSH', value: 'head' },
]

// 与后端 RedisTtlRequest.seconds 的上限一致（约 100 年）。
const TTL_MAX_SECONDS = 3_153_600_000

/** 能加元素的类型：stream 的 id 由服务端生成，module 类型后端也不认。 */
const ADDABLE_TYPES: RedisKeyType[] = ['list', 'set', 'zset', 'hash']
/** 能删元素的类型：含 stream（XDEL 按 id 删），不含 string（string 走改值）。 */
const DELETABLE_TYPES: RedisKeyType[] = ['list', 'set', 'zset', 'hash', 'stream']

const detail = ref<RedisKeyDetail | null>(null)
const loading = ref(false)
const saving = ref(false)

// scroll.y 实测容器剩余空间（无分页），写死像素会在窗口变矮时顶出容器。
const gridWrapRef = ref<HTMLElement | null>(null)
const { scrollY, measureScrollY } = useGridScrollY(gridWrapRef, { pagination: false })

const ttlText = computed(() => {
  const ttl = detail.value?.ttl
  if (ttl == null || ttl === -1) return '永不过期'
  if (ttl === -2) return ''
  return `TTL ${ttl}s`
})

const keyType = computed(() => (detail.value?.key_type || '') as RedisKeyType)
const addable = computed(() => ADDABLE_TYPES.includes(keyType.value))
const withOps = computed(() => canEdit.value && DELETABLE_TYPES.includes(keyType.value))

const OPS_COLUMN = { title: '', key: 'ops', width: 64, align: 'center' as const }

const listRows = computed(() => {
  const values = (detail.value?.value as string[]) || []
  // 列表按下标删（后端 LSET 哨兵 + LREM 一条），集合按成员值删。
  const byIndex = detail.value?.key_type === 'list'
  return values.map((v, i) => ({
    __i: i,
    index: i,
    value: v,
    __target: byIndex ? String(i) : v,
  }))
})

const listColumns = computed(() => {
  const cols = [
    { title: '#', dataIndex: 'index', key: 'index', width: 60 },
    { title: 'value', dataIndex: 'value', key: 'value', ellipsis: true },
  ]
  return withOps.value ? [...cols, OPS_COLUMN] : cols
})

const pairRows = computed(() => {
  const values = (detail.value?.value as [string, string][]) || []
  return values.map(([a, b], i) => ({ __i: i, a, b, __target: a }))
})

const pairColumns = computed(() => {
  const [ha, hb] = PAIR_HEADERS[detail.value?.key_type || ''] || ['field', 'value']
  const cols = [
    { title: ha, dataIndex: 'a', key: 'a', ellipsis: true },
    { title: hb, dataIndex: 'b', key: 'b', ellipsis: true },
  ]
  return withOps.value ? [...cols, OPS_COLUMN] : cols
})

/**
 * 提示里带上服务端生成的命令：界面上点的是「删 field x」，实际发的是 HDEL，
 * 把命令写出来，用户才知道自己在跟哪种数据类型打交道、也便于对审计。
 */
function announce(result: RedisWriteResult, message: string) {
  const cmd = result.command || ''
  toast.success(cmd.length > 90 ? `${message}：${cmd.slice(0, 90)}…` : `${message}：${cmd}`)
}

async function writeAndReload(run: () => Promise<RedisWriteResult>, message: string) {
  saving.value = true
  try {
    announce(await run(), message)
    await load()
    return true
  } catch (err: any) {
    toast.error(err?.message || '操作失败')
    return false
  } finally {
    saving.value = false
  }
}

// ---- string 改值 ----------------------------------------------------------------

const stringEditing = ref(false)
const stringDraft = ref('')

function startStringEdit() {
  stringDraft.value = String(detail.value?.value ?? '')
  stringEditing.value = true
}

async function saveString() {
  const ok = await writeAndReload(
    () => opsApi.redisStringUpdate(props.connId, props.db, props.rkey, stringDraft.value),
    '已保存',
  )
  // 失败时留在编辑态：框里的草稿是用户敲进去的，收起就等于丢了。
  if (ok) stringEditing.value = false
}

// ---- 元素增删 --------------------------------------------------------------------

const addForm = reactive({
  field: '',
  value: '',
  score: null as number | null,
  position: 'tail' as 'head' | 'tail',
})

function addInvalidReason(type: RedisKeyType): string {
  if (type === 'hash' && !addForm.field) return '请填写 field'
  if (type === 'zset' && addForm.score == null) return '请填写 score'
  if (!addForm.value) return '请填写值'
  return ''
}

function submitAdd() {
  const reason = addInvalidReason(keyType.value)
  if (!addable.value || reason) {
    toast.warning(reason || '该类型不支持添加元素')
    return
  }
  // 上面的 addable 判定已保证是这四种之一，这里只是把它告诉类型系统。
  const type = keyType.value as RedisElementAddPayload['key_type']
  void writeAndReload(
    () =>
      opsApi.redisElementAdd(props.connId, {
        db: props.db,
        key: props.rkey,
        key_type: type,
        value: addForm.value,
        field: addForm.field || null,
        score: addForm.score,
        position: addForm.position,
      }),
    '已添加',
  )
  addForm.field = ''
  addForm.value = ''
  addForm.score = null
}

/** 列表删下标，其余删第一列的值——target 的语义由后端按类型解释。 */
function deleteElement(record: Record<string, unknown>) {
  const type = keyType.value
  const target = String(record.__target ?? '')
  const label =
    type === 'list'
      ? `第 ${target} 个元素`
      : `${PAIR_HEADERS[type]?.[0] || '值'} ${target}`
  Modal.confirm({
    title: '删除元素',
    content: `确定从 ${props.rkey} 删除${label}吗？`,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () =>
      writeAndReload(
        () =>
          opsApi.redisElementDelete(props.connId, {
            db: props.db,
            key: props.rkey,
            key_type: type,
            target,
          }),
        '已删除',
      ),
  })
}

// ---- TTL 与删 key -----------------------------------------------------------------

const ttlOpen = ref(false)
const ttlSeconds = ref<number | null>(null)

function applyExpire() {
  if (!ttlSeconds.value) return
  ttlOpen.value = false
  void writeAndReload(
    () =>
      opsApi.redisTtl(props.connId, {
        db: props.db,
        key: props.rkey,
        action: 'expire',
        seconds: ttlSeconds.value,
      }),
    '已设置过期',
  )
}

function cancelTtl() {
  ttlOpen.value = false
  void writeAndReload(
    () => opsApi.redisTtl(props.connId, { db: props.db, key: props.rkey, action: 'persist' }),
    '已取消过期',
  )
}

function confirmDeleteKey() {
  Modal.confirm({
    title: '删除 key',
    content: `确定删除 ${props.rkey} 吗？删除后不可恢复。`,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: () =>
      writeAndReload(() => opsApi.redisKeysDelete(props.connId, props.db, [props.rkey]), '已删除'),
  })
}

async function load() {
  loading.value = true
  try {
    detail.value = await opsApi.redisKeyDetail(props.connId, props.db, props.rkey)
    // 表格随 detail 渲染出来后才占高度，等量一下再重算表格体高度。
    await nextTick()
    measureScrollY()
  } catch (err: any) {
    toast.error(err?.message || '加载失败')
  } finally {
    loading.value = false
  }
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

/* 纵向节奏、.spacer 走全局 .tab-page 规则（style.css）。 */

.icon {
  color: var(--text-3);
}

.key-name {
  max-width: 380px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 13px;
}

.ttl {
  color: var(--text-3);
  font-size: 12px;
}

.body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

/* 高度一路传到表格容器，useGridScrollY 实测到的才是 .body 的剩余空间，
   而不是随内容膨胀的高度。 */
.body :deep(.ant-spin-container) {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.grid-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* string 的值可能很长，自己滚，不去撑高整个页签。 */
.string-wrap {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.string-value {
  margin: 0;
  padding: var(--grid-pad-inline);
  border-radius: var(--tool-radius);
  background: var(--chrome);
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}

.string-edit {
  display: flex;
  flex-direction: column;
  gap: var(--tool-gap);
}

.string-input {
  font-family: var(--font-mono);
  font-size: 12px;
}

.edit-actions,
.row-actions {
  display: flex;
  align-items: center;
  gap: var(--tool-gap);
  margin-top: var(--tool-gap);
}

.hint {
  color: var(--text-3);
  font-size: 12px;
}

.add-bar {
  display: flex;
  align-items: center;
  gap: var(--tool-gap);
  margin-bottom: var(--tool-gap);
}

.add-field {
  width: 180px;
}

.add-value {
  width: 280px;
}

.add-score {
  width: 110px;
}

.add-position {
  width: 120px;
}

.row-del {
  color: var(--ant-color-error, #ff4d4f);
  cursor: pointer;
}

.ttl-pop {
  display: flex;
  flex-direction: column;
  gap: var(--tool-gap);
  width: 210px;
}

.pop-actions {
  display: flex;
  gap: var(--tool-gap);
}

.pop-hint {
  color: var(--text-3);
  font-size: 12px;
}
</style>
