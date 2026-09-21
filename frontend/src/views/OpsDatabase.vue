<template>
  <div class="ops-page">
    <div class="ops-body">
      <!-- 收起后原位留一条竖条，否则面板消失了没有任何回来的入口。 -->
      <div v-if="!showList" class="rail" @click="showList = true">
        <RightOutlined />
        <span class="rail-text">连接</span>
      </div>

      <template v-else>
      <a-card class="panel list-panel" size="small" :style="{ width: `${listWidth}px` }">
        <template #title>
          <div class="panel-title">
            <span>连接</span>
            <a-space size="small">
              <!-- 台账管理员登记、全员共用：新增/编辑/删除只对管理员可见，
                   后端写端点同样挂着 require_admin。 -->
              <a-button v-if="auth.isAdmin" size="small" type="primary" @click="openCreate">
                <PlusOutlined /> 新增
              </a-button>
              <a-tooltip title="收起列表">
                <a-button size="small" type="text" @click="showList = false">
                  <LeftOutlined />
                </a-button>
              </a-tooltip>
            </a-space>
          </div>
        </template>

        <div class="filters">
          <a-input
            v-model:value="keyword"
            size="small"
            allow-clear
            placeholder="搜索名称或地址"
            @change="onKeywordChange"
          >
            <template #prefix><SearchOutlined /></template>
          </a-input>
          <a-select v-model:value="typeFilter" size="small" :options="TYPE_OPTIONS" @change="load" />
        </div>

        <!-- a-spin 不会把 class 透传到渲染出的 DOM（antdv 4 嵌套模式下用自己的
             class 覆盖传入的），滚动高度链必须套一层自己的 div 来管。 -->
        <div class="tree-wrap">
          <a-spin :spinning="loading">
            <a-empty v-if="!databases.length" :image="simpleEmpty" description="暂无连接" />
            <DbConnectionTree
              v-else
              ref="treeRef"
              :connections="databases"
              @select="onSelectConn"
              @open="openTab"
              @menu="openMenu"
              @close-conn="confirmCloseConn"
              @close-db="confirmCloseDb"
            />
          </a-spin>
        </div>
      </a-card>
      <!-- 藏在面板缝隙里的拖拽条：负边距叠在 gap 上，不改变布局，只在悬停时亮一条线。 -->
      <div class="col-resizer" @mousedown="startListDrag" />
      </template>

      <a-card class="panel main-panel" size="small">
        <a-tabs
          v-model:active-key="activeTabKey"
          class="workbench"
          type="editable-card"
          hide-add
          @edit="onTabEdit"
        >
          <a-tab-pane v-for="tab in tabs" :key="tab.key" :closable="true">
            <template #tab>
              <span
                :class="['tab-label', { tinted: !!tab.conn.color }]"
                :style="tab.conn.color ? { '--tab-tint': tab.conn.color } : undefined"
              >
                <TableOutlined v-if="tab.kind === 'data'" />
                <ProfileOutlined v-else-if="tab.kind === 'structure'" />
                <KeyOutlined v-else-if="tab.kind === 'redis-key'" />
                <CodeOutlined v-else />
                <!-- 文字包一层 span：染色 ::before 是定位元素，裸文本会沉到它下面。 -->
                <a-tooltip :title="tabTooltip(tab)"><span>{{ tab.title }}</span></a-tooltip>
              </span>
            </template>

            <DbDataTab
              v-if="tab.kind === 'data'"
              :conn-id="tab.conn.id"
              :schema="tab.schema!"
              :table="tab.table!"
              :writable="tab.conn.writable"
            />
            <DbStructureTab
              v-else-if="tab.kind === 'structure'"
              :conn-id="tab.conn.id"
              :schema="tab.schema!"
              :table="tab.table!"
            />
            <RedisKeyTab
              v-else-if="tab.kind === 'redis-key'"
              :conn-id="tab.conn.id"
              :db="tab.db ?? 0"
              :rkey="tab.rkey!"
            />
            <DbQueryTab
              v-else
              :ref="(el) => setQueryRef(tab.key, el)"
              :conn-id="tab.conn.id"
              :db-type="tab.conn.db_type"
              :conn-name="tab.conn.name"
              :writable="tab.conn.writable"
              :schema="tab.schema"
              :initial-sql="tab.sql"
              @ask-ai="onAskAi"
            />
          </a-tab-pane>
        </a-tabs>

        <div v-if="!tabs.length" class="workbench-empty">
          <DatabaseOutlined class="empty-icon" />
          <div>双击左侧的表浏览数据，或右键连接「新建查询」</div>
          <div class="empty-hint">默认只读；连接开启「允许写入」后控制台可写，AI 通道始终只读</div>
        </div>
      </a-card>

      <div v-if="!showChat" class="rail" @click="showChat = true">
        <LeftOutlined />
        <span class="rail-text">智能问答</span>
      </div>

      <template v-else>
      <div class="col-resizer" @mousedown="startChatDrag" />
      <a-card class="panel chat-panel" size="small" :style="{ width: `${chatWidth}px` }">
        <template #title>
          <div class="panel-title">
            <span>智能问答</span>
            <a-tooltip title="收起">
              <a-button size="small" type="text" @click="showChat = false">
                <RightOutlined />
              </a-button>
            </a-tooltip>
          </div>
        </template>

