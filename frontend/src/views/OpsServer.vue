<template>
  <div class="ops-page">
    <div class="stats-row">
      <div
        v-for="card in statCards"
        :key="card.key"
        :class="['stat-card', { active: statusFilter === card.key }]"
        @click="onStatClick(card.key)"
      >
        <div :class="['stat-value', card.key]">{{ card.value }}</div>
        <div class="stat-label">{{ card.label }}</div>
      </div>
    </div>

    <div class="ops-body">
      <a-card class="panel list-panel" size="small">
        <template #title>
          <div class="panel-title">
            <span>服务器列表</span>
            <!-- 台账管理员登记、全员共用：新增/编辑/删除只对管理员可见，
                 后端六个写端点同样挂着 require_admin。 -->
            <a-button v-if="auth.isAdmin" size="small" type="primary" @click="openCreate">
              <PlusOutlined /> 新增
            </a-button>
          </div>
        </template>

        <div class="filters">
          <a-input
            v-model:value="keyword"
            size="small"
            allow-clear
            placeholder="搜索名称或 IP"
            @change="onKeywordChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input>
          <a-select v-model:value="statusFilter" size="small" :options="STATUS_OPTIONS" />
        </div>

        <div class="list-wrap">
          <div class="server-list">
            <!-- 首屏骨架：先闪 empty 再填充的跳变比等待本身更显眼。 -->
            <template v-if="loading && !servers.length">
              <div v-for="i in 3" :key="i" class="server-card">
                <a-skeleton active :title="{ width: '50%' }" :paragraph="{ rows: 2 }" />
              </div>
            </template>
            <template v-else>
              <a-empty v-if="!filteredServers.length" :image="simpleEmpty" description="暂无服务器" />
              <div
                v-for="item in filteredServers"
                :key="item.id"
                class="server-card"
                @dblclick="connect(item)"
              >
                <div class="card-head">
                  <a-tooltip :title="statusTitle(item)">
                    <span :class="['status-dot', statusOf(item)]" />
                  </a-tooltip>
                  <span class="server-name">{{ item.name }}</span>
                  <a-tag :color="STATUS_TAG[statusOf(item)].color">
                    {{ STATUS_TAG[statusOf(item)].text }}
                  </a-tag>
                </div>

                <a-tooltip :title="addressOf(item)">
                  <div class="server-addr">{{ addressOf(item) }}</div>
                </a-tooltip>

                <div class="server-meta">
                  {{ item.auth_type === 'key' ? '私钥认证' : '密码认证' }} ·
                  <template v-if="item.last_checked_at">
                    检测于 {{ new Date(item.last_checked_at).toLocaleString() }}
                  </template>
                  <template v-else>未检测过</template>
                </div>

                <div v-if="item.credential_error" class="server-error">凭据无法解密</div>
                <div v-if="item.remark" class="server-remark">{{ item.remark }}</div>

                <div class="card-foot" @click.stop @dblclick.stop>
                  <a-button size="small" type="primary" @click="connect(item)">连接</a-button>
                  <a-dropdown :trigger="['click']">
                    <a-button size="small" type="text"><MoreOutlined /></a-button>
                    <template #overlay>
                      <a-menu>
                        <a-menu-item
                          key="test"
                          :disabled="testingId === item.id"
                          @click="onTest(item)"
                        >
                          {{ testingId === item.id ? '测试中…' : '测试连接' }}
                        </a-menu-item>
                        <a-menu-item v-if="auth.isAdmin" key="edit" @click="openEdit(item)">
                          编辑
                        </a-menu-item>
                        <a-menu-item
                          v-if="auth.isAdmin"
                          key="delete"
                          danger
                          @click="confirmDelete(item)"
                        >
                          删除
                        </a-menu-item>
                      </a-menu>
                    </template>
                  </a-dropdown>
                </div>
              </div>
            </template>
          </div>
        </div>
      </a-card>

      <a-card class="panel audit-panel" size="small">
        <template #title>
          <div class="panel-title">
            <span>最近操作记录</span>
            <a-tooltip title="刷新">
              <a-button size="small" type="text" @click="loadAudit">
                <ReloadOutlined />
              </a-button>
            </a-tooltip>
          </div>
        </template>

        <div class="list-wrap">
          <div class="audit-list">
            <template v-if="auditLoading && !audits.length">
              <div v-for="i in 4" :key="i" class="audit-item">
                <a-skeleton
                  active
                  :avatar="{ size: 'small', shape: 'circle' }"
                  :title="{ width: '65%' }"
                  :paragraph="{ rows: 1 }"
                />
              </div>
            </template>
            <template v-else>
              <a-empty v-if="!audits.length" :image="simpleEmpty" description="暂无操作记录" />
              <div v-for="item in audits" :key="item.id" class="audit-item">
                <!-- 手敲命令（manual）拿不到退出码，用中性点，不按 success 染色。 -->
                <span
                  :class="[
                    'audit-dot',
                    item.verdict === 'manual' ? 'manual' : item.success ? 'ok' : 'fail',
                  ]"
                />
                <div class="audit-main">
                  <a-tooltip :title="item.command">
                    <div class="audit-command">{{ item.command }}</div>
                  </a-tooltip>
                  <div class="audit-meta">
                    {{ actorLabel(item.actor) }} · {{ item.target_name || `#${item.target_id}` }} ·
                    {{ new Date(item.created_at).toLocaleString() }}
                  </div>
                  <div v-if="item.error" class="audit-error">{{ item.error }}</div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </a-card>
    </div>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑服务器' : '新增服务器'"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submit"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="modal.form.name" placeholder="例如：生产 web-01" />
        </a-form-item>
        <a-form-item label="地址" required>
          <a-input-group compact>
            <a-input v-model:value="modal.form.host" class="host-input" placeholder="10.0.0.1" />
            <a-input-number v-model:value="modal.form.port" class="port-input" :min="1" :max="65535" />
          </a-input-group>
        </a-form-item>
        <a-form-item label="登录用户" required>
          <a-input v-model:value="modal.form.username" placeholder="root" />
        </a-form-item>
        <a-form-item label="认证方式">
          <a-radio-group v-model:value="modal.form.auth_type" button-style="solid">
            <a-radio-button value="password">密码</a-radio-button>
            <a-radio-button value="key">私钥</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <a-form-item v-if="modal.form.auth_type === 'password'" label="密码">
          <a-input-password
            v-model:value="modal.form.password"
            :placeholder="modal.editing ? '留空则不修改已保存的密码' : ''"
            autocomplete="off"
          />
        </a-form-item>
        <template v-else>
          <a-form-item label="私钥">
            <a-textarea
              v-model:value="modal.form.private_key"
              :rows="4"
              :placeholder="modal.editing ? '留空则不修改已保存的私钥' : '-----BEGIN OPENSSH PRIVATE KEY-----'"
            />
          </a-form-item>
          <a-form-item label="私钥口令">
            <a-input-password
              v-model:value="modal.form.passphrase"
              placeholder="没有就留空"
              autocomplete="off"
            />
          </a-form-item>
        </template>
        <a-form-item label="备注">
          <a-textarea v-model:value="modal.form.remark" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, Modal, message as toast } from 'ant-design-vue'
