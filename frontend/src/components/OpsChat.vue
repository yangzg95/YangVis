<template>
  <div :class="['ops-chat', { dark }]">
    <!-- 工具条：历史会话下拉 + 新会话。面板每次打开都是全新窗口，旧排查从这里翻回去。 -->
    <div class="chat-toolbar">
      <a-dropdown
        :trigger="['click']"
        :overlay-class-name="dark ? 'ops-history-overlay dark' : 'ops-history-overlay'"
        @open-change="onHistoryOpen"
      >
        <button type="button" class="tool-btn" :disabled="!targetId">
          <HistoryOutlined /> 历史
        </button>
        <template #overlay>
          <a-menu :selected-keys="conversationId ? [conversationId] : []">
            <a-menu-item v-for="conv in history" :key="conv.id" @click="openConversation(conv.id)">
              <div class="history-item">
                <div class="history-line">
                  <span class="history-title">{{ conv.title }}</span>
                  <!-- stop：不触发菜单项的「打开会话」，也不让下拉收起来。 -->
                  <a-tooltip title="重命名这段会话">
                    <EditOutlined class="history-rename" @click.stop="startRename(conv)" />
                  </a-tooltip>
                  <a-tooltip title="删除这段会话">
                    <DeleteOutlined class="history-del" @click.stop="removeConversation(conv)" />
                  </a-tooltip>
                </div>
                <span class="history-meta">
                  {{ formatTime(conv.updated_at) }} · {{ conv.message_count }} 条
                </span>
              </div>
            </a-menu-item>
            <a-menu-item v-if="!history.length" key="empty" disabled>
              还没有历史会话
            </a-menu-item>
          </a-menu>
        </template>
      </a-dropdown>
      <a-tooltip title="清空当前窗口，另起一段排查">
        <button
          type="button"
          class="tool-btn"
          :disabled="!targetId || isFresh || thinking"
          @click="newChat"
        >
          <PlusOutlined /> 新会话
        </button>
      </a-tooltip>
    </div>

    <div ref="scrollEl" class="chat-body" @click="onBodyClick" @scroll="onBodyScroll">
      <div v-if="!targetId" class="chat-empty">
        {{ emptyHint }}
      </div>
      <div v-else-if="!items.length" class="chat-empty">
        <RobotOutlined class="empty-icon" />
        <div>问点什么，点下面的例子可以直接填进输入框</div>
        <div v-if="samples.length" class="samples">
          <button
            v-for="text in samples"
            :key="text"
            type="button"
            class="sample"
            @click="useSample(text)"
          >
            {{ text }}
          </button>
        </div>
      </div>

      <div v-for="item in items" :key="item.key" :class="['msg', item.kind]">
        <div class="bubble">
          <!-- 助手消息按 chunks 顺序渲染：步骤与文字交替出现；步骤过多时折叠。 -->
          <template v-if="item.kind === 'assistant'">
            <div class="chunk-flow">
              <template
                v-for="chunk in displayChunks(item)"
                :key="chunk.summary ? `sum-${chunk.index}` : `chk-${chunk.index}`"
              >
                <button
                  v-if="chunk.summary"
                  type="button"
                  class="step-line step-summary"
                  @click="toggleSteps(chunk.runKey)"
                >
                  <span class="step-icon">{{ chunk.type === 'reason' ? '✦' : '⚙' }}</span>
                  <span class="step-text">
                    <template v-if="chunk.type === 'reason'">
                      {{ chunk.expanded ? '收起思考过程' : '思考过程 · 点击展开' }}
                    </template>
                    <template v-else>
                      {{ chunk.expanded ? `收起 ${chunk.runCount} 步` : `已执行 ${chunk.runCount} 步 · 点击展开` }}
                    </template>
                  </span>
                </button>
                <div v-else-if="chunk.type === 'step'" class="step-line">
                  <span class="step-icon">⚙</span>
                  <span class="step-text">{{ chunk.detail }}</span>
                </div>
                <div
                  v-else-if="chunk.type === 'reason' && chunk.detail"
                  class="content reason-line"
                  v-text="chunk.detail"
                />
                <div
                  v-else-if="chunk.type === 'text' && chunk.detail"
                  class="content markdown"
                  v-html="renderCached(`${item.key}-${chunk.index}`, chunk.detail)"
                />
              </template>
            </div>
            <!-- 兜底：如果 chunks 还没数据但 text 有内容（历史回放），整段渲染。 -->
            <div
              v-if="!item.chunks.length && item.text"
              class="content markdown"
              v-html="renderCached(item.key, item.text)"
            />
          </template>
          <div v-else class="content" v-text="item.text" />
          <!-- 写命令确认卡片：挂在触发提议的那条助手气泡下面。 -->
          <ConfirmCard
            v-for="act in actionsOf(item)"
            :key="act.id"
            :action="act"
            :acting="isActing(act)"
            :dark="dark"
            :can-approve="auth.canOpsWrite"
            @approve="approveAction"
            @reject="rejectAction"
          />
          <!-- 复制原始 markdown 文本，贴到别处还能用。流式途中不渲染：那会儿
               正文还在长，孤零零一个按钮挂在气泡底下像个空框。 -->
          <div v-if="item.kind === 'assistant' && item.text && !isStreaming(item)" class="bubble-actions">
            <a-tooltip title="复制">
              <button type="button" class="copy-btn" @click="copyItem(item)">
                <CopyOutlined />
              </button>
            </a-tooltip>
          </div>
        </div>
      </div>

      <!-- 进度指示：一行小字加三个跳动点。刻意不做成带底色的圆角块——它只是
           「AI 还在干活」的旁白，占一整行反而和真正的回答抢视线。 -->
      <div v-if="thinking" class="status-line">
        <span class="pulse"><i /><i /><i /></span>
        <span>{{ aiStatusText }}</span>
      </div>

      <!-- 上翻查看历史时出现：sticky 钉在底部，点击恢复贴底跟随。 -->
      <transition name="jump-fade">
        <button
          v-if="!stickToBottom"
          type="button"
          class="jump-bottom"
          :class="{ dark }"
          title="回到底部"
          @click="pinToBottom"
        >
          <VerticalAlignBottomOutlined />
        </button>
      </transition>
    </div>

    <div class="chat-composer">
      <a-select
        v-model:value="activeModelConfigId"
        class="composer-model"
        size="small"
        :options="modelOptions"
        :disabled="!targetId || !modelOptions.length"
        :placeholder="modelOptions.length ? '选择对话模型' : '暂无可用对话模型'"
      />
      <a-textarea
        ref="inputEl"
        v-model:value="draft"
        :rows="2"
        :disabled="!targetId"
        :placeholder="targetId ? placeholder : emptyHint"
        @keydown.enter.exact="onComposerEnter"
      />
      <!-- 发送键压在输入框右下角，省掉一行只放按钮的横条。 -->
      <div class="composer-send">
        <a-button v-if="thinking" size="small" danger @click="stop">停止</a-button>
        <a-tooltip v-else title="Enter 发送，Shift + Enter 换行">
          <a-button
            type="primary"
            shape="circle"
            size="small"
            :disabled="!targetId || !draft.trim()"
            @click="send"
          >
            <ArrowUpOutlined />
          </a-button>
        </a-tooltip>
      </div>
    </div>

    <!-- 会话重命名：从历史列表里的编辑图标唤起。 -->
    <a-modal
      v-model:open="renameOpen"
      title="重命名会话"
      :confirm-loading="renaming"
      ok-text="保存"
      cancel-text="取消"
      @ok="saveRename"
    >
      <a-input
        v-model:value="renameText"
        placeholder="给这段会话起个名字"
        :maxlength="80"
        @press-enter="saveRename"
      />
    </a-modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { message as toast, Modal } from 'ant-design-vue'