<!--        <a-alert-->
<!--          class="chat-note"-->
<!--          type="info"-->
<!--          show-icon-->
<!--          message="AI 只有只读能力，无法执行任何写操作。"-->
<!--        />-->
        <OpsChat
          ref="chatRef"
          target="database"
          :target-id="selectedId"
          placeholder="例如：哪张表最近增长最快"
          empty-hint="先在左侧选中一个连接"
          :samples="chatSamples"
          @open-query="onChatSql"
        />
      </a-card>
      </template>
    </div>

    <!-- 树节点的右键菜单：绝对定位的小菜单，点击任意处关闭。 -->
    <Teleport to="body">
      <div v-if="menu.open" class="menu-mask" @click="closeMenu" @contextmenu.prevent="closeMenu">
        <div class="menu" :style="{ left: `${menu.x}px`, top: `${menu.y}px` }" @click.stop>
          <div
            v-for="item in menuItems"
            :key="item.key"
            :class="['menu-item', { danger: item.danger, disabled: item.disabled }]"
            @click="onMenuClick(item.key)"
          >
            {{ item.label }}
          </div>
        </div>
      </div>
    </Teleport>

    <a-modal
      v-model:open="modal.open"
      :title="modal.editing ? '编辑连接' : '新增连接'"
      :confirm-loading="modal.loading"
      ok-text="保存"
      cancel-text="取消"
      @ok="submit"
    >
      <a-form layout="vertical">
        <a-form-item label="名称" required>
          <a-input v-model:value="modal.form.name" placeholder="例如：订单库（只读从库）" />
        </a-form-item>
        <a-form-item label="颜色">
          <!-- 展示色：树节点整行和页签头一起染色，方便一眼认出生产库。 -->
          <div class="color-row">
            <button
              type="button"
              :class="['swatch', 'swatch-none', { active: !modal.form.color }]"
              title="无"
              @click="modal.form.color = ''"
            />
            <button
              v-for="c in CONN_COLORS"
              :key="c.value"
              type="button"
              :class="['swatch', { active: modal.form.color === c.value }]"
              :style="{ background: c.value }"
              :title="c.label"
              @click="modal.form.color = c.value"
            />
          </div>
        </a-form-item>
        <a-form-item label="类型">
          <a-radio-group v-model:value="modal.form.db_type" button-style="solid" @change="onTypeChange">
            <a-radio-button value="mysql">MySQL / MariaDB</a-radio-button>
            <a-radio-button value="redis">Redis</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <a-form-item label="地址" required>
          <a-input-group compact>
            <a-input v-model:value="modal.form.host" class="host-input" placeholder="10.0.0.1" />
            <a-input-number v-model:value="modal.form.port" class="port-input" :min="1" :max="65535" />
          </a-input-group>
        </a-form-item>
        <a-form-item v-if="modal.form.db_type === 'mysql'" label="用户名" required>
          <a-input v-model:value="modal.form.username" placeholder="readonly" />
        </a-form-item>
        <a-form-item label="密码">
          <a-input-password
            v-model:value="modal.form.password"
            :placeholder="modal.editing ? '留空则不修改已保存的密码' : ''"
            autocomplete="off"
          />
        </a-form-item>
        <a-form-item :label="modal.form.db_type === 'mysql' ? '默认库名' : '库序号'">
          <a-input
            v-model:value="modal.form.db_name"
            :placeholder="modal.form.db_type === 'mysql' ? 'orders' : '0'"
          />
        </a-form-item>
        <a-form-item label="允许写入">
          <a-space>
            <a-switch v-model:checked="modal.form.writable" />
            <span class="writable-hint">
              打开后控制台可执行写操作（{{ modal.form.db_type === 'mysql' ? 'DML/DDL' : 'SET/DEL 等' }}），
              高危命令仍会被拦截；AI 通道始终只读
            </span>
          </a-space>
        </a-form-item>
        <a-form-item label="备注">
          <a-textarea v-model:value="modal.form.remark" :rows="2" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, Modal, message as toast } from 'ant-design-vue'
