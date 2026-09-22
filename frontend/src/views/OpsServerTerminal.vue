<template>
  <div class="term-page">
    <header class="term-topbar">
      <div class="topbar-left">
        <a-tooltip title="返回列表">
          <button type="button" class="icon-btn" @click="back">
            <ArrowLeftOutlined />
          </button>
        </a-tooltip>
        <template v-if="server">
          <span class="server-name">{{ server.name }}</span>
          <span class="server-addr">{{ address }}</span>
          <a-tooltip v-if="server.credential_error" title="凭据无法解密，请回列表页编辑">
            <WarningOutlined class="cred-warning" />
          </a-tooltip>
        </template>
        <span v-else class="server-name muted">{{ loadError || '加载中…' }}</span>
      </div>
      <div class="topbar-right">
        <span :class="['conn-dot', terminalStatus]" />
        <span class="conn-text">
          {{
            autoReconnecting
              ? `连接断开，正在重连（第 ${autoReconnectAttempt} 次）…`
              : STATUS_TEXT[terminalStatus]
          }}
        </span>
        <a-button v-if="terminalStatus === 'closed' && server" size="small" ghost @click="reconnect">
          <ReloadOutlined /> {{ autoReconnecting ? '立即重连' : '重连' }}
        </a-button>
        <a-button size="small" ghost @click="back">返回列表</a-button>
      </div>
    </header>

    <div class="term-main">
      <nav class="term-rail">
        <a-tooltip :title="showFiles ? '收起文件面板' : '文件管理'" placement="right">
          <button
            type="button"
            :class="['rail-btn', { active: showFiles }]"
            @click="toggleFiles"
          >
            <FolderOutlined />
          </button>
        </a-tooltip>
        <a-tooltip :title="showAi ? '收起 AI 助手' : '展开 AI 助手'" placement="right">
          <button
            type="button"
            :class="['rail-btn', { active: showAi }]"
            @click="toggleAi"
          >
            <RobotOutlined />
          </button>
        </a-tooltip>
      </nav>

      <!-- v-show 保住浏览路径与传输列表；v-if=server 避免服务器信息没加载完就发请求。 -->
      <aside v-show="showFiles" class="term-files">
        <div class="left-tabs">
          <button
            type="button"
            :class="['left-tab', { active: leftTab === 'files' }]"
            @click="leftTab = 'files'"
          >
            文件
          </button>
          <button
            type="button"
            :class="['left-tab', { active: leftTab === 'transfer' }]"
            @click="leftTab = 'transfer'"
          >
            传输
            <span v-if="transfers.runningCount" class="tab-badge">
              {{ transfers.runningCount }}
            </span>
          </button>
          <a-tooltip title="收起">
            <button type="button" class="left-tab-close" @click="toggleFiles">
              <DoubleLeftOutlined />
            </button>
          </a-tooltip>
        </div>
        <ServerFilePanel v-if="server" v-show="leftTab === 'files'" :server-id="serverId" />
        <TransferPanel v-show="leftTab === 'transfer'" />
      </aside>

      <div class="term-stage">
        <!-- 只读账号不开 PTY：后端握手也会以 1008 拒，这里直接给说明，
             省得看着一个连不上的黑窗。文件浏览与 AI 问答保持可用。 -->
        <div v-if="!auth.canOpsWrite" class="term-error">
          <WarningOutlined class="term-error-icon" />
          <div class="term-error-text">
            只读账号，无法打开终端（需要运维写权限）。左侧文件浏览与右侧 AI 问答不受影响。
          </div>
        </div>
        <ServerTerminal
          v-else-if="server"
          ref="terminalRef"
          :key="reconnectSeq"
          :server-id="serverId"
          @status="onTerminalStatus"
        />
        <div v-else-if="loadError" class="term-error">
          <CloudServerOutlined class="term-error-icon" />
          <div class="term-error-text">{{ loadError }}</div>
          <a-button size="small" @click="back">返回列表</a-button>
        </div>
      </div>

      <!-- 收起后留一根右缘竖条作为展开入口，不然只能去最左边的图标栏找开关。 -->
      <div v-if="!showAi" class="ai-rail" @click="toggleAi">
        <LeftOutlined />
        <span class="ai-rail-text">AI 助手</span>
      </div>

      <!-- v-show 而不是 v-if：折叠只是藏起来，会话和 WebSocket 都保住。 -->
      <aside v-show="showAi" class="term-ai" :style="{ width: `${aiWidth}px` }">
        <!-- 左缘拖拽把柄：绝对定位叠在面板与终端的缝隙上，不参与布局。 -->
        <div class="ai-resizer" @mousedown="startAiDrag" />
        <div class="ai-head">
          <span>AI 助手</span>
          <a-tooltip title="收起">
            <button type="button" class="icon-btn light" @click="toggleAi">
              <RightOutlined />
            </button>
          </a-tooltip>
        </div>
        <div class="ai-body">
          <OpsChat
            target="server"
            dark
            :target-id="server ? serverId : null"
            placeholder="例如：这台机器磁盘为什么满了"
            :empty-hint="loadError || '正在加载服务器信息…'"
            :samples="CHAT_SAMPLES"
            @exec="onAiExec"
            @send-command="onChatCommand"
          />
        </div>
      </aside>
    </div>

    <footer class="term-statusbar">
      <span :class="['conn-dot', terminalStatus]" />
      <span>{{ STATUS_TEXT[terminalStatus] }}</span>
      <span v-if="server" class="statusbar-addr">{{ address }}</span>
      <span class="statusbar-hint">AI 执行的命令会同步显示在终端里</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeftOutlined,
  CloudServerOutlined,
  DoubleLeftOutlined,
  FolderOutlined,
  LeftOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import OpsChat from '@/components/OpsChat.vue'