import {
  RobotOutlined,
  ArrowUpOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  HistoryOutlined,
  PlusOutlined,
  VerticalAlignBottomOutlined,
} from '@ant-design/icons-vue'
import ConfirmCard from '@/components/ConfirmCard.vue'
import { useAuthStore } from '@/stores/auth'
import { createMarkdown } from '@/utils/markdown'
import { useOpsConfirm, actionFromConfirm } from '@/utils/useOpsConfirm'
import { useStreamRender } from '@/utils/streamRender'
import { copyText } from '@/utils/clipboard'
import { storageKeys } from '@/utils/storage'
import {
  chatApi,
  settingsApi,
  type Conversation,
  type ModelConfig,
  type OpsAction,
  type StreamHandlers,
} from '@/api'

const props = defineProps<{
  target: 'server' | 'database'
  targetId: number | null
  placeholder?: string
  emptyHint?: string
  /** 空态里可点的示例问题，点一下填进输入框（不直接发出去，留给用户改）。 */
  samples?: string[]
  /** 深色变体：嵌在深色页里（全屏终端页）时使用，默认浅色。 */
  dark?: boolean
}>()

const auth = useAuthStore()

const emit = defineEmits<{
  /** 数据库问答里点 SQL 代码块上的按钮：把这段 SQL 拿到查询页签里。 */
  (e: 'open-query', sql: string): void
  /** 服务器问答里点命令代码块上的按钮：把这段命令送到终端（不自动回车）。 */
  (e: 'send-command', command: string): void
  /** AI 实际执行了一条命令：服务器问答时父组件把它打到终端窗口里展示。 */
  (e: 'exec', payload: { command: string; output?: string; exit_status?: number }): void
}>()

const placeholder = props.placeholder ?? '问点什么，比如「磁盘为什么满了」'
const emptyHint = props.emptyHint ?? '先在左侧选中一个目标'
const samples = props.samples ?? []

// markdown-it 基座与主对话共用（html: false 安全底线 + 外链新标签页），
// 见 utils/markdown.ts；本组件在其上叠加代码块动作按钮（下面的 fence 钩子）。
const md = createMarkdown()

// 代码块动作按钮：数据库问答的 SQL 块带「在查询框中打开」，服务器问答的
// shell 块带「在终端中打开」。按钮不内嵌内容（属性转义又长又脆），渲染时
// 存进 map，点击时按 key 取回。
let snippetSeq = 0
const snippetBlocks = new Map<string, string>()

const SHELL_LANGS = new Set(['bash', 'sh', 'shell', 'console'])

const defaultFence =
  md.renderer.rules.fence ||
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.fence = (tokens, idx, options, env, self) => {
  const rendered = defaultFence(tokens, idx, options, env, self)
  const lang = tokens[idx].info.trim().toLowerCase()
  let kind: 'query' | 'terminal' | null = null
  if (props.target === 'database' && (lang === 'sql' || lang === 'mysql')) kind = 'query'
  if (props.target === 'server' && SHELL_LANGS.has(lang)) kind = 'terminal'
  if (!kind) return rendered
  const key = `s${++snippetSeq}`
  snippetBlocks.set(key, tokens[idx].content)
  const label = kind === 'query' ? '在查询框中打开' : '在终端中打开'
  return `<div class="sql-block">${rendered}<button type="button" class="snippet-open" data-key="${key}" data-kind="${kind}">${label}</button></div>`
}

/** 代码块按钮走事件代理：v-html 每次流式更新都重渲染，直接绑监听绑不上。 */
function onBodyClick(event: MouseEvent) {
  const btn = (event.target as HTMLElement).closest('.snippet-open') as HTMLElement | null
  if (!btn) return
  const text = snippetBlocks.get(btn.dataset.key || '')
  if (!text) return
  if (btn.dataset.kind === 'terminal') emit('send-command', text.trim())
  else emit('open-query', text.trim())
}