import {
  CodeOutlined,
  DatabaseOutlined,
  KeyOutlined,
  LeftOutlined,
  PlusOutlined,
  ProfileOutlined,
  RightOutlined,
  SearchOutlined,
  TableOutlined,
} from '@ant-design/icons-vue'
import OpsChat from '@/components/OpsChat.vue'
import DbConnectionTree from '@/components/ops/DbConnectionTree.vue'
import DbDataTab from '@/components/ops/DbDataTab.vue'
import DbQueryTab from '@/components/ops/DbQueryTab.vue'
import DbStructureTab from '@/components/ops/DbStructureTab.vue'
import RedisKeyTab from '@/components/ops/RedisKeyTab.vue'
import { clampSize, readSize, saveSize, startDragResize } from '@/components/ops/resizer'
import '@/components/ops/resizer.css'
import type { TabRequest, TreeMenuEvent, TreeNode } from '@/components/ops/tabs'
import { storageKeys } from '@/utils/storage'
import {
  opsApi,
  type DatabaseType,
  type OpsDatabase,
  type OpsDatabasePayload,
} from '@/api'
import { useAuthStore } from '@/stores/auth'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const router = useRouter()
const auth = useAuthStore()

const TYPE_OPTIONS = [
  { value: '', label: '全部类型' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'redis', label: 'Redis' },
]

const CHAT_SAMPLES = [
  '这个库里最大的几张表是什么',
  '有没有正在跑的慢查询',
  '订单表的结构是怎样的',
]

// 连接展示色板：Navicat 式浅色系（深色文字上读得清，整行铺底也不刺眼）。
// 值就是存库的 #rrggbb，后端只校验格式不做枚举，调色在这里随时可加。
const CONN_COLORS = [
  { value: '#ffc9c9', label: '红' },
  { value: '#ffd9b3', label: '橙' },
  { value: '#fff3ad', label: '黄' },
  { value: '#cdeeb3', label: '绿' },
  { value: '#b7e6dd', label: '青' },
  { value: '#c3d9f7', label: '蓝' },
  { value: '#decffc', label: '紫' },
  { value: '#f7c8e0', label: '粉' },
  { value: '#d9d9d9', label: '灰' },
]

// ---- 连接列表 ----------------------------------------------------------------

const databases = ref<OpsDatabase[]>([])
const loading = ref(false)
const testingId = ref<number | null>(null)
const keyword = ref('')
const typeFilter = ref<'' | DatabaseType>('')
const selectedId = ref<number | null>(null)
const showList = ref(true)
const showChat = ref(false)
const treeRef = ref<InstanceType<typeof DbConnectionTree> | null>(null)

const chatSamples = CHAT_SAMPLES

// ---- 左右面板的拖拽调宽 ---------------------------------------------------------
// 宽度偏好记在 localStorage（纯个人习惯，丢了无妨）；中间工作区始终吃剩余空间。

const LIST_WIDTH_KEY = storageKeys.opsDbListWidth
const CHAT_WIDTH_KEY = storageKeys.opsDbChatWidth

const listWidth = ref(readSize(LIST_WIDTH_KEY, 300))
const chatWidth = ref(readSize(CHAT_WIDTH_KEY, 360))

function startListDrag(event: MouseEvent) {
  const startWidth = listWidth.value
  startDragResize(
    event,
    'x',
    (delta) => { listWidth.value = clampSize(startWidth + delta, 200, 560) },
    () => saveSize(LIST_WIDTH_KEY, listWidth.value),
  )
}