import ServerFilePanel from '@/components/ServerFilePanel.vue'
import ServerTerminal from '@/components/ServerTerminal.vue'
import TransferPanel from '@/components/TransferPanel.vue'
import { clampSize, readSize, saveSize, startDragResize } from '@/components/ops/resizer'
import '@/components/ops/resizer.css'
import { opsApi, type OpsServer } from '@/api'
import { useAuthStore } from '@/stores/auth'
import { useTransfersStore } from '@/stores/transfers'
import { storageKeys } from '@/utils/storage'

const CHAT_SAMPLES = [
  '磁盘占用最高的几个目录是什么',
  '现在负载高吗，是哪个进程占的',
  '最近有没有异常的登录记录',
]

const STATUS_TEXT: Record<string, string> = {
  closed: '未连接',
  connecting: '连接中',
  connected: '已连接',
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
// 路由有 \d+ 约束，到这里一定是数字。
const serverId = Number(route.params.id)

const server = ref<OpsServer | null>(null)
const loadError = ref('')
const terminalStatus = ref<'connecting' | 'connected' | 'closed'>('closed')
const showAi = ref(true)
// 文件面板默认收起：它走独立的 SFTP 连接，打开时才第一次列目录。
const showFiles = ref(false)
const leftTab = ref<'files' | 'transfer'>('files')
const transfers = useTransfersStore()
// 重连 = 换掉 key 重挂载 ServerTerminal，比给它加重连方法简单，状态也干净。
const reconnectSeq = ref(0)
const terminalRef = ref<InstanceType<typeof ServerTerminal> | null>(null)
// 终端还没连上时收到的「送到终端」命令先存着，连上后立即补送。
let pendingInput = ''

const address = computed(() =>
  server.value ? `${server.value.username}@${server.value.host}:${server.value.port}` : '',
)

onMounted(async () => {
  // 没有单条 GET，台账量小，从列表里挑这一条。
  try {
    const res = await opsApi.listServers()
    server.value = res.items.find((item) => item.id === serverId) ?? null
    if (!server.value) loadError.value = '服务器不存在或已被删除'
  } catch {
    loadError.value = '服务器信息加载失败'
  }
})

function back() {
  router.push({ name: 'OpsServer' })
}

function reconnect() {
  // 手动重连：停掉自动重连的倒计时并清零次数（用户已经等不及了）。
  autoReconnectAttempt.value = 0
  cancelAutoReconnect()
  reconnectSeq.value += 1
}

function toggleAi() {
  showAi.value = !showAi.value
}

// ---- AI 面板拖拽调宽 -----------------------------------------------------------
// 默认 440px，比初版 360 宽一档（AI 回答里的命令与表格多了）；宽度偏好记
// localStorage，与数据库工作台两侧面板同一套机制。
const aiWidth = ref(readSize(storageKeys.opsTermAiWidth, 440))

function startAiDrag(event: MouseEvent) {
  const startWidth = aiWidth.value
  startDragResize(
    event,
    'x',
    // 面板在右侧：往左拖变宽，位移取反。
    (delta) => { aiWidth.value = clampSize(startWidth - delta, 320, 760) },
    () => {
      saveSize(storageKeys.opsTermAiWidth, aiWidth.value)
      // 宽度落定后 fit 一次校正 PTY 列数；拖拽途中不 fit，免得鼠标每动
      // 一格就给后端发一次 resize。
      terminalRef.value?.fit()
    },
  )
}

function toggleFiles() {
  showFiles.value = !showFiles.value
}

// 面板开合改变终端容器宽度，主动 fit 一次校正 PTY 列数。
watch([showAi, showFiles], () => {
  nextTick(() => terminalRef.value?.fit())
})

// 有新传输发起时自动切到「传输」标签页，让进度直接可见。
watch(
  () => transfers.lastStartedAt,
  () => {
    if (showFiles.value) leftTab.value = 'transfer'
  },
)

/** AI 实际执行的命令：打到终端窗口里展示，让用户看到 AI 干了什么。 */
function onAiExec(payload: { command: string; output?: string; exit_status?: number }) {
  terminalRef.value?.writeAiExec(payload.command, payload.output, payload.exit_status)
}

/** 聊天里命令代码块的「在终端中打开」：送到 PTY 输入，不自动回车。 */
function onChatCommand(command: string) {
  if (terminalStatus.value !== 'connected') {
    pendingInput = command
    return
  }
  terminalRef.value?.sendInput(command)
}

// ---- 断线自动重连 ------------------------------------------------------------
// 网络层断开（区别于 1008 这类服务端主动拒绝——鉴权失败、查无此机，重连也
// 是白费）自动重连：指数退避 1s→2s→4s…封顶 10s，最多 5 次，连上即清零。
// 放弃之后顶栏的手动重连按钮依然可用。
const AUTO_RECONNECT_MAX = 5
let autoReconnectTimer: number | undefined
const autoReconnectAttempt = ref(0)
const autoReconnecting = ref(false)

function cancelAutoReconnect() {
  if (autoReconnectTimer !== undefined) {
    window.clearTimeout(autoReconnectTimer)
    autoReconnectTimer = undefined
  }
  autoReconnecting.value = false
}

function scheduleAutoReconnect() {
  if (autoReconnectTimer !== undefined) return
  if (autoReconnectAttempt.value >= AUTO_RECONNECT_MAX) {
    // 放弃自动重连：状态定格在 closed，手动按钮兜底。
    autoReconnecting.value = false
    return
  }
  autoReconnecting.value = true
  // 这次失败也算一次尝试，所以先加再算延迟。
  autoReconnectAttempt.value += 1
  const delay = Math.min(1000 * 2 ** (autoReconnectAttempt.value - 1), 10000)
  autoReconnectTimer = window.setTimeout(() => {
    autoReconnectTimer = undefined
    // 重连 = 重挂载 ServerTerminal（与手动 reconnect 同一条路）。
    reconnectSeq.value += 1
  }, delay)
}

function onTerminalStatus(value: 'connecting' | 'connected' | 'closed', code?: number) {
  terminalStatus.value = value
  if (value === 'connected') {
    autoReconnectAttempt.value = 0
    cancelAutoReconnect()
  } else if (value === 'closed' && code !== 1008) {
    scheduleAutoReconnect()
  }
  if (value === 'connected' && pendingInput) {
    terminalRef.value?.sendInput(pendingInput)
    pendingInput = ''
  }
}
</script>

<style scoped>
/* 项目没有全局盒模型重置，显式高度/拉伸 + padding 的元素在 content-box 下会
   比预期高出一截（右缘竖条曾因此顶进状态栏）。本页统一 border-box。 */
.term-page,
.term-page * {
  box-sizing: border-box;
}

/* 深色独立世界：页面底比终端 (#11181f) 略深一层，顶/底栏与终端同色，
   强调青沿用终端光标色。不套站点浅色变量——这是另一个语境。 */
.term-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #0d141b;
  color: #d8dee4;
}