// 流式渲染走「缓存 + 节拍器」：历史消息零重解析，流式中的那条按 ~100ms 节拍
// 重渲染，不再每 token 全量解析（详见 streamRender.ts 头注）。缓存也让 fence
// 钩子生成的代码块按钮 key 稳定，不会因重渲染无限累积。
const { schedule, beginStream, endStream, renderCached, clearCache } = useStreamRender((text) =>
  md.render(text || ''),
)

/** 一条助手回复由若干 chunk 按到达顺序拼接：步骤、文字、思考交替。 */
interface TalkChunk {
  type: 'step' | 'text' | 'reason'
  /** step 时是命令摘要，text 时是这段文字，reason 时是这段推理。 */
  detail: string
}

interface TalkItem {
  key: string
  /** 落库消息 id：确认卡片按它对位到触发提议的气泡下面。 */
  id?: number
  kind: 'user' | 'assistant'
  /** 完整文本（用于复制、历史回放兜底渲染）。 */
  text: string
  /** 按到达顺序排列的步骤与文字片段，用于交错渲染。 */
  chunks: TalkChunk[]
}

const items = ref<TalkItem[]>([])
const draft = ref('')

// ---- 步骤折叠 ---------------------------------------------------------------
// 一条回答里可能有多波「执行命令」，每波后面紧跟一段解说文字。折叠按波就地收：
// 只要有 ≥2 条连续命令就默认收成一行「已执行 N 步」，点开看完整列表；单条命令
// 不折叠（折叠它省不了空间，还把命令藏起来）。文字块始终保留，阅读流不受影响。
const STEP_COLLAPSE_THRESHOLD = 1
const expandedSteps = ref<Set<string>>(new Set())

function toggleSteps(key: string) {
  const set = expandedSteps.value
  if (set.has(key)) set.delete(key)
  else set.add(key)
}

interface RenderChunk extends TalkChunk {
  index: number
  summary: boolean
  /** 本组连续步骤的条数（摘要行文案用）。 */
  runCount: number
  /** 本组折叠状态的键：一条回答里多组命令各自独立展开/收起。 */
  runKey: string
  expanded: boolean
}

function displayChunks(item: TalkItem): RenderChunk[] {
  const chunks = item.chunks
  const out: RenderChunk[] = []
  let i = 0
  let runIndex = 0
  while (i < chunks.length) {
    const chunk = chunks[i]
    if (chunk.type === 'text') {
      out.push({ ...chunk, index: i, summary: false, runCount: 0, runKey: '', expanded: false })
      i += 1
      continue
    }
    // 收一段「同类型的连续 chunk」为一组：一组命令，或一段思考。文字块把组隔开，
    // 所以每组对应模型的一轮动作，后面紧跟的解说文字留在原位——折叠必须按组就地
    // 收，若把整条回答的步骤全堆到顶部，各组命令就和它们各自的解说被拆散了。
    const runType = chunk.type // 'step' | 'reason'
    let j = i
    while (j < chunks.length && chunks[j].type === runType) j += 1
    const runCount = j - i
    const runKey = `${item.key}:${runType}:${runIndex}`
    // 命令：≥2 条才折叠（单条命令藏起来省不了空间）；思考：整段可折叠。
    const collapsible = runType === 'step' ? runCount > STEP_COLLAPSE_THRESHOLD : true
    const inSet = expandedSteps.value.has(runKey)
    // 两组默认都是收起：命令组藏起来省地方，思考组动辄上千行，摊开会把真正的
    // 结论挤出视口——一轮回答里出现三五段思考是常态，全展开就没法读了。
    const expanded = inSet
    if (collapsible && !expanded) {
      // 折叠态：整组收成一行摘要，就地待在这组原本的位置。
      out.push({ ...chunk, index: i, summary: true, runCount, runKey, expanded })
    } else {
      // 展开态（或本组不折叠）：可折叠时先给一行「收起」摘要，再逐条列出。
      if (collapsible) {
        out.push({ ...chunk, index: i, summary: true, runCount, runKey, expanded })
      }
      for (let k = i; k < j; k += 1) {
        out.push({ ...chunks[k], index: k, summary: false, runCount, runKey, expanded })
      }
    }
    runIndex += 1
    i = j
  }
  return out
}

// ---- 会话与写命令确认 ---------------------------------------------------------
// 传输层是主对话那套 SSE（/chat/completions + /chat/actions/{id}/confirm），
// 会话与待确认项全在库里：刷新/重开页面，拉一遍 messages+actions 即恢复，
// 不再有 WS 时代的「刷新即丢」。
const conversationId = ref<number | null>(null)
// 会话绑定的内置智能体（server-ops/db-ops）：提问时要点名，不然后端退回
// 默认智能体，工具箱和人设都不对。
const convAgentId = ref<number | null>(null)
const actions = ref<OpsAction[]>([])
const { isActing, approve, reject } = useOpsConfirm()

// 正在流式的那条助手气泡：confirm 事件到达时 message_id 还没回填，这一段
// 时间卡片挂到它下面（与主对话 actionsOf 同一约定）。
const streamingAnswerKey = ref<string | null>(null)

function actionsOf(item: TalkItem): OpsAction[] {
  return actions.value.filter((a) =>
    a.message_id != null ? a.message_id === item.id : item.key === streamingAnswerKey.value,
  )
}

/** 这条气泡是不是正在流的那条（决定气泡操作行要不要出现）。 */
function isStreaming(item: TalkItem): boolean {
  return thinking.value && item.key === streamingAnswerKey.value
}
const thinking = ref(false)
const streamingText = ref(false)
/** 当前正在执行的步骤摘要（step 事件到达时写入，下一个 token 到达时清空）。 */
const currentStepDetail = ref('')
const scrollEl = ref<HTMLElement | null>(null)
const inputEl = ref<{ focus: () => void } | null>(null)

/** 持久状态指示文案：区分 AI 在思考、执行命令、还是空闲。 */
const aiStatusText = computed(() => {
  if (currentStepDetail.value) return `正在执行：${currentStepDetail.value}`
  if (streamingText.value) return '正在生成回答…'
  return '正在思考…'
})