function startChatDrag(event: MouseEvent) {
  const startWidth = chatWidth.value
  startDragResize(
    event,
    'x',
    // 右侧面板：往左拖变宽，位移取反。
    (delta) => { chatWidth.value = clampSize(startWidth - delta, 260, 680) },
    () => saveSize(CHAT_WIDTH_KEY, chatWidth.value),
  )
}

let keywordTimer: ReturnType<typeof setTimeout> | null = null

function onKeywordChange() {
  if (keywordTimer) clearTimeout(keywordTimer)
  keywordTimer = setTimeout(load, 300)
}

async function load() {
  loading.value = true
  try {
    const res = await opsApi.listDatabases({
      keyword: keyword.value.trim() || undefined,
      db_type: typeFilter.value || undefined,
    })
    databases.value = res.items
    if (selectedId.value && !res.items.some((item) => item.id === selectedId.value)) {
      selectedId.value = null
    }
  } finally {
    loading.value = false
  }
}

function onSelectConn(conn: OpsDatabase) {
  selectedId.value = conn.id
}

/** AI 问答里的 SQL：开一个预填好的查询页签，库选择器回落到连接默认库。 */
function onChatSql(sql: string) {
  const conn = databases.value.find((item) => item.id === selectedId.value)
  if (!conn) return
  openTab({ kind: 'query', conn, sql })
}

// ---- 查询页「询问 AI」：把编辑器里的 SQL（和报错）带进展开的问答面板 ------------------

const chatRef = ref<InstanceType<typeof OpsChat> | null>(null)

async function onAskAi(payload: { connId: number; sql: string; error?: string; schema?: string }) {
  // 问答面板目标是当前选中的连接：先切过去，再展开面板（v-else 分支要等一拍挂载）。
  selectedId.value = payload.connId
  showChat.value = true
  await nextTick()
  // 带上连接和默认库上下文，AI 才知道这条 SQL 是跑在哪个库上的。
  const conn = databases.value.find((item) => item.id === payload.connId)
  const where = `连接「${conn?.name ?? payload.connId}」${payload.schema ? ` 的库 \`${payload.schema}\`` : ''}`
  const prompt = payload.error
    ? `在${where}上执行这条 SQL 报错了，帮我分析原因并给出修正：\n\`\`\`sql\n${payload.sql || '（空）'}\n\`\`\`\n错误信息：${payload.error}`
    : `帮我解释这条 SQL（${where}）：\n\`\`\`sql\n${payload.sql}\n\`\`\``
  await chatRef.value?.ask(prompt)
}

// ---- 标签页 -------------------------------------------------------------------

interface Tab {
  key: string
  kind: TabRequest['kind']
  title: string
  conn: OpsDatabase
  schema?: string
  table?: string
  db?: number
  rkey?: string
  sql?: string
}

const tabs = ref<Tab[]>([])
const activeTabKey = ref<string>()

// 切换激活页签时，把右侧 AI 问答的目标连接一起带过去：
// 问 AI 的上下文应该是「正在看的这个页签」，而不是上次在树上点中的连接。
watch(activeTabKey, (key) => {
  const tab = tabs.value.find((item) => item.key === key)
  if (tab) selectedId.value = tab.conn.id
})

/** 每个连接各自的查询序号：查询 1 @ orders、查询 2 @ orders … */
const querySeq = new Map<number, number>()

function tabTooltip(tab: Tab): string {
  if (tab.kind === 'redis-key') return `${tab.conn.name} / db${tab.db ?? 0} / ${tab.rkey}`
  if (tab.kind === 'query') {
    // 标题里已带 @连接名，tooltip 补出默认库就是全路径。
    return tab.schema ? `${tab.title} / ${tab.schema}` : tab.title
  }
  return `${tab.conn.name} / ${tab.schema} / ${tab.table}`
}

