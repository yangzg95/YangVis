<template>
  <div class="ops-page">
    <!-- 手机端换成独立只读浏览页：桌面工作台的树、SQL 编辑器、宽数据网格都依赖
         鼠标交互，不复用；只沿用 opsApi 的数据契约。 -->
    <DbMobileBrowser v-if="isMobile" />
    <div v-else class="ops-body">
      <!-- 收起后原位留一条竖条，否则面板消失了没有任何回来的入口。 -->
      <div v-if="!showList" class="rail" @click="showList = true">
        <RightOutlined />
        <span class="rail-text">连接</span>
      </div>

      <template v-else>
      <a-card class="panel card-fill list-panel" size="small" :style="{ width: `${listWidth}px` }">
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

      <a-card class="panel card-fill main-panel" size="small">
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
                <FormOutlined v-else-if="tab.kind === 'design'" />
                <ApartmentOutlined v-else-if="tab.kind === 'erd'" />
                <KeyOutlined v-else-if="tab.kind === 'redis-key'" />
                <HddOutlined v-else-if="tab.kind === 'redis-keys'" />
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
            <DbDesignTab
              v-else-if="tab.kind === 'design'"
              :conn-id="tab.conn.id"
              :schema="tab.schema!"
              :table="tab.table!"
              :writable="tab.conn.writable"
            />
            <DbErdTab
              v-else-if="tab.kind === 'erd'"
              :conn-id="tab.conn.id"
              :schema="tab.schema!"
              @open-table="(name) => openTab({ kind: 'data', conn: tab.conn, schema: tab.schema!, table: name })"
            />
            <RedisKeysTab
              v-else-if="tab.kind === 'redis-keys'"
              :conn-id="tab.conn.id"
              :db="tab.db ?? 0"
              :writable="tab.conn.writable"
              @open-key="(key: string) => openTab({ kind: 'redis-key', conn: tab.conn, db: tab.db ?? 0, rkey: key })"
            />
            <RedisKeyTab
              v-else-if="tab.kind === 'redis-key'"
              :conn-id="tab.conn.id"
              :db="tab.db ?? 0"
              :rkey="tab.rkey!"
              :writable="tab.conn.writable"
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
              :session-key="tab.key"
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
      <a-card class="panel card-fill chat-panel" size="small" :style="{ width: `${chatWidth}px` }">
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
          :dark="themeStore.isDark"
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

    <!-- 一键备份：整库或选中的几张表，DDL 与 INSERT 汇成一个 .sql。 -->
    <a-modal v-model:open="backup.open" :title="`备份库 ${backup.schema}`" :footer="null" :width="520">
      <a-form layout="vertical">
        <a-form-item label="备份内容">
          <a-radio-group v-model:value="backup.structureOnly" button-style="solid">
            <a-radio-button :value="false">结构 + 数据</a-radio-button>
            <a-radio-button :value="true">仅结构</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <a-form-item>
          <template #label>
            <span>备份范围</span>
            <span class="backup-scope">
              {{
                backup.selected.length
                  ? `已选 ${backup.selected.length} / ${backup.tables.length} 张`
                  : `整库 ${backup.tables.length} 张`
              }}
            </span>
          </template>
          <a-select
            v-model:value="backup.selected"
            mode="multiple"
            :options="backupOptions"
            :loading="backup.tablesLoading"
            :max-tag-count="2"
            :disabled="backup.running"
            show-search
            placeholder="不选即整库"
            class="backup-tables"
          />
          <a-space size="small">
            <a-button
              size="small"
              type="link"
              :disabled="!backup.tables.length || backup.running"
              @click="backup.selected = backup.tables.map((t) => t.name)"
            >
              全选
            </a-button>
            <a-button
              size="small"
              type="link"
              :disabled="!backup.selected.length || backup.running"
              @click="backup.selected = []"
            >
              清空
            </a-button>
          </a-space>
        </a-form-item>
      </a-form>
      <p v-if="backup.tablesError" class="backup-hint">表清单没取到，这次会按整库备份。</p>
      <p class="backup-hint">
        每张表先给建表语句，再按批给 INSERT；视图只留定义。备份文件不含 DROP TABLE，
        还原到已有同名表的库需要先把表清掉。
      </p>
      <a-space>
        <a-button type="primary" :loading="backup.running" @click="runBackup('netdisk')">
          保存到网盘
        </a-button>
        <a-button :disabled="backup.running" @click="runBackup('download')">下载到本地</a-button>
      </a-space>
    </a-modal>

    <!-- 还原备份 / 导入 SQL：一份 .sql 逐句跑在一条可写会话上，进度一帧一帧回来。 -->
    <a-modal
      v-model:open="restore.open"
      :title="`还原到 ${restore.connName} · ${restore.schema}`"
      :footer="null"
      :width="560"
      :mask-closable="false"
    >
      <a-form layout="vertical">
        <a-form-item label="SQL 文件">
          <a-space>
            <a-button :disabled="restore.running" @click="restoreInput?.click()">选择文件</a-button>
            <span v-if="restore.file" class="restore-file">
              {{ restore.file.name }}（{{ fmtSize(restore.file.size) }}）
            </span>
            <span v-else class="restore-file">未选择</span>
          </a-space>
          <input
            ref="restoreInput"
            type="file"
            accept=".sql,text/plain"
            class="restore-file-input"
            @change="onPickRestoreFile"
          />
        </a-form-item>
        <a-form-item>
          <a-checkbox v-model:checked="restore.stopOnError" :disabled="restore.running">
            撞到第一个错误就停（默认逐句继续，出错的那句单独记）
          </a-checkbox>
        </a-form-item>
      </a-form>

      <p class="restore-hint">
        逐句独立执行、成功的各自已落盘：中途停止或断开都不回滚，写进去的留在库里。
        备份文件不含 DROP TABLE，同名表已存在时那句建表会失败而不是覆盖；
        文件里的 USE 会把会话切到它点名的库，这里选的目标库只是起点。
      </p>
      <p v-if="!restore.allowed" class="restore-hint restore-warn">
        这条连接没开「允许写入」，或当前账号没有运维写权限——还原会被直接拒掉。
      </p>

      <template v-if="restore.total">
        <a-progress :percent="restorePercent" :status="restoreStatus" />
        <p class="restore-counts">
          成功 {{ restore.succeeded }} · 失败 {{ restore.failed }} · 跳过 {{ restore.skipped }}
          <template v-if="restore.notRun"> · 未执行 {{ restore.notRun }}</template>
          / 共 {{ restore.total }} 句
          <template v-if="restore.elapsedMs"> · 用时 {{ fmtElapsed(restore.elapsedMs) }}</template>
        </p>
        <p v-if="restore.uses.length" class="restore-hint restore-warn">
          这份脚本会切库到：{{ restore.uses.join('、') }}。
        </p>
        <p v-if="restore.stmtTimeout" class="restore-hint">
          单句超时放宽到 {{ restore.stmtTimeout }} 秒，几十万行的多行 INSERT 才跑得完。
        </p>
        <div v-if="restore.errors.length" class="restore-errors">
          <div v-for="err in restore.errors" :key="err.index" class="restore-error">
            <span class="restore-error-where">第 {{ err.index }} 句</span>
            <span class="restore-error-msg">{{ err.message }}</span>
            <div class="restore-error-sql mono">{{ err.sql }}</div>
          </div>
        </div>
      </template>
      <p v-if="restore.note" class="restore-hint restore-warn">{{ restore.note }}</p>

      <a-space>
        <a-button
          type="primary"
          :loading="restore.running"
          :disabled="!restore.file || !restore.allowed"
          @click="runRestore"
        >
          {{ restore.finished ? '再还原一次' : '开始还原' }}
        </a-button>
        <a-button v-if="restore.running" @click="stopRestore">停止</a-button>
        <a-button :disabled="restore.running" @click="restore.open = false">关闭</a-button>
      </a-space>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Empty, Modal, message as toast } from 'ant-design-vue'