// ---- 贴底滚动 ---------------------------------------------------------------
// 与 Chat.vue 同一口径：上翻时流式事件不抢视图，原本贴底才往下跟。
const STICK_THRESHOLD = 40
const stickToBottom = ref(true)

function onBodyScroll() {
  const el = scrollEl.value
  if (el) {
    stickToBottom.value =
      el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = scrollEl.value
    if (el && stickToBottom.value) el.scrollTop = el.scrollHeight
  })
}

function pinToBottom() {
  stickToBottom.value = true
  scrollToBottom()
}

// ---- 模型选择 ---------------------------------------------------------------
// 只把测通过的模型放进下拉；选择存 localStorage，纯查看者偏好，丢了就用默认。

const MODEL_PREF_KEY = storageKeys.opsChatModel

const chatModels = ref<ModelConfig[]>([])
const activeModelConfigId = ref<number | undefined>(undefined)

const modelOptions = computed(() =>
  chatModels.value.filter((m) => m.last_test_ok).map((m) => ({ value: m.id, label: m.title })),
)

function readModelPref(): number | undefined {
  try {
    const value = Number(localStorage.getItem(MODEL_PREF_KEY))
    return Number.isInteger(value) && value > 0 ? value : undefined
  } catch {
    return undefined
  }
}

watch(activeModelConfigId, (id) => {
  try {
    if (id) localStorage.setItem(MODEL_PREF_KEY, String(id))
    else localStorage.removeItem(MODEL_PREF_KEY)
  } catch {
    // 存储被禁用时，这次不记住而已。
  }
})

onMounted(async () => {
  try {
    const data = await settingsApi.listModels('chat')
    chatModels.value = data.items
    const usable = modelOptions.value.map((o) => o.value)
    const preferred = readModelPref()
    const fallback =
      chatModels.value.find((m) => m.last_test_ok && m.is_default)?.id ?? usable[0]
    activeModelConfigId.value =
      preferred && usable.includes(preferred) ? preferred : fallback
  } catch {
    // 拉不到列表就保持空：发送时后端会报「尚未配置可用的对话模型」，原因更准。
    chatModels.value = []
  }
})

function useSample(text: string) {
  draft.value = text
  inputEl.value?.focus()
}

let seq = 0
const nextKey = () => `i${++seq}`

function currentAnswer(): TalkItem {
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'assistant') return last
  const created: TalkItem = { key: nextKey(), kind: 'assistant', text: '', chunks: [] }
  items.value.push(created)
  return created
}

// ---- 会话与历史 ---------------------------------------------------------------
// 面板每次打开（或点「新会话」）都是全新窗口：会话懒创建，第一条提问发出去
// 时才落库，免得历史列表堆满一句话都没说的空会话。旧排查从历史下拉翻回去，
// 消息与待确认项一起回放。loadSeq 管竞态：快速切换目标/会话时，慢响应不得
// 覆盖新选择。
const history = ref<Conversation[]>([])

let loadSeq = 0
let abort: (() => void) | null = null

/** 当前是不是一段还没开口的新窗口（此时「新会话」按钮没有意义）。 */
const isFresh = computed(() => conversationId.value === null && !items.value.length)

function resetView() {
  conversationId.value = null
  convAgentId.value = null
  items.value = []
  actions.value = []
  snippetBlocks.clear()
  expandedSteps.value = new Set()
  thinking.value = false
  streamingText.value = false
  streamingAnswerKey.value = null
  currentStepDetail.value = ''
  endStream()
  clearCache()
}

/** 拉目标的历史会话列表。mine 是调用方手里的 loadSeq 快照。 */
async function refreshHistory(mine: number) {
  if (!props.targetId) {
    history.value = []
    return
  }
  try {
    const data = await chatApi.listOpsConversations(props.target, props.targetId)
    if (mine !== loadSeq) return
    history.value = data.items
  } catch {
    // 拦截器已提示；历史拉不到不挡着聊天。
  }
}

/** 换目标：窗口回到全新状态，只把该目标的历史列表装进来。 */
async function loadTarget() {
  const mine = ++loadSeq
  abort?.()
  abort = null
  resetView()
  history.value = []
  await refreshHistory(mine)
}

/** 从历史里翻回一段旧排查：消息与待确认项一起回放。 */
async function openConversation(id: number) {
  if (thinking.value) {
    // 回答流还挂着气泡机制，切走会把流式状态搞乱。
    toast.warning('请等当前回答结束后再切换会话')
    return
  }
  if (id === conversationId.value) return
  const mine = ++loadSeq
  abort?.()
  abort = null
  resetView()
  try {
    const [msgs, acts] = await Promise.all([
      chatApi.listMessages(id),
      chatApi.listActions(id),
    ])
    if (mine !== loadSeq) return
    conversationId.value = id
    convAgentId.value = history.value.find((c) => c.id === id)?.agent_id ?? null
    items.value = msgs.items.map((m) => ({
      key: `m${m.id}`,
      id: m.id,
      kind: m.role,
      text: m.content,
      // 后端按到达顺序存了整条回答流（含命令与思考），据此重建交错渲染；
      // 老消息没有 timeline，退回空 chunks → 模板用 content 兜底整段渲染。
      chunks: (m.timeline ?? []).map((e) =>
        e.type === 'step'
          ? { type: 'step', detail: describeStep({ tool: e.tool ?? '', query: e.query, input: e.input }) }
          : { type: e.type, detail: e.detail ?? '' },
      ),
    }))
    actions.value = acts.items
    pinToBottom()
  } catch {
    // 拦截器已提示；窗口保持全新状态，接着提问不受影响。
  }
}

/** 清空窗口另起一段：会话本身不删，它留在历史列表里。 */
function newChat() {
  if (isFresh.value || thinking.value) return
  loadSeq += 1
  abort?.()
  abort = null
  resetView()
}