function openTab(request: TabRequest) {
  let tab: Tab
  if (request.kind === 'query') {
    const seq = (querySeq.get(request.conn.id) ?? 0) + 1
    querySeq.set(request.conn.id, seq)
    tab = {
      key: `query:${request.conn.id}:${seq}`,
      kind: 'query',
      // Navicat 式标题带上连接名，多连接并排开页签时分得清谁的。
      // 已知边角：连接改名后已开的页签不追新，重开即更新。
      title: `查询 ${seq} @ ${request.conn.name}`,
      conn: request.conn,
      // 从库节点开的查询默认选中那个库；从连接节点开的回落到台账默认库。
      schema: request.schema ?? request.conn.db_name ?? undefined,
      // AI 问答带过来的 SQL 预填进编辑器。
      sql: request.sql,
    }
  } else if (request.kind === 'redis-key') {
    tab = {
      key: `rkey:${request.conn.id}:${request.db}:${request.rkey}`,
      kind: 'redis-key',
      title: `${request.rkey.length > 24 ? `${request.rkey.slice(0, 24)}…` : request.rkey} @ ${request.conn.name}`,
      conn: request.conn,
      db: request.db,
      rkey: request.rkey,
    }
  } else {
    tab = {
      key: `${request.kind}:${request.conn.id}:${request.schema}:${request.table}`,
      kind: request.kind,
      title: request.kind === 'data'
        ? `${request.table} @ ${request.conn.name}`
        : `${request.table} 结构 @ ${request.conn.name}`,
      conn: request.conn,
      schema: request.schema,
      table: request.table,
    }
  }

  const existing = tabs.value.find((item) => item.key === tab.key)
  if (existing) {
    // 重复打开同一张表：激活已有 tab，并刷新连接快照（连接可能刚被编辑过）。
    existing.conn = request.kind === 'query' ? existing.conn : (request as { conn: OpsDatabase }).conn
    activeTabKey.value = existing.key
    return
  }
  tabs.value.push(tab)
  activeTabKey.value = tab.key
}

/** 各查询页签的组件实例：关页签前用 isDirty() 判断编辑器里还有没有内容。 */
const queryTabRefs = new Map<string, InstanceType<typeof DbQueryTab>>()

function setQueryRef(key: string, el: unknown) {
  if (el) queryTabRefs.set(key, el as InstanceType<typeof DbQueryTab>)
  else queryTabRefs.delete(key)
}

function removeTab(tabKey: string) {
  const index = tabs.value.findIndex((tab) => tab.key === tabKey)
  if (index === -1) return
  tabs.value.splice(index, 1)
  if (activeTabKey.value === tabKey) {
    activeTabKey.value = tabs.value[Math.min(index, tabs.value.length - 1)]?.key
  }
}

function onTabEdit(key: unknown, action: string) {
  if (action !== 'remove') return
  const tabKey = String(key)
  const tab = tabs.value.find((item) => item.key === tabKey)
  if (!tab) return
  // 查询页签的编辑器里还有 SQL：关掉就找不回来了，先确认一下。
  if (tab.kind === 'query' && queryTabRefs.get(tabKey)?.isDirty()) {
    Modal.confirm({
      title: '关闭查询页签？',
      content: '编辑器里的 SQL 还没有执行或收藏，关闭后会丢失。',
      okText: '关闭',
      cancelText: '取消',
      onOk: () => removeTab(tabKey),
    })
    return
  }
  removeTab(tabKey)
}

function closeTabsOf(connId: number) {
  tabs.value = tabs.value.filter((tab) => tab.conn.id !== connId)
  if (!tabs.value.some((tab) => tab.key === activeTabKey.value)) {
    activeTabKey.value = tabs.value[0]?.key
  }
}

/**
 * 关闭连接（双击或右键菜单触发）：二次确认后收起树节点，并把它打开的标签页
 * 一并关掉——标签页里都连着这个连接，留着也只会报错。
 */
function confirmCloseConn(node: TreeNode) {
  const conn = node.conn
  if (!conn) return
  const openTabs = tabs.value.filter((tab) => tab.conn.id === conn.id).length
  Modal.confirm({
    title: '关闭连接',
    content: openTabs
      ? `确认关闭「${conn.name}」？它打开的 ${openTabs} 个标签页会一并关闭，查询页里未保存的内容会丢失。`
      : `确认关闭「${conn.name}」？`,
    okText: '关闭',
    cancelText: '取消',
    onOk: () => {
      treeRef.value?.closeConn(node)
      closeTabsOf(conn.id)
    },
  })
}

/** 标签页是否属于某个库节点：MySQL 按 schema 名，Redis 按 db 序号。 */
function tabOfDatabase(tab: Tab, node: TreeNode): boolean {
  if (tab.conn.id !== node.conn?.id) return false
  if (node.kind === 'redisdb') return tab.kind === 'redis-key' && tab.db === (node.db ?? 0)
  return tab.schema !== undefined && tab.schema === node.schema
}

