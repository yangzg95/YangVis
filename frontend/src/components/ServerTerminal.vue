<template>
  <div :class="['terminal-wrap', { 'terminal-light': terminalTheme === 'light' }]" @keydown="onWrapKeydown">
    <div ref="hostEl" class="terminal-host" />
    <!-- Ctrl+F 唤出的终端内容搜索：Enter 下一个、Shift+Enter 上一个、Esc 关闭。 -->
    <div v-if="searchOpen" class="terminal-search">
      <input
        ref="searchInputRef"
        v-model="searchKeyword"
        class="search-input"
        placeholder="搜索终端内容"
        @input="findNext"
        @keydown.enter.exact.prevent="findNext"
        @keydown.shift.enter.prevent="findPrev"
        @keydown.esc.prevent="closeSearch"
      />
      <button type="button" class="search-btn" title="上一个（Shift+Enter）" @click="findPrev">
        <ArrowUpOutlined />
      </button>
      <button type="button" class="search-btn" title="下一个（Enter）" @click="findNext">
        <ArrowDownOutlined />
      </button>
      <button type="button" class="search-btn" title="关闭（Esc）" @click="closeSearch">
        <CloseOutlined />
      </button>
    </div>
    <div v-if="!serverId" class="terminal-mask">
      <div>在左侧选中一台服务器并点「连接」</div>
    </div>
    <!-- 右键菜单：复制 / 粘贴 / 全选 / 清屏，和文件面板共用同一个深色组件。 -->
    <DarkContextMenu
      :open="menu.open"
      :x="menu.x"
      :y="menu.y"
      :items="menuItems"
      @pick="onMenuPick"
      @close="menu.open = false"
    />
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { SearchAddon } from 'xterm-addon-search'
import { WebLinksAddon } from 'xterm-addon-web-links'
import 'xterm/css/xterm.css'
import {
  ArrowDownOutlined,
  ArrowUpOutlined,
  ClearOutlined,
  CloseOutlined,
  CopyOutlined,
  FolderOpenOutlined,
  SelectOutlined,
  SnippetsOutlined,
} from '@ant-design/icons-vue'
import { opsApi, opsWsUrl } from '@/api'
import DarkContextMenu, { type DarkMenuItem } from './DarkContextMenu.vue'

const props = defineProps<{ serverId: number | null }>()
const emit = defineEmits<{
  // closed 时带上 WebSocket 断开码：父组件据此区分「服务端主动拒绝」
  // （1008 鉴权/查无此机，自动重连没意义）和网络层断开（值得自动重连）。
  (e: 'status', value: 'connecting' | 'connected' | 'closed', code?: number): void
  // 右键「在文件浏览器打开」：拿到终端当前目录后交给父页面展开文件面板。
  (e: 'browseFiles', path: string): void
}>()

const hostEl = ref<HTMLElement | null>(null)

// 服务端控制帧前缀，与 backend/app/services/ops/terminal.py 的 CONTROL_MARK 对应。
// 0x1e 在真实终端输出里几乎不会出现，专门用来和裸 PTY 输出消歧。
const CONTROL_MARK = '\x1e'
// 本端主动断开（切走服务器/卸载），告诉父页面不要自动重连。
const WS_INTENTIONAL = 4000

let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let searchAddon: SearchAddon | null = null
let socket: WebSocket | null = null
let observer: ResizeObserver | null = null
// 每次连接自增。异步拿票的过程中用户可能已经切走了，回来时靠它判断这张票是否过期。
let generation = 0
// 用户已键入但还没回车的内容。AI 补提示符时要避开它，免得把半条命令提交上去。
let pendingLine = ''
// 控制帧提前告知的关闭原因（会话结束 / 被新页面顶掉），onclose 时展示。
let closeNote: string | null = null

// ---- 终端主题切换 -----------------------------------------------------------
// 深色/浅色两套配色，存 localStorage 持久化。
const TERMINAL_THEME_KEY = 'yangvis-terminal-theme'
type TerminalTheme = 'dark' | 'light'

const TERMINAL_THEMES = {
  dark: {
    background: '#11181f',
    foreground: '#d8dee4',
    cursor: '#3ad6de',
    selectionBackground: 'rgba(58, 214, 222, 0.25)',
  },
  light: {
    background: '#f5f5f5',
    foreground: '#1a1a1a',
    cursor: '#0080ff',
    selectionBackground: 'rgba(0, 128, 255, 0.2)',
  },
} as const

