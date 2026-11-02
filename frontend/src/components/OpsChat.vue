<template>
  <div :class="['ops-chat', { dark }]">
    <!-- 工具条：历史会话下拉 + 新会话。面板每次打开都是全新窗口，旧排查从这里翻回去。 -->
    <div class="chat-toolbar">
      <OpsHistoryMenu
        :history="history"
        :conversation-id="conversationId"
        :dark="dark"
        @open="openConversation"
        @rename="startRename"
        @remove="removeConversation"
        @open-change="onHistoryOpen"
      >
        <button type="button" class="tool-btn" :disabled="!targetId">
          <HistoryOutlined /> 历史
        </button>
      </OpsHistoryMenu>
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

    <!-- 待确认提示条：钉在滚动区之外，滚到哪儿都在。写命令只有 180s 窗口，
         卡片埋在长回答中间没人点，超时后只能让 AI 重新提议一遍。 -->
    <button
      v-if="pendingActions.length"
      type="button"
      :class="['pending-bar', { dark }]"
      @click="jumpToPending"
    >
      <WarningOutlined />
      {{ pendingActions.length }} 条写命令等你确认 · 点击跳过去
    </button>

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
                <div
                  v-else-if="chunk.type === 'step'"
                  class="step-line"
                  :title="chunk.full"
                >
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
                <!-- 写命令确认卡片：按提议发生的时刻插在正文中间，而不是
                     沉在气泡底部被后续内容顶上去。 -->
                <ConfirmCard
                  v-else-if="chunk.type === 'confirm' && chunk.action"
                  :action="chunk.action"
                  :acting="isActing(chunk.action)"
                  :dark="dark"
                  :can-approve="auth.canOpsWrite"
                  @approve="approveAction"
                  @reject="rejectAction"
                  @expired="settleExpired"
                />
              </template>
            </div>
            <!-- 兜底：如果 chunks 还没数据但 text 有内容（历史回放），整段渲染。 -->
            <div
              v-if="!item.chunks.length && item.text"
              class="content markdown"
              v-html="renderCached(item.key, item.text)"
            />
            <!-- 重试留痕：这条回答是超时后自动重试才生成出来的。 -->
            <div v-if="item.note" class="retry-note">{{ item.note }}</div>
          </template>
          <div v-else class="content" v-text="item.text" />
          <!-- 兜底：没有时间线条目的待确认项（老消息）仍按 message_id 挂气泡末尾。 -->
          <ConfirmCard
            v-for="act in actionsOf(item)"
            :key="act.id"
            :action="act"
            :acting="isActing(act)"
            :dark="dark"
            :can-approve="auth.canOpsWrite"
            @approve="approveAction"
            @reject="rejectAction"
            @expired="settleExpired"
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
import { computed, nextTick, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { message as toast, Modal } from 'ant-design-vue'
import {
  RobotOutlined,
  ArrowUpOutlined,
  CopyOutlined,
  HistoryOutlined,
  PlusOutlined,
  VerticalAlignBottomOutlined,
  WarningOutlined,
} from '@ant-design/icons-vue'
import ConfirmCard from '@/components/ConfirmCard.vue'
import OpsHistoryMenu from '@/components/OpsHistoryMenu.vue'
import { useAuthStore } from '@/stores/auth'
import { createMarkdown } from '@/utils/markdown'
import { useStickToBottom } from '@/utils/useStickToBottom'
import { capCache } from '@/utils/cacheLimit'
import { toolLabel } from '@/utils/chatTools'
import { useOpsConfirm, actionFromConfirm } from '@/utils/useOpsConfirm'
import { useStreamRender } from '@/utils/streamRender'
import { foldChunks, type FoldChunk, type FoldedChunk } from '@/utils/opsChunks'
import { copyText } from '@/utils/clipboard'
import { storageKeys } from '@/utils/storage'
import {
  chatApi,
  settingsApi,
  type Conversation,
  type ModelConfig,
  type OpsAction,
  type StreamHandlers,
  type TimelineEntry,
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
  capCache(snippetBlocks, 300)
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

/** 一条助手回复由若干 chunk 按到达顺序拼接：步骤、文字、思考、确认卡片交替。 */
type TalkChunk = FoldChunk

interface TalkItem {
  key: string
  /** 落库消息 id：确认卡片按它对位到触发提议的气泡下面。 */
  id?: number
  kind: 'user' | 'assistant'
  /** 完整文本（用于复制、历史回放兜底渲染）。 */
  text: string
  /** 按到达顺序排列的步骤与文字片段，用于交错渲染。 */
  chunks: TalkChunk[]
  /** 自动重试过的说明（只在本次页面内可见，不落库不参与复制）。 */
  note?: string
}

const items = ref<TalkItem[]>([])
const draft = ref('')

// ---- 步骤折叠 ---------------------------------------------------------------
// 折叠规则（按波就地收、单条命令不折叠）在 utils/opsChunks.ts 里，这里只管
// 展开状态与点击切换。
const expandedSteps = ref<Set<string>>(new Set())

function toggleSteps(key: string) {
  const set = expandedSteps.value
  if (set.has(key)) set.delete(key)
  else set.add(key)
}

type RenderChunk = FoldedChunk<TalkChunk, OpsAction>

function displayChunks(item: TalkItem): RenderChunk[] {
  return foldChunks(item.chunks, item.key, expandedSteps.value, actionById.value)
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

const actionById = computed(() => new Map(actions.value.map((a) => [a.id, a])))

// 正在流式的那条助手气泡：老消息没有时间线时，卡片只能按它挂到气泡尾部。
const streamingAnswerKey = ref<string | null>(null)

/** 最后一条助手气泡：无主待确认项的兜底挂载点。 */
const tailAssistantKey = computed(() => {
  for (let i = items.value.length - 1; i >= 0; i -= 1) {
    if (items.value[i].kind === 'assistant') return items.value[i].key
  }
  return null
})

/**
 * 挂在气泡尾部的待确认项兜底：时间线里有 confirm 条目的按位置就地渲染（见
 * displayChunks），这里只收那些没有位置信息的——老消息、以及本轮模型一个
 * 字都没吐就直接提议命令的极端情况。
 */
function actionsOf(item: TalkItem): OpsAction[] {
  const inline = new Set(
    item.chunks.filter((c) => c.type === 'confirm').map((c) => c.actionId),
  )
  return actions.value.filter((a) => {
    if (inline.has(a.id)) return false
    if (a.message_id != null) return a.message_id === item.id
    // message_id 没回填（整轮回答没落库就断了：停止/刷新/断线）：宁可挂到最后
    // 一条助手气泡，也不能让它谁都不匹配——那样这条命令在界面上根本不存在，
    // 用户只看到 AI 说「已提交，请点确认」而没有任何可点的东西，180s 后超时。
    return item.key === streamingAnswerKey.value || item.key === tailAssistantKey.value
  })
}

/** 还没了结的写命令：卡片可能埋在长回答中间，也可能整张在视口外。 */
const pendingActions = computed(() => actions.value.filter((a) => a.status === 'pending'))

/**
 * 本地倒计时归零。服务端对超时的 pending 本来就是报 expired（见 OpsAction 的
 * status 注释），这里只是让流式中创建的那一条在界面上早点自洽：卡片切文案、
 * 顶部提示条收起来，不用等下一次拉列表。
 */
function settleExpired(action: OpsAction) {
  if (action.status === 'pending') action.status = 'expired'
}

/** 跳到第一条待确认的卡片：卡片可能离当前视口很远，滚过去比让用户自己找快。 */
function jumpToPending() {
  const target = pendingActions.value[0]
  if (!target) return
  nextTick(() => {
    const cards = scrollEl.value?.querySelectorAll('.confirm-card') ?? []
    for (const card of cards) {
      if ((card as HTMLElement).dataset.actionId === String(target.id)) {
        card.scrollIntoView({ block: 'center', behavior: 'smooth' })
        return
      }
    }
  })
}

/** 这条气泡是不是正在流的那条（决定气泡操作行要不要出现）。 */
function isStreaming(item: TalkItem): boolean {
  return thinking.value && item.key === streamingAnswerKey.value
}
const thinking = ref(false)
const streamingText = ref(false)

/** 自动重试上限：一次提问最多再试 2 次（加首次共 3 次尝试）。 */
const MAX_AUTO_RETRIES = 2
/** 本轮已经自动重试过几次；每次新提问归零。 */
let retryCount = 0
/** 重试中的提示状态（attempt 从 1 开始）；不在重试时为 null。 */
const retryState = ref<{ attempt: number; max: number } | null>(null)

// ---- 流式合批 -----------------------------------------------------------------
// token 先攒进普通字符串，每 80ms 提交一次：逐 token 改 item.text / chunk.detail
// 会让整条气泡（含历史 chunk 折叠逻辑）按 token 频率重渲染。
const STREAM_FLUSH_MS = 80
/** 页面是否在前台：keep-alive 切走后 token 只进缓冲，不再驱动 DOM。 */
const pageActive = ref(true)
/** 当前活跃流的合批提交函数，由 streamHandlers 挂上；收尾与切回页面时直接调用。 */
let flushStreamTextHook: () => void = () => {}
/** 同一条流的缓冲清理钩子：自动重试丢弃半截气泡时先调它，免得迟到的 flush 写脏数据。 */
let cancelStreamBufferHook: () => void = () => {}
/** 当前正在执行的步骤摘要（step 事件到达时写入，下一个 token 到达时清空）。 */
const currentStepDetail = ref('')
const scrollEl = ref<HTMLElement | null>(null)
const inputEl = ref<{ focus: () => void } | null>(null)

/** 持久状态指示文案：区分 AI 在思考、执行命令、还是空闲。 */
const aiStatusText = computed(() => {
  // 重试中上一轮的步骤与正文都已丢弃，这里只报重试进度。
  if (retryState.value) {
    const { attempt, max } = retryState.value
    return `请求超时，正在自动重试（${attempt}/${max}）…`
  }
  // 步骤文案走 toolLabel，动词自带（「执行只读命令：…」），再套一层
  // 「正在执行：」就重复了；进行中由那三个跳动的点表示。
  if (currentStepDetail.value) return currentStepDetail.value
  if (streamingText.value) return '正在生成回答…'
  return '正在思考…'
})

// ---- 贴底滚动 ---------------------------------------------------------------
// 与 Chat.vue 同一口径：上翻时流式事件不抢视图，原本贴底才往下跟。
const {
  stickToBottom,
  onScroll: onBodyScroll,
  scrollToBottom,
  pinToBottom,
} = useStickToBottom(scrollEl, pageActive)

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
      // 后端按到达顺序存了整条回答流（含命令、思考与确认提议），据此重建交错
      // 渲染；老消息没有 timeline，退回空 chunks → 模板用 content 兜底整段渲染。
      // llm 节点（模型往返耗时）是统一 trace 给对话页的信息，运维面板的命令
      // 时间线不展示它：过滤掉再映射，避免落进兜底分支造出非法 chunk。
      chunks: (m.timeline ?? [])
        .filter(
          (e): e is TimelineEntry & { type: 'text' | 'reason' | 'step' | 'confirm' } =>
            e.type !== 'llm',
        )
        .map((e): TalkChunk => {
          if (e.type === 'step') {
            const { text, full } = describeStep({
              tool: e.tool ?? '',
              query: e.query,
              input: e.input,
            })
            return { type: 'step', detail: text, full }
          }
          if (e.type === 'confirm') {
            return { type: 'confirm', detail: '', actionId: e.action_id }
          }
          return { type: e.type, detail: e.detail ?? '' }
        }),
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

// ---- SSE 事件处理 -------------------------------------------------------------
// completions 与 confirm 两条流共用同一组回调：step/token/confirm/done/error
// 语义一致；exec 只有 confirm 流会发。
//
// ``onStreamError`` 只有提问流传：超时这类可重试的失败交给它决定要不要自动重试；
// 确认流没传，出错就直接弹提示收尾（那一轮没有可重放的提问）。
function streamHandlers(
  streamActions: OpsAction[],
  onStreamError?: (message: string, retryable: boolean) => void,
): StreamHandlers {
  // 一轮流式 = 一条助手气泡。确认流的续答绝不能并进提问流那条气泡：卡片是
  // 按时间线插在气泡里的，两轮流式混在一条气泡里就会让先到的卡片被后到的
  // 步骤与正文压在中间。后端本来也是每轮流式各落一条消息，这样两边对得上。
  let bubble: TalkItem | null = null
  // 已经提议过写命令：重试会让确认卡片多出一张，所以这种情况不自动重试。
  let sawProposal = false
  const answer = () => {
    if (!bubble) {
      const created: TalkItem = { key: nextKey(), kind: 'assistant', text: '', chunks: [] }
      items.value.push(created)
      bubble = created
    }
    streamingAnswerKey.value = bubble.key
    return bubble
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

  // 正文合批：token 攒在非响应式字符串里，80ms 提交一次；思考段仍逐条追加。
  let textBuffer = ''
  let bufferKey = ''
  let bufferItem: TalkItem | null = null
  let bufferChunk: TalkChunk | null = null
  let flushTimer: number | undefined
  const flushText = () => {
    if (flushTimer !== undefined) {
      window.clearTimeout(flushTimer)
      flushTimer = undefined
    }
    if (!textBuffer || !bufferItem || !bufferChunk) return
    const chunk = textBuffer
    textBuffer = ''
    bufferItem.text += chunk
    bufferChunk.detail += chunk
    // 收到 token 后步骤已执行完，清掉步骤状态。
    if (currentStepDetail.value && streamingText.value) currentStepDetail.value = ''
    streamingText.value = true
    // 节流 key 在开始攒时就定好（活跃文字块的稳定下标），不能等到提交时再算：
    // 期间若插入了 step/reason 块，下标会漂移，renderCached 对不上就每 tick 全量解析。
    beginStream(bufferKey)
    schedule()
    scrollToBottom()
  }
  flushStreamTextHook = flushText
  // 自动重试要丢掉这一轮的气泡：连没提交的缓冲和定时器一起扔，否则 80ms
  // 后那次 flush 会往一条已经移除的气泡里写字。
  cancelStreamBufferHook = () => {
    if (flushTimer !== undefined) {
      window.clearTimeout(flushTimer)
      flushTimer = undefined
    }
    textBuffer = ''
    bufferItem = null
    bufferChunk = null
  }
  return {
    onStep: (data) => {
      const item = answer()
      // 换块前先把还没提交的正文结掉，否则这段文字会被算到下一步骤后的新块里。
      flushText()
      closeTextChunk()
      const { text, full } = describeStep(data)
      currentStepDetail.value = text
      item.chunks.push({ type: 'step', detail: text, full })
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
      const tc = ensureTextChunk(item)
      // 切到新文字块时，先把上一块的缓冲提交掉。
      if (bufferChunk && bufferChunk !== tc) flushText()
      bufferItem = item
      bufferChunk = tc
      // key 用本块在 chunks 里的稳定下标，提交时不再重算。
      bufferKey = `${item.key}-${item.chunks.length - 1}`
      textBuffer += token
      // keep-alive 缓存期间只进缓冲：切回来时 onActivated 一次补上。
      if (!pageActive.value) return
      if (flushTimer === undefined) {
        flushTimer = window.setTimeout(flushText, STREAM_FLUSH_MS)
      }
    },
    onConfirm: (data) => {
      // 模型提议后可能直接结束本轮、一个 token 都不吐：先保证气泡存在，
      // 确认卡片才有地方挂。
      const item = answer()
      // 已经落了一条待确认命令，本轮不再自动重试：重放会多出一张一样的卡片。
      sawProposal = true
      const action = actionFromConfirm(data, conversationId.value)
      actions.value.push(action)
      streamActions.push(action)
      // 卡片就地插在提议发生的这个位置（与后端 timeline 的 confirm 条目同一
      // 时刻），后面的正文从新的一块 chunk 开始。
      flushText()
      closeTextChunk()
      item.chunks.push({ type: 'confirm', detail: '', actionId: action.id })
      scrollToBottom()
    },
    onExec: (ev) => {
      // 确认流的 exec：useOpsConfirm 已把结果写回卡片，这里补步骤标签。
      // 只读命令（action_id 为空）在 step 里已经亮过命令，不再重复打标签。
      if (ev.action_id != null) {
        const item = answer()
        flushText()
        closeTextChunk()
        // 命令全文在上面那张确认卡片里，这里只报结果，不再摊一遍命令。
        item.chunks.push({
          type: 'step',
          detail: `已执行写命令（exit ${ev.exit_status ?? '—'}）`,
          full: ev.command,
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
      // 记下落库 id 并回填本轮提议的 message_id：卡片已按时间线就地渲染，
      // 这个 id 只服务「老消息没有时间线」那条兜底路径。
      const item = bubble ?? items.value[items.value.length - 1]
      if (item && item.kind === 'assistant') {
        item.id = message_id
        // 重试成功也要在页面上留痕：用户得知道这段回答是第二次才生成的。
        if (retryCount > 0) item.note = `请求超时，已自动重试 ${retryCount} 次后生成`
      }
      for (const a of streamActions) a.message_id = message_id
      finishStream()
      // 本轮只改变当前会话的条数与活跃时间，历史下拉里已有这条就就地更新；
      // 新会话首次落库（列表里还没有它）才回源，省掉每轮一次整表请求。
      const row = history.value.find((c) => c.id === conversationId.value)
      if (row) {
        row.message_count += 2
        row.updated_at = new Date().toISOString()
      } else {
        void refreshHistory(loadSeq)
      }
    },
    onError: ({ message, retryable }) => {
      if (onStreamError) {
        onStreamError(message, Boolean(retryable) && !sawProposal)
        return
      }
      toast.error(message)
      finishStream()
    },
  }
}

function finishStream() {
  // 收尾前先提交缓冲里的残文，停止/出错时半段也要完整落定。
  flushStreamTextHook()
  flushStreamTextHook = () => {}
  cancelStreamBufferHook = () => {}
  thinking.value = false
  streamingText.value = false
  streamingAnswerKey.value = null
  currentStepDetail.value = ''
  retryState.value = null
  retryCount = 0
  abort = null
  endStream()
}

async function copyItem(item: TalkItem) {
  if (await copyText(item.text)) toast.success('已复制')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

/**
 * 步骤行的文案：走主对话同一份 toolLabel（中文标题 + 80 字截断），全文另存在
 * full 里给悬停 title。写命令提议是个例外——它的正文就是紧随其后的那张确认
 * 卡片，步骤行再摊一遍命令，一条气泡里同一串 kubectl 会出现三遍。
 */
function describeStep(payload: {
  tool: string
  query?: string
  input?: Record<string, unknown>
}): { text: string; full: string } {
  const input = payload.input ?? (payload.query ? { query: payload.query } : {})
  const raw = input.command ?? input.statement ?? input.query ?? ''
  const full = typeof raw === 'string' && raw ? raw : ''
  if (payload.tool === 'propose_command') {
    return { text: '已提议一条写命令，见下方确认卡片', full }
  }
  return { text: toolLabel(payload.tool, input), full }
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
  retryCount = 0
  retryState.value = null
  pinToBottom()

  const streamActions: OpsAction[] = []
  abort = chatApi.streamCompletion(
    {
      message: question,
      conversation_id: convId,
      agent_id: convAgentId.value ?? undefined,
      model_config_id: activeModelConfigId.value,
    },
    streamHandlers(streamActions, handleStreamError),
  )
}

/**
 * 可重试失败（目前是模型调用超时）的处理：自动用 /retry 再答一遍。
 *
 * 超时意味着这一问已落库、回答没有落库，正好对上 retry 接口的前提——
 * 它会先删掉那条悬空提问再重新回答，历史里不会留下重复的一问。
 * 重试前把这一轮流出来的半截气泡丢掉，从干净气泡重新生成。
 */
function handleStreamError(message: string, retryable: boolean) {
  const convId = conversationId.value
  if (retryable && convId && retryCount < MAX_AUTO_RETRIES) {
    retryCount += 1
    retryState.value = { attempt: retryCount, max: MAX_AUTO_RETRIES }
    discardStreamingBubble()
    const streamActions: OpsAction[] = []
    abort = chatApi.streamRetry(convId, streamHandlers(streamActions, handleStreamError))
    return
  }
  // 到顶或不可重试：把「试过几次」一并写进提示，用户不会以为是一次就失败。
  toast.error(retryCount > 0 ? `已自动重试 ${retryCount} 次仍失败：${message}` : message)
  finishStream()
}

/** 丢弃正在流的那条半截气泡：它的内容会被重新生成的完整回答取代。 */
function discardStreamingBubble() {
  cancelStreamBufferHook()
  const key = streamingAnswerKey.value
  if (key) items.value = items.value.filter((item) => item.key !== key)
  streamingAnswerKey.value = null
  streamingText.value = false
  currentStepDetail.value = ''
  endStream()
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

// keep-alive：切走不中断 SSE，但停止驱动视图——token 只进缓冲，滚动全部跳过。
onDeactivated(() => {
  pageActive.value = false
})

onActivated(() => {
  pageActive.value = true
  // 缓存期间积压的正文一次补上。
  flushStreamTextHook()
  scrollToBottom()
})

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
  border-bottom: 1px solid var(--hairline);
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
  background: var(--hover-neutral);
  color: var(--signal-text);
}

.tool-btn:disabled {
  opacity: 0.45;
  cursor: default;
}

/* 待确认提示条：压在滚动区上方，不随消息滚走。整条就是按钮，点一下滚到
   卡片那儿去——确认窗口只有 180s，看不见就等于拒绝。 */
.pending-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  margin-bottom: 6px;
  padding: 5px 8px;
  border: 1px solid var(--warning);
  border-radius: var(--radius-sm);
  background: var(--warning-bg);
  color: var(--warning);
  font-size: var(--fs-xs);
  text-align: left;
  cursor: pointer;
  transition: background 0.15s;
}

.pending-bar:hover {
  background: var(--warning-bg-strong);
}

.ops-chat.dark .pending-bar {
  color: var(--warning-bright);
  border-color: var(--warning-on-dark-border);
  background: var(--warning-on-dark-bg);
}

.ops-chat.dark .pending-bar:hover {
  background: var(--warning-on-dark-bg-strong);
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
  border-color: var(--signal-on-dark-border-hover);
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
  /* 离屏消息跳过布局与绘制；auto 记住上次高度，滚动回看不抖。 */
  content-visibility: auto;
  contain-intrinsic-size: auto 120px;
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

/* toolLabel 把命令截到 80 字，这里只负责让截断后的部分换行不溢出。全文没丢：
   整行有悬停 title，写命令的全文在确认卡片里，只读命令在终端窗口有回显。 */
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

/* 重试留痕：一条小字旁白，跟在回答末尾，不参与复制。 */
.retry-note {
  margin-top: 6px;
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--text-3);
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
  background: var(--hover-neutral);
  color: var(--signal-text);
}

.chat-composer {
  position: relative;
  padding-top: 8px;
  border-top: 1px solid var(--hairline);
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
  background: var(--surface);
  border-color: var(--hairline);
  color: var(--disabled-text);
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
  color: var(--text-dark);
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
  background: var(--code-action-bg);
  color: var(--signal-bright);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background 0.15s;
}

.markdown :deep(.snippet-open:hover) {
  background: var(--code-action-bg-hover);
}

/* ---- 深色变体（全屏终端页右侧的 AI 面板，与终端 #11181f 同一族） ------------ */

.ops-chat.dark .chat-empty {
  color: var(--text-dark-2);
}

.ops-chat.dark .sample {
  border-color: var(--on-dark-border);
  color: var(--text-dark-2);
}

.ops-chat.dark .sample:hover {
  border-color: var(--signal-on-dark-border);
  background: var(--signal-on-dark-bg);
  color: var(--signal-bright);
}

.ops-chat.dark .bubble {
  background: transparent;
  color: var(--text-dark);
}

.ops-chat.dark .msg.user .bubble {
  background: var(--signal-on-dark-bg-strong);
  color: var(--text-dark);
}

.ops-chat.dark .status-line {
  color: var(--text-dark-2);
}

.ops-chat.dark .step-line {
  color: var(--text-dark-3);
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

.ops-chat.dark .retry-note {
  color: var(--text-dark-3);
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
  background: var(--surface-dark-inset);
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
  background: var(--surface-dark-inset);
  border-color: var(--on-dark-border);
  color: var(--text-dark);
}

.ops-chat.dark .chat-composer :deep(textarea::placeholder) {
  color: var(--text-dark-2);
}

.ops-chat.dark .chat-composer :deep(textarea:hover),
.ops-chat.dark .chat-composer :deep(textarea:focus) {
  border-color: var(--signal-on-dark-border);
  box-shadow: none;
}
</style>