/**
 * 关闭一个库（双击库节点触发，数据库维度）：二次确认后收起节点，并把这个库
 * 打开的标签页（该库的数据 / 结构 / 查询页，或该 db 的 key 详情页）一并关掉。
 * 三角单击收起不受影响，只是视图折叠。
 */
function confirmCloseDb(node: TreeNode) {
  const conn = node.conn
  if (!conn) return
  const label = node.kind === 'redisdb' ? `db${node.db ?? 0}` : node.schema
  const affected = tabs.value.filter((tab) => tabOfDatabase(tab, node))
  Modal.confirm({
    title: '关闭数据库',
    content: affected.length
      ? `确认关闭「${conn.name} / ${label}」？它打开的 ${affected.length} 个标签页会一并关闭，查询页里未保存的内容会丢失。`
      : `确认关闭「${conn.name} / ${label}」？`,
    okText: '关闭',
    cancelText: '取消',
    onOk: () => {
      treeRef.value?.collapse(node)
      tabs.value = tabs.value.filter((tab) => !tabOfDatabase(tab, node))
      if (!tabs.value.some((tab) => tab.key === activeTabKey.value)) {
        activeTabKey.value = tabs.value[0]?.key
      }
    },
  })
}

// ---- 右键菜单 -----------------------------------------------------------------

interface MenuItem {
  key: string
  label: string
  danger?: boolean
  disabled?: boolean
}

const menu = reactive({
  open: false,
  x: 0,
  y: 0,
  node: null as TreeNode | null,
  /** 右键的连接节点当前是否打开（决定菜单显示「打开」还是「关闭」）。 */
  connOpen: false,
})

const menuItems = computed<MenuItem[]>(() => {
  const node = menu.node
  if (!node) return []
  switch (node.kind) {
    case 'conn':
      return [
        { key: 'open-chat', label: '打开智能问答' },
        { key: 'toggle-conn', label: menu.connOpen ? '关闭连接' : '打开连接' },
        { key: 'new-query', label: '新建查询' },
        { key: 'refresh', label: '刷新' },
        { key: 'test', label: testingId.value === node.conn?.id ? '测试中…' : '测试连接', disabled: testingId.value === node.conn?.id },
        ...(auth.isAdmin
          ? [
              { key: 'edit', label: '编辑连接' },
              { key: 'delete', label: '删除连接', danger: true },
            ]
          : []),
      ]
    case 'schema':
      return [
        { key: 'new-query', label: '新建查询' },
        { key: 'refresh', label: '刷新' },
      ]
    case 'table':
      return [
        { key: 'open-data', label: '打开数据' },
        { key: 'open-structure', label: '查看结构' },
      ]
    case 'redisdb':
      return [{ key: 'refresh', label: '刷新' }]
    case 'rkey':
      return [{ key: 'open-key', label: '查看详情' }]
    default:
      return []
  }
})

function openMenu(event: TreeMenuEvent) {
  menu.node = event.node
  menu.connOpen = !!event.open
  menu.open = true
  // 贴着右边缘或下边缘右键时，菜单别探出视口。
  menu.x = Math.min(event.x, window.innerWidth - 140)
  menu.y = Math.min(event.y, window.innerHeight - menuItems.value.length * 34 - 16)
}

function closeMenu() {
  menu.open = false
}

function onMenuClick(key: string) {
  const node = menu.node
  closeMenu()
  if (!node || !node.conn) return
  const conn = node.conn

  switch (key) {
    case 'open-chat': {
      // 全屏智能问答页：查询台 + AI 的独立交互空间，和服务器终端页同一模式。
      // 同一浏览器新标签页打开（bare 路由不带主布局）；不带尺寸参数才是普通
      // 标签页，否则浏览器会开成弹出窗口。
      const { href } = router.resolve({ name: 'OpsDatabaseChat', params: { id: conn.id } })
      window.open(href, '_blank', 'noopener')
      break
    }
    case 'toggle-conn':
      treeRef.value?.toggleConn(node)
      break
    case 'new-query':
      openTab({ kind: 'query', conn, schema: node.schema })
      break
    case 'refresh':
      treeRef.value?.refresh(node)
      break
    case 'test':
      onTest(conn)
      break
    case 'edit':
      openEdit(conn)
      break
    case 'delete':
      confirmDelete(conn)
      break
    case 'open-data':
      openTab({ kind: 'data', conn, schema: node.schema!, table: node.table! })
      break
    case 'open-structure':
      openTab({ kind: 'structure', conn, schema: node.schema!, table: node.table! })
      break
    case 'open-key':
      openTab({ kind: 'redis-key', conn, db: node.db ?? 0, rkey: node.title })
      break
  }
}