/**
 * 删掉一段历史会话。用 Modal 而不是 popconfirm：下拉菜单里的确认气泡会
 * 被下拉的「点击外部收起」一起带走，Modal 独立于这层 DOM，没这个问题。
 */
function removeConversation(conv: Conversation) {
  if (thinking.value && conversationId.value === conv.id) {
    toast.warning('这段会话正在生成回答，结束后再删')
    return
  }
  Modal.confirm({
    title: '删除这段会话？',
    content: `「${conv.title}」的 ${conv.message_count} 条消息会一起删掉，不可恢复。`,
    okText: '删除',
    okButtonProps: { danger: true },
    cancelText: '取消',
    onOk: async () => {
      try {
        await chatApi.deleteConversation(conv.id)
      } catch {
        return // 拦截器已提示
      }
      if (conversationId.value === conv.id) {
        // 删的正是打开的这段：窗口回到全新状态。
        loadSeq += 1
        abort?.()
        abort = null
        resetView()
      }
      toast.success('已删除')
      await refreshHistory(loadSeq)
    },
  })
}

// ---- 会话重命名 ---------------------------------------------------------------
// 标题原本只从第一个问题自动截断而来，没有改名入口。这里在历史列表每行加了编辑
// 图标，点开弹一个输入框，保存即调 updateConversation 落库、就地更新列表标题。
const renameOpen = ref(false)
const renaming = ref(false)
const renameText = ref('')
const renameTarget = ref<Conversation | null>(null)

function startRename(conv: Conversation) {
  renameTarget.value = conv
  renameText.value = conv.title
  renameOpen.value = true
}

async function saveRename() {
  const conv = renameTarget.value
  const title = renameText.value.trim()
  if (!conv) return
  if (!title) {
    toast.warning('名称不能为空')
    return
  }
  renameOpen.value = false
  if (title === conv.title) return
  renaming.value = true
  try {
    await chatApi.updateConversation(conv.id, { title })
    const row = history.value.find((c) => c.id === conv.id)
    if (row) row.title = title
    toast.success('已重命名')
  } catch {
    // 拦截器已提示；标题保持原样。
  } finally {
    renaming.value = false
  }
}

/** 下拉展开时刷新一次历史：别的标签页聊过的也能看到。 */
function onHistoryOpen(open: boolean) {
  if (open) void refreshHistory(loadSeq)
}

/** 会话标题与主对话同一规则：第一个问题，压平空白后截 40 字。 */
function deriveTitle(question: string): string {
  const title = question.split(/\s+/).join(' ')
  return title.length <= 40 ? title : `${title.slice(0, 40)}…`
}