import {
  ApartmentOutlined,
  CodeOutlined,
  DatabaseOutlined,
  FormOutlined,
  HddOutlined,
  KeyOutlined,
  LeftOutlined,
  PlusOutlined,
  ProfileOutlined,
  RightOutlined,
  SearchOutlined,
  TableOutlined,
} from '@ant-design/icons-vue'
import OpsChat from '@/components/OpsChat.vue'
import DbMobileBrowser from '@/components/ops/DbMobileBrowser.vue'
import DbConnectionTree from '@/components/ops/DbConnectionTree.vue'
import DbDataTab from '@/components/ops/DbDataTab.vue'
import DbDesignTab from '@/components/ops/DbDesignTab.vue'
import DbErdTab from '@/components/ops/DbErdTab.vue'
import DbQueryTab from '@/components/ops/DbQueryTab.vue'
import DbStructureTab from '@/components/ops/DbStructureTab.vue'
import RedisKeyTab from '@/components/ops/RedisKeyTab.vue'
import RedisKeysTab from '@/components/ops/RedisKeysTab.vue'
import { clampSize, readSize, saveSize, startDragResize } from '@/components/ops/resizer'
import '@/components/ops/resizer.css'
import type { TabRequest, TreeMenuEvent, TreeNode } from '@/components/ops/tabs'
import {
  dropWorkbenchSql,
  loadWorkbenchState,
  restoredQuerySeq,
  saveWorkbenchState,
} from '@/components/ops/workbenchSession'
import { saveBlobResponse } from '@/utils/download'
import { fmtElapsed, fmtSize } from '@/utils/format'
import { storageKeys } from '@/utils/storage'
import { useIsMobile } from '@/utils/useIsMobile'
import {
  opsApi,
  type DatabaseType,
  type DbTableItem,
  type OpsDatabase,
  type OpsDatabasePayload,
} from '@/api'
import { streamRestore, type RestoreErrorItem } from '@/api/restore'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'