const terminalTheme = ref<TerminalTheme>(
  (localStorage.getItem(TERMINAL_THEME_KEY) as TerminalTheme) || 'dark',
)

function applyTheme() {
  if (!term) return
  term.options.theme = TERMINAL_THEMES[terminalTheme.value]
  try {
    localStorage.setItem(TERMINAL_THEME_KEY, terminalTheme.value)
  } catch {
    // 存储被禁用时，这次不记住而已。
  }
}

function toggleTerminalTheme() {
  terminalTheme.value = terminalTheme.value === 'dark' ? 'light' : 'dark'
  applyTheme()
}

function write(text: string) {
  term?.write(text)
}

/** xterm 的尺寸变了就同步给远端 PTY，否则 vim/top 的画面会错位。 */
function syncSize() {
  if (!term || !fitAddon) return
  try {
    fitAddon.fit()
  } catch {
    // 容器还没布局出来（比如面板刚被折叠），这一次跳过就行。
    return
  }
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
  }
}

function closeSocket() {
  if (!socket) return
  const current = socket
  socket = null
  current.onmessage = null
  current.onclose = null
  current.onerror = null
  if (current.readyState === WebSocket.OPEN || current.readyState === WebSocket.CONNECTING) {
    current.close()
  }
}

async function connect(serverId: number) {
  const mine = ++generation
  closeSocket()
  // 不在连接前清屏：重连失败时旧画面必须留在屏幕上（那是用户的现场）。
  // 等服务端 hello 帧确认新会话/续接成功，再 reset + 回放。
  closeNote = null
  emit('status', 'connecting')
  write(`\x1b[90m正在连接 #${serverId} …\x1b[0m\r\n`)

  let ticket: string
  try {
    ticket = (await opsApi.ticket()).ticket
  } catch {
    if (mine === generation) {
      write('\x1b[31m获取连接凭证失败\x1b[0m\r\n')
      emit('status', 'closed')
    }
    return
  }
  if (mine !== generation) return

  const url = opsWsUrl('terminal', {
    server_id: serverId,
    ticket,
    cols: term?.cols ?? 80,
    rows: term?.rows ?? 24,
  })
  const ws = new WebSocket(url)
  socket = ws

  ws.onopen = () => {
    if (mine !== generation) return
    emit('status', 'connected')
    syncSize()
  }
  ws.onmessage = (event) => {
    if (mine !== generation || typeof event.data !== 'string') return
    handleFrame(event.data)
  }
  ws.onclose = (event) => {
    if (mine !== generation) return
    socket = null
    // 4001/4002 的具体原因由控制帧先行告知；1008 是鉴权/查不到机器。
    const reason = closeNote || (event.reason ? `：${event.reason}` : '')
    write(`\r\n\x1b[90m连接已断开${reason}\x1b[0m\r\n`)
    emit('status', 'closed', event.code)
  }
  ws.onerror = () => {
    if (mine === generation) write('\r\n\x1b[31m连接出错\x1b[0m\r\n')
  }
}

/** 处理一帧服务端消息：0x1e 打头是控制帧，其余原样写进终端。 */
function handleFrame(data: string) {
  if (!data.startsWith(CONTROL_MARK)) {
    write(data)
    return
  }
  let payload: { type?: string; reason?: string; resume?: boolean }
  try {
    payload = JSON.parse(data.slice(1))
  } catch {
    write(data)
    return
  }
  if (payload.type === 'hello') {
    // 新会话：干净屏幕；续接：reset 后由后续回放帧重建断线前的画面。
    term?.reset()
    pendingLine = ''
    closeNote = null
  } else if (payload.type === 'live') {
    // 回放结束、进入实时——状态由顶栏表达，这里不插提示行（会破坏光标位置）。
  } else if (payload.type === 'ended') {
    closeNote = `：${payload.reason || '远端会话已结束'}`
  } else if (payload.type === 'takenover') {
    closeNote = '：终端已在另一个页面打开'
  }
}

/** 不断开、不清屏地重连一次（由父页面的自动重连/手动重连调用）。 */
function reconnect() {
  if (props.serverId !== null) void connect(props.serverId)
}