.term-topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 48px;
  flex-shrink: 0;
  padding: 0 16px;
  background: #11181f;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.topbar-left,
.topbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.server-name {
  font-size: 14px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.server-name.muted {
  color: #8b98a5;
  font-weight: 400;
}

.server-addr {
  color: #8b98a5;
  font-family: var(--font-mono);
  font-size: 12px;
  white-space: nowrap;
}

.cred-warning {
  color: #faad14;
}

/* 深色页上的 ghost 按钮默认样式偏亮，压一压。 */
.term-topbar :deep(.ant-btn-ghost) {
  color: #d8dee4;
  border-color: rgba(255, 255, 255, 0.25);
}

.term-topbar :deep(.ant-btn-ghost:hover) {
  color: #3ad6de;
  border-color: #3ad6de;
}

.term-main {
  display: flex;
  flex: 1;
  min-height: 0;
}

.term-rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 44px;
  flex-shrink: 0;
  padding: 10px 0;
  border-right: 1px solid rgba(255, 255, 255, 0.08);
}

.icon-btn,
.rail-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #8b98a5;
  font-size: 15px;
  cursor: pointer;
  transition: all 0.15s;
}

.icon-btn:hover,
.rail-btn:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #d8dee4;
}

.rail-btn.active {
  background: rgba(58, 214, 222, 0.15);
  color: #3ad6de;
}

