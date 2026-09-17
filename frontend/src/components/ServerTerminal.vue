<template>
  <div class="terminal-wrap" @keydown="onWrapKeydown">
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
  </div>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { SearchAddon } from 'xterm-addon-search'
import { WebLinksAddon } from 'xterm-addon-web-links'
import 'xterm/css/xterm.css'
import { ArrowDownOutlined, ArrowUpOutlined, CloseOutlined } from '@ant-design/icons-vue'
import { opsApi, opsWsUrl } from '@/api'

const props = defineProps<{ serverId: number | null }>()
const emit = defineEmits<{
  // closed 时带上 WebSocket 断开码：父组件据此区分「服务端主动拒绝」
  // （1008 鉴权/查无此机，自动重连没意义）和网络层断开（值得自动重连）。
  (e: 'status', value: 'connecting' | 'connected' | 'closed', code?: number): void
}>()

const hostEl = ref<HTMLElement | null>(null)

let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let searchAddon: SearchAddon | null = null
let socket: WebSocket | null = null
let observer: ResizeObserver | null = null
// 每次连接自增。异步拿票的过程中用户可能已经切走了，回来时靠它判断这张票是否过期。
let generation = 0
// 用户已键入但还没回车的内容。AI 补提示符时要避开它，免得把半条命令提交上去。
let pendingLine = ''

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
  term?.reset()
  pendingLine = ''
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
    if (typeof event.data === 'string') write(event.data)
  }
  ws.onclose = (event) => {
    if (mine !== generation) return
    socket = null
    // 1008 是鉴权/查不到机器，服务端会把原因塞在 reason 里。
    const reason = event.reason ? `：${event.reason}` : ''
    write(`\r\n\x1b[90m连接已断开${reason}\x1b[0m\r\n`)
    emit('status', 'closed', event.code)
  }
  ws.onerror = () => {
    if (mine === generation) write('\r\n\x1b[31m连接出错\x1b[0m\r\n')
  }
}

function disconnect() {
  generation += 1
  closeSocket()
  emit('status', 'closed')
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
    // 与全局 --ink / --signal-bright 同色系；xterm 主题在 JS 里，用不了 CSS 变量。
    theme: {
      background: '#11181f',
      foreground: '#d8dee4',
      cursor: '#3ad6de',
      selectionBackground: 'rgba(58, 214, 222, 0.25)',
    },
  })
  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  searchAddon = new SearchAddon()
  term.loadAddon(searchAddon)
  // 输出里的 URL 变成可点链接（默认新窗口打开，与 markdown 链接同一策略）。
  term.loadAddon(new WebLinksAddon())
  term.open(hostEl.value as HTMLElement)
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

defineExpose({ focus: () => term?.focus(), fit: syncSize, writeAiExec, sendInput })
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
  background: #11181f;
  border-radius: 6px;
  overflow: hidden;
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
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 4px;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 6px;
  background: #1b2530;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
}

.search-input {
  width: 180px;
  padding: 4px 8px;
  border: none;
  border-radius: 4px;
  background: #0d141b;
  color: #d8dee4;
  font-size: 12px;
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
  border-radius: 4px;
  background: transparent;
  color: #8b98a5;
  font-size: 12px;
  cursor: pointer;
}

.search-btn:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #d8dee4;
}

.terminal-mask {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #8c8c8c;
  font-size: 13px;
  background: #11181f;
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
  border-radius: 4px;
}

.terminal-wrap :deep(.xterm-viewport)::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.2);
}
</style>