// ---- 连接维护 -----------------------------------------------------------------

async function onTest(item: OpsDatabase) {
  testingId.value = item.id
  try {
    const res = await opsApi.testDatabase(item.id)
    if (res.success) toast.success(res.message || '连接正常')
    else toast.error(res.message || '连接失败')
  } catch (err: any) {
    toast.error(err?.message || '测试失败')
  } finally {
    testingId.value = null
    await load()
  }
}

function confirmDelete(item: OpsDatabase) {
  Modal.confirm({
    title: '删除连接',
    content: `确认删除「${item.name}」？连接配置会被移除，它打开的标签页也会一并关闭。`,
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    onOk: () => onDelete(item),
  })
}

async function onDelete(item: OpsDatabase) {
  await opsApi.removeDatabase(item.id)
  closeTabsOf(item.id)
  if (selectedId.value === item.id) selectedId.value = null
  toast.success('已删除')
  await load()
}

const emptyForm = () => ({
  id: 0,
  name: '',
  db_type: 'mysql' as DatabaseType,
  host: '',
  port: 3306,
  username: '',
  password: '',
  db_name: '',
  writable: false,
  /** '' 表示不染色，提交时归一为 null。 */
  color: '',
  remark: '',
})

const modal = reactive({
  open: false,
  editing: false,
  loading: false,
  form: emptyForm(),
})

function onTypeChange() {
  if (modal.editing) return
  modal.form.port = modal.form.db_type === 'mysql' ? 3306 : 6379
}

function openCreate() {
  modal.editing = false
  modal.form = emptyForm()
  modal.open = true
}

function openEdit(item: OpsDatabase) {
  modal.editing = true
  modal.form = {
    ...emptyForm(),
    id: item.id,
    name: item.name,
    db_type: item.db_type,
    host: item.host,
    port: item.port,
    username: item.username || '',
    // 列表接口只回掩码值（sk-****后四位）：原样回填展示，保存时后端见到掩码
    // 会保留已存密码（is_masked 判断），用户输入新值才会覆盖。
    password: item.password || '',
    db_name: item.db_name || '',
    writable: item.writable,
    color: item.color || '',
    remark: item.remark || '',
  }
  modal.open = true
}

async function submit() {
  const form = modal.form
  if (!form.name.trim() || !form.host.trim()) {
    toast.warning('名称和地址不能为空')
    return
  }
  if (form.db_type === 'mysql' && !form.username.trim()) {
    toast.warning('MySQL 需要填写用户名')
    return
  }

  // 密码留空表示保持原样，不能提交空串把已存的密码清掉。
  const payload: OpsDatabasePayload = {
    name: form.name.trim(),
    db_type: form.db_type,
    host: form.host.trim(),
    port: form.port,
    username: form.username.trim() || null,
    db_name: form.db_name.trim() || null,
    writable: form.writable,
    color: form.color || null,
    remark: form.remark.trim() || null,
  }
  if (form.password) payload.password = form.password

  modal.loading = true
  try {
    if (modal.editing) await opsApi.updateDatabase(form.id, payload)
    else await opsApi.createDatabase(payload)
    modal.open = false
    toast.success('已保存')
    await load()
  } finally {
    modal.loading = false
  }
}

onMounted(load)
</script>

<style scoped>
.ops-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.ops-body {
  display: flex;
  gap: 12px;
  flex: 1;
  min-height: 0;
}

.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  /* 与全站 .page-card 同一套语言：发丝线描边 + 卡片级圆角，无投影。 */
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

/* 面板收起后的窄条入口用全局 .rail / .rail-text（style.css）。 */

/* 宽度由 v-bind 的行内样式给（拖拽可调），这里只管不被压缩。 */
.list-panel,
.chat-panel {
  flex-shrink: 0;
}

.main-panel {
  flex: 1;
  min-width: 0;
}