/** 历史下拉里的一行时间：MM-DD HH:mm，够认就行。 */
function formatTime(value: string): string {
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// ---- SSE 事件处理 -------------------------------------------------------------
// completions 与 confirm 两条流共用同一组回调：step/token/confirm/done/error
// 语义一致；exec 只有 confirm 流会发。
function streamHandlers(streamActions: OpsAction[]): StreamHandlers {
  // 取当前回答气泡（没有就建），并记住它是本轮流式的挂载点。
  const answer = () => {
    const item = currentAnswer()
    streamingAnswerKey.value = item.key
    return item
  }
  // 当前文字 / 思考 chunk 的引用：同类增量追加到它，异类或步骤到来时关闭它。
  let textChunk: TalkChunk | null = null
  let reasonChunk: TalkChunk | null = null
  const ensureTextChunk = (item: TalkItem) => {
    if (!textChunk || textChunk.type !== 'text') {
      textChunk = { type: 'text', detail: '' }
      reasonChunk = null // 开始正文，结束思考段
      item.chunks.push(textChunk)
    }
    return textChunk
  }
  const ensureReasonChunk = (item: TalkItem) => {
    if (!reasonChunk || reasonChunk.type !== 'reason') {
      reasonChunk = { type: 'reason', detail: '' }
      textChunk = null // 开始思考，结束正文段
      item.chunks.push(reasonChunk)
    }
    return reasonChunk
  }
  const closeTextChunk = () => {
    textChunk = null
    reasonChunk = null
  }
  return {
    onStep: (data) => {
      const item = answer()
      closeTextChunk()
      const detail = describeStep(data)
      currentStepDetail.value = detail
      item.chunks.push({ type: 'step', detail })
      scrollToBottom()
    },
    onReason: (token) => {
      const item = answer()
      // 思考走 v-text 直接绑定 chunk.detail，改一处即重渲染，不必进 streamRender
      // 的按拍缓存（那套只管 markdown 块）。
      ensureReasonChunk(item).detail += token
      streamingText.value = false // 还在推理阶段，正文尚未开始
      scrollToBottom()
    },
    onToken: (token) => {
      const item = answer()
      item.text += token
      const tc = ensureTextChunk(item)
      tc.detail += token
      // 收到第一个 token 后，步骤已执行完，清空步骤状态。
      if (currentStepDetail.value && streamingText.value) {
        currentStepDetail.value = ''
      }
      streamingText.value = true
      // 节流 key 必须与模板 renderCached 的 key 一致（活跃文字块永远是最后一块），
      // 否则 streamRender 的「同 tick 回旧 HTML」判断对不上，每个 token 都全量重解析。
      beginStream(`${item.key}-${item.chunks.length - 1}`)
      schedule()
      scrollToBottom()
    },
    onConfirm: (data) => {
      // 模型提议后可能直接结束本轮、一个 token 都不吐：先保证气泡存在，
      // 确认卡片才有地方挂。
      answer()
      const action = actionFromConfirm(data, conversationId.value)
      actions.value.push(action)
      streamActions.push(action)
      scrollToBottom()
    },
    onExec: (ev) => {
      // 确认流的 exec：useOpsConfirm 已把结果写回卡片，这里补步骤标签。
      // 只读命令（action_id 为空）在 step 里已经亮过命令，不再重复打标签。
      if (ev.action_id != null) {
        const item = answer()
        closeTextChunk()
        item.chunks.push({
          type: 'step',
          detail: `已执行：${ev.command}（exit ${ev.exit_status ?? '—'}）`,
        })
      }
      // 两种 exec 都通知父组件——服务器问答时父组件把命令和输出打到终端窗口。
      emit('exec', {
        command: ev.command,
        output: ev.output,
        exit_status: ev.exit_status ?? undefined,
      })
      scrollToBottom()
    },
    onDone: ({ message_id }) => {
      // 记下落库 id 并回填本轮提议的 message_id：卡片从「流式气泡」改挂到
      // 落库消息上，与历史回放的定位方式一致。
      const item = items.value[items.value.length - 1]
      if (item && item.kind === 'assistant') item.id = message_id
      for (const a of streamActions) a.message_id = message_id
      finishStream()
      // 本轮问答落库了：历史列表里这条会话的条数/活跃时间变了，顺手刷新。
      void refreshHistory(loadSeq)
    },
    onError: ({ message }) => {
      toast.error(message)
      finishStream()
    },
  }
}

function finishStream() {
  thinking.value = false
  streamingText.value = false
  streamingAnswerKey.value = null
  currentStepDetail.value = ''
  abort = null
  endStream()
}

async function copyItem(item: TalkItem) {
  if (await copyText(item.text)) toast.success('已复制')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

function describeStep(payload: {
  tool: string
  query?: string
  input?: Record<string, unknown>
}): string {
  const input = payload.input ?? {}
  const detail = input.command || input.statement || input.table || payload.query || ''
  return detail ? `${payload.tool}: ${detail}` : payload.tool
}

function onComposerEnter(e: KeyboardEvent) {
  // 中文输入法里按 Enter 是选词、不是发送：这种 keydown 的 isComposing 为 true。
  // 不拦的话消息会被提前发出去，随后输入法把整段文字回填进输入框。
  if (e.isComposing) return
  e.preventDefault()
  void send()
}

async function send() {
  const question = draft.value.trim()
  if (!question) return
  draft.value = ''
  await ask(question)
}

/** 发一条提问。暴露给外部（工作台「询问 AI」）直接带问题进来。 */
async function ask(question: string) {
  if (!question.trim() || !props.targetId || thinking.value) return
  let convId = conversationId.value
  if (!convId) {
    // 懒创建：面板打开是全新窗口，第一条提问发出去时才会话才落库。
    try {
      const conv = await chatApi.createOpsConversation(
        props.target,
        props.targetId,
        deriveTitle(question),
      )
      conversationId.value = conv.id
      convAgentId.value = conv.agent_id
      convId = conv.id
    } catch {
      return // 拦截器已提示（目标被删、模型不可用等）
    }
  }

  items.value.push({ key: nextKey(), kind: 'user', text: question, chunks: [] })
  thinking.value = true
  streamingText.value = false
  pinToBottom()

  const streamActions: OpsAction[] = []
  abort = chatApi.streamCompletion(
    {
      message: question,
      conversation_id: convId,
      agent_id: convAgentId.value ?? undefined,
      model_config_id: activeModelConfigId.value,
    },
    streamHandlers(streamActions),
  )
}

// 确认一条写命令：exec 结果由 useOpsConfirm 写回卡片，续答进新气泡，
// 与提问共用同一条流式管线。
function approveAction(action: OpsAction) {
  if (thinking.value) {
    // 一次只跑一条流：回答进行中发起的确认没有气泡机制可挂。
    toast.warning('请等当前回答结束后再确认')
    return
  }
  thinking.value = true
  streamingText.value = false
  const streamActions: OpsAction[] = []
  abort = approve(action, streamHandlers(streamActions))
}

function rejectAction(action: OpsAction) {
  void reject(action)
}

function stop() {
  abort?.()
  finishStream()
}

// 换了目标就是另一台机器/另一个库：窗口回到全新状态，历史列表整个换掉。
watch(
  () => [props.target, props.targetId],
  () => void loadTarget(),
  { immediate: true },
)

onBeforeUnmount(() => {
  loadSeq += 1
  abort?.()
})

defineExpose({
  /** 外部（查询工作台「询问 AI」按钮）直接把一个问题发进会话。 */
  ask,
})
</script>

<style scoped>
.ops-chat {
  display: flex;
  flex-direction: column;
  /* 卡片 body 是 flex 列，上面还压着一条 alert：用 flex:1 占剩余空间。
     height:100% 会拿整个 body 的高度，把底部的输入框挤出可视区。 */
  flex: 1;
  min-height: 0;
}

/* 工具条（历史 / 新会话）：发丝线以下才是消息区。 */
.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 8px;
  margin-bottom: 4px;
  border-bottom: 1px solid #f0f0f0;
}

.tool-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: all 0.15s;
}

.tool-btn:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.05);
  color: var(--signal-text);
}

.tool-btn:disabled {
  opacity: 0.45;
  cursor: default;
}

.chat-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px 4px;
}

/* 回到底部：sticky 钉在右下角，口径与 Chat.vue 一致，另补暗色块。 */
.jump-bottom {
  position: sticky;
  bottom: 12px;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  margin: 0 6px 6px auto;
  border: 1px solid var(--hairline);
  border-radius: 50%;
  background: var(--surface);
  color: var(--signal);
  box-shadow: var(--shadow-overlay);
  cursor: pointer;
}

.jump-bottom:hover {
  border-color: var(--signal);
  color: var(--signal);
}

.ops-chat.dark .jump-bottom {
  border-color: var(--on-dark-border);
  background: var(--surface-dark);
  color: var(--signal-bright);
}

.ops-chat.dark .jump-bottom:hover {
  border-color: rgba(58, 214, 222, 0.6);
  color: var(--signal-bright);
}

.jump-fade-enter-active,
.jump-fade-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.jump-fade-enter-from,
.jump-fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

.chat-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 100%;
  color: var(--text-3);
  font-size: var(--fs-sm);
  text-align: center;
}