/* AI 面板头部的收起按钮比顶栏的小一号。 */
.icon-btn.light {
  width: 26px;
  height: 26px;
  font-size: 13px;
}

/* 文件面板在图标栏与终端之间，与 AI 面板同款底色和发丝线。 */
.term-files {
  display: flex;
  flex-direction: column;
  width: 300px;
  flex-shrink: 0;
  margin: 10px 0 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #11181f;
  overflow: hidden;
}

/* 「文件 / 传输」标签栏：选中项底部一道青色，沿用 rail 按钮的强调色。 */
.left-tabs {
  display: flex;
  align-items: center;
  flex-shrink: 0;
  padding: 0 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.left-tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 9px 10px 8px;
  border: none;
  background: transparent;
  color: #8b98a5;
  font-size: 13px;
  cursor: pointer;
}

.left-tab:hover {
  color: #d8dee4;
}

.left-tab.active {
  color: #3ad6de;
}

.left-tab.active::after {
  content: '';
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: -1px;
  height: 2px;
  border-radius: 1px;
  background: #3ad6de;
}

.tab-badge {
  min-width: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: rgba(58, 214, 222, 0.2);
  color: #3ad6de;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}

.left-tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  margin-left: auto;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: #8b98a5;
  font-size: 12px;
  cursor: pointer;
}

.left-tab-close:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #d8dee4;
}

.term-stage {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  display: flex;
}

.term-stage > * {
  flex: 1;
  min-width: 0;
}

.term-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  border-radius: 6px;
  background: #11181f;
  color: #8b98a5;
}

.term-error-icon {
  font-size: 36px;
  color: rgba(255, 255, 255, 0.2);
}

.term-error-text {
  font-size: 13px;
}

/* AI 面板收起后的展开入口：右缘一根竖条，悬停时用强调青提示可点。 */
.ai-rail {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  width: 32px;
  flex-shrink: 0;
  margin: 10px 12px 10px 0;
  padding: 12px 0;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #11181f;
  color: #8b98a5;
  cursor: pointer;
  transition: all 0.15s;
}

.ai-rail:hover {
  border-color: rgba(58, 214, 222, 0.45);
  color: #3ad6de;
}

.ai-rail-text {
  writing-mode: vertical-rl;
  letter-spacing: 2px;
  font-size: 12px;
}

/* AI 面板与终端同底 (#11181f)，靠一道发丝线和终端区分开。
   宽度由模板里的内联样式给（可拖拽），这里只定布局。 */
.term-ai {
  position: relative;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  margin: 10px 12px 10px 0;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  background: #11181f;
  overflow: hidden;
}

/* 左缘拖拽把柄：12px 热区贴面板左缘内侧（面板 overflow:hidden，不能探出去），
   悬停/拖拽（body.dragging-col，全局规则在 resizer.css）时亮出一条青色细线。 */
.ai-resizer {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 12px;
  display: flex;
  justify-content: center;
  cursor: col-resize;
  z-index: 6;
}

.ai-resizer::after {
  content: '';
  width: 2px;
  border-radius: 1px;
  background: transparent;
  transition: background 0.15s;
}

.ai-resizer:hover::after,
body.dragging-col .ai-resizer::after {
  background: rgba(58, 214, 222, 0.6);
}

.ai-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  color: #d8dee4;
  font-size: 13px;
  font-weight: 600;
}

.ai-body {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  padding: 8px 12px 12px;
}

.term-statusbar {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 26px;
  flex-shrink: 0;
  padding: 0 16px;
  background: #11181f;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  color: #8b98a5;
  font-family: var(--font-mono);
  font-size: 11px;
}

.statusbar-addr {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.statusbar-hint {
  margin-left: auto;
  white-space: nowrap;
}

.conn-dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: 50%;
  background: #6e7a86;
}

.conn-dot.connecting {
  background: #faad14;
}

.conn-dot.connected {
  background: #3ad6de;
}

.conn-text {
  font-size: 12px;
  color: #8b98a5;
}
</style>