import {
  MoreOutlined,
  PlusOutlined,
  ReloadOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import {
  opsApi,
  type OpsAuditItem,
  type OpsServer,
  type OpsServerPayload,
  type ServerAuthType,
} from '@/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'ok', label: '可用' },
  { value: 'error', label: '异常' },
  { value: 'unknown', label: '未测试' },
]

const STATUS_TAG: Record<string, { text: string; color: string }> = {
  ok: { text: '可用', color: 'success' },
  error: { text: '异常', color: 'error' },
  unknown: { text: '未测试', color: 'default' },
}

const router = useRouter()

const servers = ref<OpsServer[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const keyword = ref('')
const statusFilter = ref('all')

const audits = ref<OpsAuditItem[]>([])
const auditLoading = ref(false)

// 关键字交给后端过滤（能命中备注等未展示字段），状态在前端就地过滤。
const filteredServers = computed(() =>
  servers.value.filter((item) => {
    switch (statusFilter.value) {
      case 'ok':
        return item.last_check_ok
      case 'error':
        return !item.last_check_ok && Boolean(item.last_check_error)
      case 'unknown':
        return !item.last_check_ok && !item.last_check_error
      default:
        return true
    }
  }),
)

// 统计口径与上面的筛选保持一致：可用=上次测试通过，异常=有失败记录，其余算未测试。
const stats = computed(() => {
  const all = servers.value
  const ok = all.filter((item) => item.last_check_ok).length
  const error = all.filter((item) => !item.last_check_ok && Boolean(item.last_check_error)).length
  return { total: all.length, ok, error, unknown: all.length - ok - error }
})

const statCards = computed(() => [
  { key: 'all', label: '服务器总数', value: stats.value.total },
  { key: 'ok', label: '可用', value: stats.value.ok },
  { key: 'error', label: '异常', value: stats.value.error },
  { key: 'unknown', label: '未测试', value: stats.value.unknown },
])

/** 点卡片等价于切状态筛选，再点一次当前卡片复位到全部。 */
function onStatClick(key: string) {
  statusFilter.value = statusFilter.value === key ? 'all' : key
}

function addressOf(item: OpsServer): string {
  return `${item.username}@${item.host}:${item.port}`
}

function statusOf(item: OpsServer): 'ok' | 'error' | 'unknown' {
  if (item.last_check_ok) return 'ok'
  return item.last_check_error ? 'error' : 'unknown'
}

function statusTitle(item: OpsServer): string {
  switch (statusOf(item)) {
    case 'ok':
      return '上次测试正常'
    case 'error':
      return item.last_check_error || '上次测试失败'
    default:
      return '尚未测试过连接'
  }
}

function actorLabel(actor: string): string {
  return actor === 'user' ? '手动' : 'AI'
}

function connect(item: OpsServer) {
  router.push({ name: 'OpsServerTerminal', params: { id: item.id } })
}

function confirmDelete(item: OpsServer) {
  Modal.confirm({
    title: '删除服务器',
    content: `确认删除「${item.name}」？连接配置和凭据都会被移除。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: () => onDelete(item),
  })
}

let keywordTimer: ReturnType<typeof setTimeout> | null = null

function onKeywordChange() {
  if (keywordTimer) clearTimeout(keywordTimer)
  keywordTimer = setTimeout(load, 300)
}

async function load() {
  loading.value = true
  try {
    const res = await opsApi.listServers(keyword.value.trim() || undefined)
    servers.value = res.items
  } finally {
    loading.value = false
  }
}

async function loadAudit() {
  auditLoading.value = true
  try {
    const res = await opsApi.audit({ target_type: 'server', limit: 10 })
    audits.value = res.items
  } finally {
    auditLoading.value = false
  }
}

async function onTest(item: OpsServer) {
  testingId.value = item.id
  try {
    const res = await opsApi.testServer(item.id)
    if (res.success) toast.success(res.message || '连接正常')
    else toast.error(res.message || '连接失败')
  } catch (err: any) {
    toast.error(err?.message || '测试失败')
  } finally {
    testingId.value = null
    await load()
  }
}

async function onDelete(item: OpsServer) {
  await opsApi.removeServer(item.id)
  toast.success('已删除')
  await load()
}

const emptyForm = () => ({
  id: 0,
  name: '',
  host: '',
  port: 22,
  username: 'root',
  auth_type: 'password' as ServerAuthType,
  password: '',
  private_key: '',
  passphrase: '',
  remark: '',
})

const modal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: emptyForm(),
})

function openCreate() {
  modal.editing = false
  modal.form = emptyForm()
  modal.open = true
}

function openEdit(item: OpsServer) {
  modal.editing = true
  modal.form = {
    ...emptyForm(),
    id: item.id,
    name: item.name,
    host: item.host,
    port: item.port,
    username: item.username,
    auth_type: item.auth_type,
    remark: item.remark || '',
  }
  modal.open = true
}

async function submit() {
  const form = modal.form
  if (!form.name.trim() || !form.host.trim() || !form.username.trim()) {
    toast.warning('名称、地址、登录用户都不能为空')
    return
  }

  // 留空表示保持原样，所以不能把空串提交上去——那会被后端当作「清空凭据」。
  const payload: OpsServerPayload = {
    name: form.name.trim(),
    host: form.host.trim(),
    port: form.port,
    username: form.username.trim(),
    auth_type: form.auth_type,
    remark: form.remark.trim() || null,
  }
  if (form.auth_type === 'password') {
    if (form.password) payload.password = form.password
  } else {
    if (form.private_key.trim()) payload.private_key = form.private_key.trim()
    if (form.passphrase) payload.passphrase = form.passphrase
  }
  if (!modal.editing && !payload.password && !payload.private_key) {
    toast.warning(form.auth_type === 'password' ? '请填写密码' : '请填写私钥')
    return
  }

  modal.loading = true
  try {
    if (modal.editing) await opsApi.updateServer(form.id, payload)
    else await opsApi.createServer(payload)
    modal.open = false
    toast.success('已保存')
    await load()
  } finally {
    modal.loading = false
  }
}

onMounted(() => {
  load()
  loadAudit()
})
</script>

<style scoped>
.ops-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
}

.ops-body {
  display: flex;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

/* ---- 统计卡片 ------------------------------------------------------------ */

.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  flex-shrink: 0;
}

/* 和站点里的 .page-card 同一套语言：发丝线描边 + 卡片级圆角，无投影。 */
.stat-card {
  padding: 14px 16px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  cursor: pointer;
  transition: border-color 0.15s;
}

.stat-card:hover {
  border-color: var(--signal-border);
}

/* 点中的卡片就是当前筛选条件，给个描边让人看出联动关系。 */
.stat-card.active {
  border-color: var(--signal-border);
  background: var(--signal-bg);
}

.stat-value {
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

.stat-value.ok {
  color: #52c41a;
}

.stat-value.error {
  color: #ff4d4f;
}

.stat-value.unknown {
  color: var(--text-3);
}

.stat-label {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 12px;
}

/* ---- 面板 ---------------------------------------------------------------- */

.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  overflow: hidden;
}

.panel :deep(.ant-card-body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.list-panel {
  flex: 1;
  min-width: 0;
}

.audit-panel {
  width: 340px;
  flex-shrink: 0;
}

.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.filters {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.filters :deep(.ant-select) {
  width: 110px;
  flex-shrink: 0;
}

.list-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 窄屏上下堆叠：面板各自定高、内部滚动，整页不再依赖左右并排。 */
@media (max-width: 1100px) {
  .stats-row {
    grid-template-columns: repeat(2, 1fr);
  }
  .ops-body {
    flex-direction: column;
    overflow-y: auto;
  }
  .list-panel {
    flex: none;
    height: 480px;
  }
  .audit-panel {
    flex: none;
    width: auto;
    height: 320px;
  }
}

/* 服务器是卡片不是行：机器少的时候（比如就两三台），单行列会摊出一大块空白，
   信息卡片自带边框和内容层次，稀疏时也撑得住。用 auto-fit + 1fr 而不是
   auto-fill + 固定上限：空轨道塌缩，卡片均分整行宽度，几台就撑满几列，
   不会在右侧留一截用不上的空白。 */
.server-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 12px;
  align-content: start;
  height: 100%;
  padding: 2px;
  overflow-y: auto;
  overflow-x: hidden;
}

.server-list :deep(.ant-empty) {
  grid-column: 1 / -1;
  margin-top: 48px;
}

.server-card {
  display: flex;
  flex-direction: column;
  /* auto-fit + 1fr 会把唯一一张卡拉成整行宽，封顶 640px 避免单台机器时太扁长。 */
  max-width: 640px;
  padding: 12px 14px 10px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-lg);
  background: var(--surface);
  transition: border-color 0.15s;
}

.server-card:hover {
  border-color: var(--signal-border);
}

.card-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 50%;
  background: #d9d9d9;
}

.status-dot.ok {
  background: #52c41a;
}

.status-dot.error {
  background: #ff4d4f;
}

.server-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  font-weight: 500;
}

/* 状态标签不让步，被截断的应该是长名字。 */
.card-head :deep(.ant-tag) {
  flex-shrink: 0;
  margin-inline-end: 0;
}

/* 主机名可能很长（比如云厂商那种一长串域名），截断并交给 tooltip，
   否则卡片会被撑出横向滚动条。 */
.server-addr {
  margin-top: 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: rgba(0, 0, 0, 0.65);
  font-family: var(--font-mono);
  font-size: 12px;
}

.server-meta {
  margin-top: 4px;
  color: var(--text-3);
  font-size: 12px;
}

.server-error {
  margin-top: 4px;
  color: #cf1322;
  font-size: 12px;
}

/* 备注两行封顶，多了截断——卡片高度不应该被一条长备注带跑偏。 */
.server-remark {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-3);
  font-size: 12px;
}

/* 操作常驻底部右对齐：卡片本来就是独立的操作单元，没必要藏进 hover。
   margin-top:auto 让同行里高矮不一的卡片脚注依然底边对齐。 */
.card-foot {
  display: flex;
  justify-content: flex-end;
  gap: 4px;
  margin-top: auto;
  padding-top: 10px;
  border-top: 1px solid var(--hairline);
}

/* ---- 审计 ---------------------------------------------------------------- */

.audit-list {
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
}

.audit-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--hairline);
}

.audit-item:last-child {
  border-bottom: none;
}

.audit-dot {
  width: 8px;
  height: 8px;
  margin-top: 5px;
  flex-shrink: 0;
  border-radius: 50%;
}

.audit-dot.ok {
  background: #52c41a;
}

.audit-dot.fail {
  background: #ff4d4f;
}

.audit-dot.manual {
  background: #8b98a5;
}

.audit-main {
  flex: 1;
  min-width: 0;
}

.audit-command {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12px;
}

.audit-meta {
  margin-top: 2px;
  color: var(--text-3);
  font-size: 11px;
}

.audit-error {
  margin-top: 2px;
  color: #cf1322;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