const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

const router = useRouter()
const auth = useAuthStore()
const themeStore = useThemeStore()

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
    // 连接列表到手，才有得认上次留下的页签（只认这一次）。
    restoreSession()
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

// 页签列表 + 激活项一变就落 sessionStorage：刷新、误关浏览器标签页回来，
// 工作台还是原来那一摊。编辑器草稿由 DbQueryTab 自己按页签 key 记。
watch(
  [tabs, activeTabKey],
  () => {
    saveWorkbenchState({
      tabs: tabs.value.map((tab) => ({
        key: tab.key,
        kind: tab.kind,
        title: tab.title,
        connId: tab.conn.id,
        schema: tab.schema,
        table: tab.table,
        db: tab.db,
        rkey: tab.rkey,
      })),
      activeKey: activeTabKey.value,
    })
  },
  { deep: true },
)

/** 恢复只做一次：首次 load 之后跑，之后页签怎么变都以现场里的为准。 */
let sessionRestored = false

/**
 * 把上次的页签摊回来。连接按 id 现认——认不到就是这一轮台账里已经没有它了，
 * 那张页签没有落点，丢掉；其余照原顺序、原激活项恢复。
 */
function restoreSession(): void {
  if (sessionRestored) return
  sessionRestored = true
  const state = loadWorkbenchState()
  if (!state) return
  const restored: Tab[] = []
  for (const item of state.tabs) {
    const conn = databases.value.find((row) => row.id === item.connId)
    if (!conn || restored.some((tab) => tab.key === item.key)) continue
    restored.push({
      key: item.key,
      kind: item.kind,
      title: item.title,
      conn,
      schema: item.schema,
      table: item.table,
      db: item.db,
      rkey: item.rkey,
    })
  }
  if (!restored.length) {
    // 页签全都找不到落点了（连接被删）：现场清掉，别留着下次再认一遍。
    saveWorkbenchState({ tabs: [] })
    return
  }
  tabs.value = restored
  // 查询序号接在恢复出来的最大号后面，否则新建查询会撞已恢复页签的 key。
  for (const [connId, seq] of Object.entries(restoredQuerySeq(state.tabs))) {
    querySeq.set(Number(connId), seq)
  }
  activeTabKey.value = restored.some((tab) => tab.key === state.activeKey)
    ? state.activeKey
    : restored[0].key
}

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
  if (tab.kind === 'redis-keys') return `${tab.conn.name} / db${tab.db ?? 0} 的 key 列表`
  if (tab.kind === 'erd') return `${tab.conn.name} / ${tab.schema}`
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
  } else if (request.kind === 'redis-keys') {
    tab = {
      key: `rkeys:${request.conn.id}:${request.db}`,
      kind: 'redis-keys',
      title: `db${request.db} key 列表 @ ${request.conn.name}`,
      conn: request.conn,
      db: request.db,
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
  } else if (request.kind === 'erd') {
    tab = {
      key: `erd:${request.conn.id}:${request.schema}`,
      kind: 'erd',
      title: `${request.schema} ER 图 @ ${request.conn.name}`,
      conn: request.conn,
      schema: request.schema,
    }
  } else {
    tab = {
      key: `${request.kind}:${request.conn.id}:${request.schema}:${request.table}`,
      kind: request.kind,
      title:
        request.kind === 'data'
          ? `${request.table} @ ${request.conn.name}`
          : request.kind === 'design'
            ? `${request.table} 设计 @ ${request.conn.name}`
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
  dropWorkbenchSql(tabKey)
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

/** 批量关页签：草稿跟着页签一起删，别在存储里留一份没人认领的 SQL。 */
function closeTabsWhere(shouldClose: (tab: Tab) => boolean) {
  const kept: Tab[] = []
  for (const tab of tabs.value) {
    if (shouldClose(tab)) dropWorkbenchSql(tab.key)
    else kept.push(tab)
  }
  if (kept.length === tabs.value.length) return
  tabs.value = kept
  if (!kept.some((tab) => tab.key === activeTabKey.value)) {
    activeTabKey.value = kept[0]?.key
  }
}

function closeTabsOf(connId: number) {
  closeTabsWhere((tab) => tab.conn.id === connId)
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
  if (node.kind === 'redisdb')
    return (tab.kind === 'redis-key' || tab.kind === 'redis-keys') && tab.db === (node.db ?? 0)
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
      closeTabsWhere((tab) => tabOfDatabase(tab, node))
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
        { key: 'open-erd', label: 'ER 图' },
        { key: 'backup', label: '一键备份' },
        { key: 'restore', label: '还原备份…' },
        { key: 'refresh', label: '刷新' },
      ]
    case 'table':
      return [
        { key: 'open-data', label: '打开数据' },
        { key: 'open-structure', label: '查看结构' },
        // 视图没有可编辑的定义，设计表只对基表开放。
        ...(node.tableType === 'VIEW'
          ? []
          : [{ key: 'open-design', label: '设计表' }]),
      ]
    case 'redisdb':
      return [
        // 树里一次只看得见一页 key，批量删除得有个能勾选的列表页签。
        { key: 'open-keys', label: 'key 列表' },
        { key: 'refresh', label: '刷新' },
      ]
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
    case 'open-design':
      openTab({ kind: 'design', conn, schema: node.schema!, table: node.table! })
      break
    case 'open-erd':
      openTab({ kind: 'erd', conn, schema: node.schema! })
      break
    case 'backup':
      openBackup(conn, node.schema!)
      break
    case 'restore':
      openRestore(conn, node.schema!, node)
      break
    case 'open-keys':
      openTab({ kind: 'redis-keys', conn, db: node.db ?? 0 })
      break
    case 'open-key':
      openTab({ kind: 'redis-key', conn, db: node.db ?? 0, rkey: node.title })
      break
  }
}

// ---- 一键备份 -----------------------------------------------------------------

const backup = reactive({
  open: false,
  running: false,
  connId: 0,
  schema: '',
  structureOnly: false,
  tables: [] as DbTableItem[],
  tablesLoading: false,
  tablesError: false,
  selected: [] as string[],
})

const BACKUP_TOAST = 'ops-db-backup'

/** 下拉里的候选：库里的表在前、视图在后——视图的 INSERT 本来就不进备份文件。 */
const backupOptions = computed(() => {
  const base = backup.tables.filter((t) => t.table_type !== 'VIEW')
  const views = backup.tables.filter((t) => t.table_type === 'VIEW')
  return [...base, ...views].map((t) => ({
    value: t.name,
    label: t.table_type === 'VIEW' ? `${t.name}（视图）` : t.name,
  }))
})

function openBackup(conn: OpsDatabase, schema: string) {
  backup.connId = conn.id
  backup.schema = schema
  backup.structureOnly = false
  backup.tables = []
  backup.selected = []
  backup.tablesError = false
  backup.open = true
  loadBackupTables()
}

/**
 * 取这一库的表清单供勾选。
 *
 * 失败不拦备份：名单为空时提交的就是整库，与加选表之前的行为一致，
 * 用户仍可照常备份，只是这一次挑不了表。
 */
async function loadBackupTables() {
  const { connId, schema } = backup
  backup.tablesLoading = true
  try {
    const res = await opsApi.dbTables(connId, schema)
    if (backup.schema !== schema || backup.connId !== connId) return
    backup.tables = res.items
  } catch (err: any) {
    if (backup.schema === schema) backup.tablesError = true
    toast.error(err?.message || '获取表清单失败')
  } finally {
    backup.tablesLoading = false
  }
}

/**
 * 备份这一个库（或选中的几张表）。网盘是主路径（存档留在服务端一侧），下载是同一次生成的另一出口。
 *
 * 两条路都不设前端超时：整库读取加上网盘的三段式上传，30 秒的全局默认必然误杀。
 */
async function runBackup(target: 'netdisk' | 'download') {
  if (backup.running) return
  backup.running = true
  const scope = backup.structureOnly ? '仅结构' : '结构 + 数据'
  const picked = backup.selected.length ? `已选 ${backup.selected.length} 张表` : '整库'
  toast.open({
    key: BACKUP_TOAST,
    content: `正在备份 ${backup.schema}（${picked}，${scope}）…`,
    duration: 0,
  })
  const tables = backup.selected.length ? [...backup.selected] : undefined
  try {
    if (target === 'netdisk') {
      const res = await opsApi.dbBackupToNetdisk(
        backup.connId,
        backup.schema,
        backup.structureOnly,
        tables,
      )
      toast.success({ key: BACKUP_TOAST, content: `已保存到网盘：${res.path}`, duration: 8 })
    } else {
      const res = await opsApi.dbBackupDownload(
        backup.connId,
        backup.schema,
        backup.structureOnly,
        tables,
      )
      await saveBlobResponse(res, `${backup.schema}.sql`)
      toast.success({ key: BACKUP_TOAST, content: '备份完成', duration: 2 })
    }
    backup.open = false
  } catch (err: any) {
    toast.error({ key: BACKUP_TOAST, content: err?.message || '备份失败', duration: 6 })
  } finally {
    backup.running = false
  }
}

// ---- 还原备份 / 导入 SQL -------------------------------------------------------

const restore = reactive({
  open: false,
  running: false,
  finished: false,
  connId: 0,
  connName: '',
  schema: '',
  /** 台账写开关 × 账号运维写权限，两道都过才谈得上还原（服务端同样判一次）。 */
  allowed: false,
  stopOnError: false,
  file: null as File | null,
  total: 0,
  done: 0,
  succeeded: 0,
  failed: 0,
  skipped: 0,
  notRun: 0,
  elapsedMs: 0,
  stmtTimeout: 0,
  uses: [] as string[],
  errors: [] as RestoreErrorItem[],
  /** 停止、断开、连接被拒这类「为什么没跑完」的一句话。 */
  note: '',
})

const restoreInput = ref<HTMLInputElement | null>(null)
let restoreAbort: (() => void) | null = null
// 树里被还原的那个库节点：跑完要刷新它，表清单大概率已经不是原来那份了。
// 放在 reactive 外面——节点对象归树自己所有，不该被弹窗再包一层响应式。
let restoreNode: TreeNode | null = null

/** 进度按「处理过的句数」走：没跑完之前不封顶到 100，跑完才给满格。 */
const restorePercent = computed(() =>
  restore.total
    ? restore.finished
      ? 100
      : Math.min(99, Math.floor((restore.done / restore.total) * 100))
    : 0,
)

/** 跑完之前进度条一直是 active：半路停下（错误、停止、断线）用 exception，全绿才算 success。 */
const restoreStatus = computed(() => {
  if (!restore.finished) return 'active'
  return restore.failed || restore.notRun || restore.note ? 'exception' : 'success'
})

function openRestore(conn: OpsDatabase, schema: string, node: TreeNode) {
  restore.connId = conn.id
  restore.connName = conn.name
  restore.schema = schema
  restore.allowed = Boolean(conn.writable && auth.canOpsWrite)
  restore.stopOnError = false
  restore.file = null
  restoreNode = node
  resetRestoreProgress()
  restore.open = true
}

function resetRestoreProgress() {
  restore.finished = false
  restore.total = 0
  restore.done = 0
  restore.succeeded = 0
  restore.failed = 0
  restore.skipped = 0
  restore.notRun = 0
  restore.elapsedMs = 0
  restore.stmtTimeout = 0
  restore.uses = []
  restore.errors = []
  restore.note = ''
}

function onPickRestoreFile(event: Event) {
  const input = event.target as HTMLInputElement
  restore.file = input.files?.[0] ?? null
  resetRestoreProgress()
  // 同一个文件重选一次也要能触发：input 的 value 不清掉就不算 change。
  input.value = ''
}

function runRestore() {
  if (restore.running || !restore.file) return
  const { connId, schema, file, stopOnError } = restore
  resetRestoreProgress()
  restore.running = true
  restoreAbort = streamRestore(
    connId,
    schema,
    file,
    {
      onStart: (data) => {
        restore.total = data.total
        restore.uses = data.uses || []
        restore.stmtTimeout = data.stmt_timeout_seconds
      },
      onProgress: (data) => {
        restore.done = data.done
        restore.succeeded = data.succeeded
        restore.failed = data.failed
        restore.skipped = data.skipped
        restore.notRun = data.not_run
        // 每帧带的是累积到现在的样本（服务端全局只留前 50 条），整份替换即可。
        if (data.errors?.length) restore.errors = data.errors
        if (data.stop_reason) restore.note = data.stop_reason
      },
      onDone: (data) => {
        restore.done = data.done
        restore.succeeded = data.succeeded
        restore.failed = data.failed
        restore.skipped = data.skipped
        restore.notRun = data.not_run
        restore.elapsedMs = data.elapsed_ms
        restore.errors = data.errors || []
        restore.note = data.stop_reason || ''
        restore.running = false
        restore.finished = true
        restoreAbort = null
        if (data.failed || data.not_run) {
          toast.warning({
            content: `还原结束：成功 ${data.succeeded} 句，失败 ${data.failed} 句，未执行 ${data.not_run} 句`,
            duration: 8,
          })
        } else {
          toast.success({ content: `还原完成：${data.succeeded} 句`, duration: 6 })
        }
        // 表清单大概率换了：刷新还原目标那个库节点。
        if (restoreNode) treeRef.value?.refresh(restoreNode)
      },
      onError: (message) => {
        restore.running = false
        restore.finished = true
        restore.note = message
        restoreAbort = null
        toast.error({ content: message, duration: 8 })
      },
    },
    { stopOnError },
  )
}

/**
 * 「停止」只断这一条进度流。
 *
 * 服务端那边的会话随之断开，正在执行的那一批跑完当前语句后就不再继续；已经落盘的
 * 语句不回滚——这是 autocommit 逐句执行的唯一诚实说法，界面必须照着说。
 */
function stopRestore() {
  restoreAbort?.()
  restoreAbort = null
  restore.running = false
  restore.finished = true
  restore.note = '已停止收进度：正在执行的语句跑完之后不再继续，已经写入的不会回滚。'
}

// 关掉这个标签页（keep-alive 销毁）后弹窗连同状态一起没了，进度没地方放：
// 断流，服务端那一侧照上面的口径继续跑完当前批。
onBeforeUnmount(() => {
  restoreAbort?.()
  restoreAbort = null
})

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

// 手机端由 DbMobileBrowser 自己拉连接列表，桌面工作台不重复请求；窗口从手机
// 拉宽回桌面时 keep-alive 不会重跑 onMounted，靠 watch 补一次加载。
const isMobile = useIsMobile()
onMounted(() => {
  if (!isMobile.value) load()
})
watch(isMobile, (mobile) => {
  if (!mobile && !databases.value.length) load()
})
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
  z-index: var(--z-sticky);
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
  background: var(--signal-border);
}