.empty-icon {
  font-size: var(--fs-4xl);
}

.samples {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  margin-top: 4px;
}

/* 用 button 而不是 div：空态里的例子是可操作的，键盘也该能选中。 */
.sample {
  padding: 6px 10px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--scrim);
  font-size: var(--fs-xs);
  text-align: left;
  cursor: pointer;
  transition: all 0.15s;
}

.sample:hover {
  border-color: var(--signal-border);
  background: var(--signal-bg);
  color: var(--signal-text);
}

.msg {
  display: flex;
  margin-bottom: 10px;
}

.msg.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 92%;
  padding: 6px 10px;
  border-radius: var(--radius-md);
  /* 助手气泡不再铺底。之前正文、步骤、思考、进度、确认卡五种块全带底色，
     结果最该被看见的正文反而是唯一没底色的，被一圈色块淹没。现在整屏只有
     用户气泡和确认卡有色，其余一律降级成细线 + 灰字。 */
  background: transparent;
  font-size: var(--fs-sm);
  line-height: 1.65;
  word-break: break-word;
}

.msg.user .bubble {
  background: var(--signal-bg-strong);
}

.thinking-hint {
  margin-left: 8px;
  color: var(--text-3);
}

/* 进度指示：贴着最后一条气泡的一行小字 + 三个跳动点。做成带底色的整行块时，
   它每时每刻都在和刚生成的结论抢视线，而它想说的只有「还没说完」。 */
.status-line {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 2px 0 4px 10px;
  color: var(--text-3);
  font-size: var(--fs-xs);
}

.pulse {
  display: inline-flex;
  gap: 3px;
}

.pulse i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: currentColor;
  animation: ops-pulse 1.1s ease-in-out infinite;
}

.pulse i:nth-child(2) {
  animation-delay: 0.16s;
}

.pulse i:nth-child(3) {
  animation-delay: 0.32s;
}

@keyframes ops-pulse {
  0%,
  60%,
  100% {
    opacity: 0.25;
    transform: translateY(0);
  }
  30% {
    opacity: 0.9;
    transform: translateY(-2px);
  }
}

/* 步骤与文字交错流：附属行之间挤紧，正文块自己拉开一段。 */
.chunk-flow {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chunk-flow .content.markdown {
  margin-top: 6px;
}

/* 步骤行：等宽小字，无边角无边底。 */
.step-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding-left: 2px;
  font-size: var(--fs-xs);
  color: var(--text-3);
  line-height: 1.6;
}

.step-icon {
  flex: none;
  font-size: 11px;
  opacity: 0.5;
}

/* 命令按内容换行整段摊开：一行裁掉的 ellipsis 让长命令（jq 表达式、带管道的
   systemctl）在点开后又变成看不到全文的死信息。 */
.step-text {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: 'IBM Plex Mono', Menlo, Consolas, monospace;
  color: var(--text-2);
}

/* 折叠摘要行同样是无边底的一行字：可点的提示交给 hover 变色，不靠底色。 */
.step-summary {
  width: 100%;
  padding: 0 0 0 2px;
  border: none;
  background: transparent;
  text-align: left;
  cursor: pointer;
  transition: color 0.15s;
}

.step-summary:hover .step-icon,
.step-summary:hover .step-text {
  color: var(--signal-text);
  opacity: 1;
}

/* 思考过程：正文的旁白。一道细线 + 更淡的字，不铺底也不斜体——中文斜体既难读
   又让它看起来比结论更重要。展开后限高自己滚，别把正文顶出视口。 */
.reason-line {
  margin: 0;
  padding: 1px 8px 1px 9px;
  border-left: 2px solid var(--hairline);
  background: transparent;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--text-3);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}

/* 气泡操作行（复制）：悬停整条消息才出现。 */
.bubble-actions {
  display: flex;
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}

.msg:hover .bubble-actions,
.msg:focus-within .bubble-actions {
  opacity: 1;
}

.copy-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: all 0.15s;
}

.copy-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--signal-text);
}

.chat-composer {
  position: relative;
  padding-top: 8px;
  border-top: 1px solid #f0f0f0;
}

/* 模型选择条：窄条压在输入框上方，不换行、不抢输入区的位置。 */
.composer-model {
  width: 50%;
  margin-bottom: 6px;
}

.composer-model :deep(.ant-select-selector) {
  font-size: var(--fs-xs);
}

/* 给右下角的发送键让出位置，否则长文会钻到按钮底下。 */
.chat-composer :deep(textarea) {
  padding-right: 40px;
  resize: none;
}

.composer-send {
  position: absolute;
  right: 8px;
  bottom: 8px;
}

/* antd 的 disabled primary 圆钮会把箭头压到 rgba(0,0,0,.25)，
   在浅底上只剩一个空圈。这里固定一条可读的描边与箭头色。 */
.composer-send :deep(.ant-btn[disabled]) {
  background: #fff;
  border-color: #c8ced6;
  color: rgba(0, 0, 0, 0.45);
}

.ops-chat.dark .composer-send :deep(.ant-btn[disabled]) {
  background: transparent;
  border-color: var(--on-dark-border);
  color: var(--text-dark-2);
}

.markdown :deep(p) {
  margin: 0 0 6px;
}

.markdown :deep(pre) {
  padding: 8px;
  border-radius: var(--radius-sm);
  background: var(--ink);
  color: #d4d4d4;
  overflow-x: auto;
}

.markdown :deep(code) {
  font-size: var(--fs-xs);
}

/* 代码块 + 动作按钮（查询框/终端）：钉在右上角——右下角会跟横向滚动条打架。 */
.markdown :deep(.sql-block) {
  position: relative;
}

.markdown :deep(.sql-block pre) {
  margin-bottom: 0;
}

.markdown :deep(.snippet-open) {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 2px 8px;
  border: none;
  border-radius: var(--radius-sm);
  /* 代码可能正好写到按钮底下，底色得够实才能读。 */
  background: #3c3c3c;
  color: var(--signal-bright);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background 0.15s;
}