/* 拖拽条：12px 热区用负边距叠在 flex gap 上，视觉上不占位；
   悬停 / 拖拽（body.dragging-col）时中间亮出一条 2px 的线。 */
.col-resizer {
  flex-shrink: 0;
  width: 12px;
  margin: 0 -12px;
  display: flex;
  justify-content: center;
  cursor: col-resize;
  z-index: 5;
}

.col-resizer::after {
  content: '';
  width: 2px;
  border-radius: 1px;
  background: transparent;
  transition: background 0.15s;
}

.col-resizer:hover::after,
body.dragging-col .col-resizer::after {
  background: var(--signal-border, #91caff);
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

.tree-wrap {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

/* 高度链补全：wrapper(flex:1) → spin 嵌套层 → spin 容器都定高，
   滚动由树自身（.conn-tree 的非 scoped 规则）负责。 */
.tree-wrap :deep(.ant-spin-nested-loading),
.tree-wrap :deep(.ant-spin-container) {
  height: 100%;
}

/* 标签页工作台：内容区撑满卡片，切换 tab 不重新挂载（antdv 懒渲染 + 保活）。 */
.workbench {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.workbench :deep(.ant-tabs-content-holder) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.workbench :deep(.ant-tabs-content),
.workbench :deep(.ant-tabs-tabpane) {
  height: 100%;
}

.workbench-empty {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-3);
  font-size: 13px;
  pointer-events: none;
}

.empty-icon {
  font-size: 40px;
  color: rgba(0, 0, 0, 0.15);
}

.empty-hint {
  font-size: 12px;
  color: rgba(0, 0, 0, 0.25);
}

.writable-hint {
  font-size: 12px;
  color: var(--text-3);
}

.main-panel :deep(.ant-card-body) {
  position: relative;
  padding-top: 8px;
}

.tab-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 页签压矮：上下 padding 8px→3px，页签栏与内容区的间距 16px→8px。 */
.main-panel :deep(.ant-tabs-card > .ant-tabs-nav .ant-tabs-tab) {
  padding: 3px 16px;
}

.main-panel :deep(.ant-tabs-top > .ant-tabs-nav) {
  margin-bottom: 8px;
}

/* 页签整格染色：::before 以 .ant-tabs-tab 为定位盒铺满整格（inset:0 不受
   tab 内边距影响），label 里的图标 / 文案和关闭钮提到染色层之上；
   pointer-events 关掉，点关闭钮不会被这层挡住。 */
.main-panel :deep(.ant-tabs-tab) {
  position: relative;
}

.main-panel :deep(.ant-tabs-tab .tab-label.tinted)::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 8px 8px 0 0;
  background: var(--tab-tint);
  pointer-events: none;
}

.main-panel :deep(.ant-tabs-tab .tab-label.tinted) > * {
  position: relative;
}

.main-panel :deep(.ant-tabs-tab .ant-tabs-tab-remove) {
  position: relative;
}

/* 连接展示色板：小方块直选，「无」用白底斜杠表示。 */
.color-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.swatch {
  width: 22px;
  height: 22px;
  padding: 0;
  border: 1px solid rgba(0, 0, 0, 0.15);
  border-radius: 4px;
  cursor: pointer;
}

.swatch.active {
  outline: 2px solid var(--signal-text);
  outline-offset: 1px;
}

.swatch-none {
  position: relative;
  background: #fff;
}

.swatch-none::after {
  content: '';
  position: absolute;
  left: 2px;
  right: 2px;
  top: 50%;
  height: 1px;
  background: #ff4d4f;
  transform: rotate(-45deg);
}

.chat-note {
  margin-bottom: 8px;
}
</style>

<style>
/* 右键菜单挂在 body 下，不能 scoped。 */
.menu-mask {
  position: fixed;
  inset: 0;
  z-index: 1050;
}

.menu {
  position: fixed;
  min-width: 120px;
  padding: 4px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: var(--surface);
  box-shadow: var(--shadow-overlay);
}

.menu-item {
  padding: 6px 12px;
  border-radius: 4px;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.15s;
}

.menu-item:hover {
  background: #f5f5f5;
}

.menu-item.danger {
  color: #ff4d4f;
}

.menu-item.disabled {
  color: rgba(0, 0, 0, 0.25);
  cursor: not-allowed;
}

.menu-item.disabled:hover {
  background: transparent;
}
</style>