.panel-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

/* 连接面板与右侧工作台的内边距比通用卡片紧一档：DBX 的页签栏离面板边缘
   只有一条窄缝，密度与工作台保持一致。 */
.list-panel :deep(.ant-card-body),
.main-panel :deep(.ant-card-body) {
  position: relative;
  padding: var(--tool-gap) var(--grid-pad-inline);
}

.filters {
  display: flex;
  gap: var(--tool-gap);
  margin-bottom: var(--tool-gap);
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
  font-size: var(--fs-sm);
  pointer-events: none;
}

.empty-icon {
  font-size: 40px;
  color: var(--ghost-icon);
}

.empty-hint {
  font-size: var(--fs-xs);
  color: var(--disabled-text);
}

.writable-hint {
  font-size: var(--fs-xs);
  color: var(--text-3);
}

.backup-hint {
  margin: -8px 0 16px;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-3);
}

.backup-tables {
  width: 100%;
}

/* 标签右侧的进度式提示：选了几个 / 一共有几个，勾选时不用自己数。 */
.backup-scope {
  margin-left: 8px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}

/* ---- 还原弹窗 ---- */
.restore-file {
  font-size: var(--fs-xs);
  color: var(--text-3);
}

/* 真的选文件的是那个隐藏 input，按钮只是替它挨一下点击。 */
.restore-file-input {
  display: none;
}