function disconnect() {
  generation += 1
  closeSocket()
  emit('status', 'closed', WS_INTENTIONAL)
}

/** 把 AI 执行的命令和结果打进终端，青色 [AI] 前缀跟用户自己的输入区分开。 */
function writeAiExec(command: string, output?: string, exitStatus?: number) {
  if (!term) return
  const suffix =
    exitStatus === undefined || exitStatus === 0
      ? ''
      : `  \x1b[31mexit ${exitStatus}\x1b[0m`
  write(`\r\n\x1b[36m[AI]\x1b[0m \x1b[1m$ ${command}\x1b[0m${suffix}\r\n`)
  if (output) write(output.replace(/\r?\n/g, '\r\n') + '\r\n')
  refreshPrompt()
}

/**
 * AI 的命令走的是独立 exec 通道，不经过 PTY，shell 不会自己重打提示符，
 * 光标会孤零零停在空行上。替它补一个回车，让 shell 把提示符打出来。
 * 用户敲了半行命令、或正在 vim/top 这类全屏程序里时别动——
 * 前者会误提交，后者这个回车会进到程序里。
 */
function refreshPrompt() {
  if (!term || term.buffer.active.type === 'alternate') return
  if (pendingLine) return
  if (socket?.readyState === WebSocket.OPEN) socket.send('\r')
}

/** 把一段命令送到 PTY 的输入（不带回车），用户看完再自己决定跑不跑。 */
function sendInput(text: string) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(text)
    pendingLine += text
  }
}

// ---- 复制 / 粘贴 --------------------------------------------------------------
// xterm 默认对剪贴板毫无支持：右键没反应、Ctrl+Shift+C/V 原样发给 PTY。
// 这里补齐终端圈的三套惯例：右键菜单、Ctrl+Shift 快捷键、选中即复制。

async function copySelection() {
  const text = term?.getSelection() ?? ''
  if (text) {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // 剪贴板权限被拒（非安全上下文/焦点丢失）：没有安全的提示通道，安静失败。
    }
  }
}

async function pasteClipboard() {
  if (!term) return
  try {
    const text = await navigator.clipboard.readText()
    // term.paste 会走 onData：socket 发送与 pendingLine 跟踪都不用重复实现。
    if (text) term.paste(text)
  } catch {
    // 同上。
  }
}

// ---- 右键菜单 -----------------------------------------------------------------
// 一律弹菜单，不再做「右键即粘贴」：静默粘贴会把一整段内容直接打进 PTY，
// 手滑右键就等于执行命令，代价和风险都不该由一次右键承担。
// hasSelection 是 xterm 的内部状态、不是响应式数据，所以置灰判断在打开那一刻取一次。

const menu = reactive({ open: false, x: 0, y: 0 })
const menuItems = ref<DarkMenuItem[]>([])

function openMenu(event: MouseEvent) {
  event.preventDefault()
  // xterm 自己也绑了 contextmenu（把隐藏 textarea 挪到鼠标下、灌入选中文字），
  // 但它不动视觉选区，所以这里的 hasSelection 拿到的还是用户刚框住的那段。
  // 探测当前目录要向 PTY 提交命令：全屏程序里会变成程序输入，有半行未提交
  // 内容会把用户的半截命令一起带走，两种情况都置灰。
  const inAlternate = term?.buffer.active.type === 'alternate'
  const socketClosed = socket?.readyState !== WebSocket.OPEN
  const probeBlocked = socketClosed || inAlternate || !!pendingLine
  const probeHint = socketClosed
    ? '终端还没连上'
    : inAlternate
      ? '正在使用全屏程序，先退出'
      : '当前行有未提交内容，先回车或清空'
  menuItems.value = [
    {
      key: 'copy',
      label: '复制',
      icon: CopyOutlined,
      group: 0,
      disabled: !term?.hasSelection(),
      disabledHint: '没有选中内容',
    },
    {
      key: 'paste',
      label: '粘贴',
      icon: SnippetsOutlined,
      group: 0,
      disabled: socketClosed,
      disabledHint: '终端还没连上',
    },
    { key: 'select-all', label: '全选', icon: SelectOutlined, group: 1 },
    { key: 'clear', label: '清屏', icon: ClearOutlined, group: 1 },
    {
      key: 'open-in-files',
      label: '在文件浏览器打开',
      icon: FolderOpenOutlined,
      group: 2,
      disabled: probeBlocked,
      disabledHint: probeHint,
    },
  ]
  menu.x = event.clientX
  menu.y = event.clientY
  menu.open = true
}