.markdown :deep(.snippet-open:hover) {
  background: #4f4f4f;
}

/* ---- 深色变体（全屏终端页右侧的 AI 面板，与终端 #11181f 同一族） ------------ */

.ops-chat.dark .chat-empty {
  color: var(--text-dark-2);
}

.ops-chat.dark .sample {
  border-color: var(--on-dark-border);
  color: #aab6c2;
}

.ops-chat.dark .sample:hover {
  border-color: rgba(58, 214, 222, 0.45);
  background: rgba(58, 214, 222, 0.1);
  color: var(--signal-bright);
}

.ops-chat.dark .bubble {
  background: transparent;
  color: var(--text-dark);
}

.ops-chat.dark .msg.user .bubble {
  background: rgba(58, 214, 222, 0.16);
  color: var(--text-dark);
}

.ops-chat.dark .status-line {
  color: var(--text-dark-2);
}

.ops-chat.dark .step-line {
  color: rgba(216, 222, 228, 0.42);
}

.ops-chat.dark .step-text {
  color: var(--text-dark-2);
}

.ops-chat.dark .step-summary:hover .step-icon,
.ops-chat.dark .step-summary:hover .step-text {
  color: var(--signal-bright);
}

.ops-chat.dark .reason-line {
  border-left-color: var(--on-dark-border);
  color: var(--text-dark-2);
}

.ops-chat.dark .thinking-hint {
  color: var(--text-dark-2);
}

.ops-chat.dark .copy-btn {
  color: var(--text-dark-2);
}

.ops-chat.dark .copy-btn:hover {
  background: var(--on-dark-hover);
  color: var(--signal-bright);
}

.ops-chat.dark .chat-toolbar {
  border-bottom-color: var(--on-dark-hover);
}

.ops-chat.dark .tool-btn {
  color: var(--text-dark-2);
}

.ops-chat.dark .tool-btn:hover:not(:disabled) {
  background: var(--on-dark-hover);
  color: var(--signal-bright);
}

.ops-chat.dark .chat-composer {
  border-top-color: var(--on-dark-hover);
}

.ops-chat.dark .composer-model :deep(.ant-select-selector) {
  background: #141d26;
  border-color: var(--on-dark-border);
  color: var(--text-dark);
}

.ops-chat.dark .composer-model :deep(.ant-select-selection-item),
.ops-chat.dark .composer-model :deep(.ant-select-selection-placeholder) {
  color: var(--text-dark);
}

.ops-chat.dark .composer-model :deep(.ant-select-arrow) {
  color: var(--text-dark-2);
}

.ops-chat.dark .chat-composer :deep(textarea) {
  background: #141d26;
  border-color: var(--on-dark-border);
  color: var(--text-dark);
}

.ops-chat.dark .chat-composer :deep(textarea::placeholder) {
  color: var(--text-dark-2);
}

.ops-chat.dark .chat-composer :deep(textarea:hover),
.ops-chat.dark .chat-composer :deep(textarea:focus) {
  border-color: rgba(58, 214, 222, 0.45);
  box-shadow: none;
}
</style>

<!-- 历史下拉渲染在 body（scoped 够不到），样式只能全局：一律用
     ops-history 前缀避免撞名。 -->
<style>
.ops-history-overlay .ant-dropdown-menu {
  max-width: 300px;
  max-height: 320px;
  overflow-y: auto;
}

.ops-history-overlay .history-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 0;
}

.ops-history-overlay .history-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.ops-history-overlay .history-title {
  flex: 1;
  min-width: 0;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
}

/* 删除图标平时藏着，悬停该条才出现：列表主要还是用来「打开」的。 */
.ops-history-overlay .history-del {
  flex-shrink: 0;
  padding: 2px;
  border-radius: var(--radius-sm);
  color: rgba(0, 0, 0, 0.4);
  font-size: var(--fs-xs);
  opacity: 0;
  transition: all 0.15s;
}

.ops-history-overlay .ant-dropdown-menu-item:hover .history-del {
  opacity: 1;
}

.ops-history-overlay .history-del:hover {
  background: rgba(255, 77, 79, 0.12);
  color: var(--danger-bright);
}

/* 重命名图标与删除同一交互：悬停整行才显形，hover 各自上色。 */
.ops-history-overlay .history-rename {
  flex-shrink: 0;
  padding: 2px;
  border-radius: var(--radius-sm);
  color: rgba(0, 0, 0, 0.4);
  font-size: var(--fs-xs);
  opacity: 0;
  transition: all 0.15s;
}

.ops-history-overlay .ant-dropdown-menu-item:hover .history-rename {
  opacity: 1;
}

.ops-history-overlay .history-rename:hover {
  background: rgba(0, 128, 255, 0.12);
  color: #0080ff;
}

.ops-history-overlay .history-meta {
  color: rgba(0, 0, 0, 0.45);
  font-size: var(--fs-xs);
}

/* 深色变体（全屏终端页的 AI 面板）：菜单底色/文字与面板同族。 */
.ops-history-overlay.dark .ant-dropdown-menu {
  background: var(--surface-dark);
}

.ops-history-overlay.dark .ant-dropdown-menu-item {
  color: var(--text-dark);
}

.ops-history-overlay.dark .ant-dropdown-menu-item:hover,
.ops-history-overlay.dark .ant-dropdown-menu-item-selected {
  background: rgba(58, 214, 222, 0.12) !important;
  color: var(--signal-bright);
}

.ops-history-overlay.dark .ant-dropdown-menu-item-disabled {
  color: var(--text-dark-2) !important;
}

.ops-history-overlay.dark .history-meta {
  color: var(--text-dark-2);
}

.ops-history-overlay.dark .history-del {
  color: var(--text-dark-2);
}

.ops-history-overlay.dark .history-del:hover {
  background: rgba(255, 77, 79, 0.18);
  color: var(--danger-soft);
}
</style>