.restore-hint {
  margin: -8px 0 12px;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-3);
}

.restore-warn {
  color: var(--warning);
}

.restore-counts {
  margin: 0 0 12px;
  font-size: var(--fs-xs);
  color: var(--text-2);
}

/* 失败样本最多 50 条，全铺会把弹窗撑出屏幕：给一段自己的滚动区。 */
.restore-errors {
  max-height: 180px;
  margin-bottom: 12px;
  padding: 8px 10px;
  overflow: auto;
  background: var(--danger-bg);
  border: 1px solid var(--danger-border);
  border-radius: 6px;
}

.restore-error + .restore-error {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--hairline);
}

.restore-error-where {
  margin-right: 6px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}

.restore-error-msg {
  font-size: var(--fs-xs);
  color: var(--danger);
}

.restore-error-sql {
  margin-top: 2px;
  font-size: var(--fs-xs);
  color: var(--text-2);
  word-break: break-all;
  white-space: pre-wrap;
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

/* 工作台页签对齐 DBX 的 app-tab-bar：等高的小胶囊（28px）、4~6px 圆角、
   非激活不带灰底，悬停是中性灰，只有激活那一枚拿信号色淡底 + 描边。
   上下 padding 归零、高度固定，页签栏和内容的间距收到 --tool-gap。 */
.main-panel :deep(.ant-tabs-card > .ant-tabs-nav .ant-tabs-tab) {
  display: inline-flex;
  align-items: center;
  padding: 0 10px;
  height: 28px;
  margin: 0 4px 0 0 !important;
  border: 1px solid transparent !important;
  border-radius: var(--tool-radius) !important;
  background: transparent;
  transition:
    background-color 150ms ease,
    border-color 150ms ease,
    color 150ms ease;
}

.main-panel :deep(.ant-tabs-card > .ant-tabs-nav .ant-tabs-tab:hover) {
  background: var(--grid-hover);
}

.main-panel :deep(.ant-tabs-card > .ant-tabs-nav .ant-tabs-tab-active) {
  border-color: var(--signal-border) !important;
  background: var(--signal-bg);
}

.main-panel :deep(.ant-tabs-top > .ant-tabs-nav) {
  margin-bottom: var(--tool-gap);
}

/* 关闭钮平时收着，悬停／激活页签上才露出来（DBX 同款），省掉一整列灰叉。 */
.main-panel :deep(.ant-tabs-nav .ant-tabs-tab .ant-tabs-tab-remove) {
  margin-inline-start: 2px;
  opacity: 0;
  transition: opacity 150ms ease;
}

.main-panel :deep(.ant-tabs-nav .ant-tabs-tab:hover .ant-tabs-tab-remove),
.main-panel :deep(.ant-tabs-nav .ant-tabs-tab-active .ant-tabs-tab-remove) {
  opacity: 1;
}

/* 页签整格染色：::before 以 .ant-tabs-tab 为定位盒铺满整格（inset:0 不受
   tab 内边距影响），label 里的图标 / 文案和关闭钮提到染色层之上；
   pointer-events 关掉，点关闭钮不会被这层挡住。染色层跟着胶囊圆角。 */
.main-panel :deep(.ant-tabs-tab) {
  position: relative;
}

.main-panel :deep(.ant-tabs-tab .tab-label.tinted)::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: var(--tool-radius);
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
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  cursor: pointer;
}

.swatch.active {
  outline: 2px solid var(--signal-text);
  outline-offset: 1px;
}

.swatch-none {
  position: relative;
  background: var(--surface);
}

.swatch-none::after {
  content: '';
  position: absolute;
  left: 2px;
  right: 2px;
  top: 50%;
  height: 1px;
  background: var(--danger);
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
  z-index: var(--z-modal);
}

.menu {
  position: fixed;
  min-width: 148px;
  padding: 4px;
  border: 1px solid var(--hairline);
  border-radius: var(--tool-radius);
  background: var(--surface-raised);
  box-shadow: var(--shadow-overlay);
}

/* 菜单项的尺度和右键菜单（GridContextMenu 的 .gmenu-item）对齐：一行约 28px、
   悬停中性灰，两处菜单看起来是同一族控件。 */
.menu-item {
  padding: 5px var(--grid-pad-inline);
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: background 0.15s;
}

.menu-item:hover {
  background: var(--grid-hover);
}

.menu-item.danger {
  color: var(--danger);
}

.menu-item.disabled {
  color: var(--disabled-text);
  cursor: not-allowed;
}

.menu-item.disabled:hover {
  background: transparent;
}
</style>