function onMenuPick(key: string) {
  menu.open = false
  if (key === 'copy') void copySelection()
  else if (key === 'paste') void pasteClipboard()
  else if (key === 'select-all') term?.selectAll()
  else if (key === 'clear') term?.clear()
  else if (key === 'open-in-files') requestCwd()
  // 清屏只清本地缓冲，一个字节都不发给 PTY：远端 shell 的状态不受影响。
}

/** 探测 PTY shell 的当前目录。

 * 新开 SSH exec 通道拿到的是全新会话的起始目录，不是这个 shell 的 cwd，所以
 * 只能向 PTY 自己问。命令尾部带的 CSI 序列把刚回显的命令行上移、整行擦除并
 * 删行——屏幕和滚动缓冲里都不留痕；路径通过 OSC 697 回传，由 xterm 的
 * parser 接住。只在右键打开的那一刻校验过状态（已连接 / 主缓冲 / 无半行）。
 */
function requestCwd() {
  if (!term || socket?.readyState !== WebSocket.OPEN) return
  if (term.buffer.active.type === 'alternate' || pendingLine) return

  let done = false
  const osc = term.parser.registerOscHandler(697, (data) => {
    if (!done && data.startsWith('/')) {
      done = true
      emit('browseFiles', data)
    }
    return true
  })
  window.setTimeout(() => osc.dispose(), 3000)

  socket.send("printf '\\e]697;%s\\a\\e[1A\\e[2K\\e[M' \"$PWD\"\r")
}

// 选中即复制（PuTTY 口径）。onSelectionChange 在拖选过程中连续触发，
// 防抖到鼠标停下 150ms 再写，避免一路狂刷剪贴板。
let copyOnSelectTimer: ReturnType<typeof setTimeout> | undefined

// ---- 终端内容搜索 -------------------------------------------------------------
// 只在回滚缓冲里做客户端查找，不发任何请求；大小写不敏感。

const searchOpen = ref(false)
const searchKeyword = ref('')
const searchInputRef = ref<HTMLInputElement | null>(null)
const SEARCH_OPTIONS = { caseSensitive: false }

function onWrapKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'f') {
    e.preventDefault()
    searchOpen.value = true
    void nextTick(() => searchInputRef.value?.focus())
  }
}

function findNext() {
  if (searchKeyword.value) searchAddon?.findNext(searchKeyword.value, SEARCH_OPTIONS)
}

function findPrev() {
  if (searchKeyword.value) searchAddon?.findPrevious(searchKeyword.value, SEARCH_OPTIONS)
}

function closeSearch() {
  searchOpen.value = false
  searchKeyword.value = ''
  searchAddon?.clearDecorations()
  term?.focus()
}

onMounted(() => {
  term = new Terminal({
    fontSize: 13,
    fontFamily: "'IBM Plex Mono', Menlo, Consolas, 'Courier New', monospace",
    cursorBlink: true,
    scrollback: 5000,
    theme: TERMINAL_THEMES[terminalTheme.value],
  })
  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  searchAddon = new SearchAddon()
  term.loadAddon(searchAddon)
  // 输出里的 URL 变成可点链接（默认新窗口打开，与 markdown 链接同一策略）。
  term.loadAddon(new WebLinksAddon())
  term.open(hostEl.value as HTMLElement)

  // 返回 false 就拦下这个按键，xterm 不再发给 PTY。Ctrl+Shift 组合在
  // 终端里是剪贴板保留键；另补 xterm 世界的 Ctrl+Insert / Shift+Insert。
  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== 'keydown') return true
    const key = event.key.toLowerCase()
    if (event.ctrlKey && event.shiftKey && (key === 'c' || key === 'v')) {
      if (key === 'c') void copySelection()
      else void pasteClipboard()
      return false
    }
    if (event.ctrlKey && !event.shiftKey && key === 'insert') {
      void copySelection()
      return false
    }
    if (event.shiftKey && !event.ctrlKey && key === 'insert') {
      void pasteClipboard()
      return false
    }
    return true
  })

  term.element?.addEventListener('contextmenu', openMenu)

  term.onSelectionChange(() => {
    if (!term?.hasSelection()) return
    if (copyOnSelectTimer) clearTimeout(copyOnSelectTimer)
    copyOnSelectTimer = setTimeout(() => void copySelection(), 150)
  })

  term.onData((data) => {
    if (socket?.readyState === WebSocket.OPEN) socket.send(data)
    // 方向键、功能键这类转义序列整体跳过，不影响行缓冲。
    if (data.startsWith('\x1b')) return
    for (const ch of data) {
      // 回车提交、Ctrl+C 打断、Ctrl+U 清行，都意味着行缓冲空了。
      if (ch === '\r' || ch === '\n' || ch === '\x03' || ch === '\x15') pendingLine = ''
      else if (ch === '\x7f' || ch === '\b') pendingLine = pendingLine.slice(0, -1)
      else pendingLine += ch
    }
  })

  observer = new ResizeObserver(() => syncSize())
  if (hostEl.value) observer.observe(hostEl.value)
  nextTick(syncSize)

  if (props.serverId) connect(props.serverId)
})

onBeforeUnmount(() => {
  generation += 1
  closeSocket()
  if (copyOnSelectTimer) clearTimeout(copyOnSelectTimer)
  observer?.disconnect()
  observer = null
  term?.dispose()
  term = null
  fitAddon = null
  searchAddon = null
})

watch(
  () => props.serverId,
  (id) => {
    if (id) connect(id)
    else disconnect()
  },
)

defineExpose({
  focus: () => term?.focus(),
  fit: syncSize,
  reconnect,
  writeAiExec,
  sendInput,
  terminalTheme,
  toggleTerminalTheme,
})
</script>

<style scoped>
/* padding 放在 wrap 而不是 host：FitAddon 用 host（term.element 的父元素）的
   完整高度减 .xterm 自身的 padding 来算行数，host 若带 padding，行数就会多算
   出 16px 的余量，最后一行在 wrap 的 overflow 边界被切——切不切取决于窗口
   高度对行高的余数，所以表现为有的分辨率切、有的不切。host 保持无 padding，
   fit 量到的才是 .xterm 真实可用的高度。 */
.terminal-wrap {
  box-sizing: border-box;
  position: relative;
  height: 100%;
  min-height: 0;
  padding: 8px;
  background: var(--ink);
  border-radius: var(--tool-radius);
  overflow: hidden;
}

.terminal-wrap.terminal-light {
  background: #e8e8e8;
}

.terminal-host {
  box-sizing: border-box;
  height: 100%;
}

/* Ctrl+F 搜索条：压在终端右上角，深色与终端同族。 */
.terminal-search {
  position: absolute;
  top: 14px;
  right: 18px;
  z-index: var(--z-header);
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 4px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: var(--tool-radius);
  background: var(--surface-dark);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}

.search-input {
  width: 180px;
  padding: 4px 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: #0d141b;
  color: var(--text-dark);
  font-size: var(--fs-xs);
  outline: none;
}

.search-input:focus {
  outline: 1px solid rgba(58, 214, 222, 0.45);
}

.search-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-dark-2);
  font-size: var(--fs-xs);
  cursor: pointer;
}

.search-btn:hover {
  background: var(--on-dark-hover);
  color: var(--text-dark);
}

.terminal-mask {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8c8c8c;
  font-size: var(--fs-sm);
  background: var(--ink);
}

/* xterm 默认滚动条在白底主题里设计，黑底上白得刺眼：压成一道暗色细条，
   悬停时才稍微明显一点。 */
.terminal-wrap :deep(.xterm-viewport)::-webkit-scrollbar {
  width: 8px;
}

.terminal-wrap :deep(.xterm-viewport)::-webkit-scrollbar-track {
  background: transparent;
}

.terminal-wrap :deep(.xterm-viewport)::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: var(--radius-sm);
}

.terminal-wrap :deep(.xterm-viewport)::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.2);
}

.terminal-light :deep(.xterm-viewport)::-webkit-scrollbar-thumb {
  background: rgba(0, 0, 0, 0.15);
}

.terminal-light :deep(.xterm-viewport)::-webkit-scrollbar-thumb:hover {
  background: rgba(0, 0, 0, 0.25);
}
</style>
