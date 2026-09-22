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

    <div ref="scrollEl" class="chat-body" @click="onBodyClick">
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
          <div v-if="item.steps.length" class="steps">
            <a-tag v-for="(step, i) in item.steps" :key="i" color="blue">{{ step }}</a-tag>
          </div>
          <!-- 助手回答按 markdown 渲染，用户输入保持纯文本。 -->
          <div
            v-if="item.kind === 'assistant'"
            class="content markdown"
            v-html="renderCached(item.key, item.text)"
          />
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
          <!-- 复制原始 markdown 文本，贴到别处还能用。 -->
          <div v-if="item.kind === 'assistant' && item.text" class="bubble-actions">
            <a-tooltip title="复制">
              <button type="button" class="copy-btn" @click="copyItem(item)">
                <CopyOutlined />
              </button>
            </a-tooltip>
          </div>
        </div>
      </div>

      <div v-if="thinking && !streamingText" class="msg assistant">
        <div class="bubble">
          <a-spin size="small" />
          <span class="thinking-hint">正在排查…</span>
        </div>
      </div>
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
  HistoryOutlined,
  PlusOutlined,
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

interface TalkItem {
  key: string
  /** 落库消息 id：确认卡片按它对位到触发提议的气泡下面。 */
  id?: number
  kind: 'user' | 'assistant'
  text: string
  steps: string[]
}

const items = ref<TalkItem[]>([])
const draft = ref('')

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
const thinking = ref(false)
const streamingText = ref(false)
const scrollEl = ref<HTMLElement | null>(null)
const inputEl = ref<{ focus: () => void } | null>(null)

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

function scrollToBottom() {
  nextTick(() => {
    const el = scrollEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function currentAnswer(): TalkItem {
  const last = items.value[items.value.length - 1]
  if (last && last.kind === 'assistant') return last
  const created: TalkItem = { key: nextKey(), kind: 'assistant', text: '', steps: [] }
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
  thinking.value = false
  streamingText.value = false
  streamingAnswerKey.value = null
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
      steps: [],
    }))
    actions.value = acts.items
    scrollToBottom()
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
  return {
    onStep: (data) => {
      answer().steps.push(describeStep(data))
      scrollToBottom()
    },
    onToken: (token) => {
      const item = answer()
      item.text += token
      streamingText.value = true
      beginStream(item.key)
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
        answer().steps.push(`已执行：${ev.command}（exit ${ev.exit_status ?? '—'}）`)
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

  items.value.push({ key: nextKey(), kind: 'user', text: question, steps: [] })
  thinking.value = true
  streamingText.value = false
  scrollToBottom()

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
  border-radius: 4px;
  background: transparent;
  color: var(--text-3);
  font-size: 12px;
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

.chat-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 100%;
  color: var(--text-3);
  font-size: 13px;
  text-align: center;
}

.empty-icon {
  font-size: 28px;
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
  color: rgba(0, 0, 0, 0.65);
  font-size: 12px;
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
  padding: 8px 10px;
  border-radius: 8px;
  background: #f5f5f5;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}

.msg.user .bubble {
  background: var(--signal-bg-strong);
}

.steps {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 6px;
}

.thinking-hint {
  margin-left: 8px;
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
  border-radius: 4px;
  background: transparent;
  color: var(--text-3);
  font-size: 12px;
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
  font-size: 12px;
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

.markdown :deep(p) {
  margin: 0 0 6px;
}

.markdown :deep(pre) {
  padding: 8px;
  border-radius: 4px;
  background: var(--ink);
  color: #d4d4d4;
  overflow-x: auto;
}

.markdown :deep(code) {
  font-size: 12px;
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
  border-radius: 4px;
  /* 代码可能正好写到按钮底下，底色得够实才能读。 */
  background: #3c3c3c;
  color: var(--signal-bright);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s;
}

.markdown :deep(.snippet-open:hover) {
  background: #4f4f4f;
}

/* ---- 深色变体（全屏终端页右侧的 AI 面板，与终端 #11181f 同一族） ------------ */

.ops-chat.dark .chat-empty {
  color: #8b98a5;
}

.ops-chat.dark .sample {
  border-color: rgba(255, 255, 255, 0.14);
  color: #aab6c2;
}

.ops-chat.dark .sample:hover {
  border-color: rgba(58, 214, 222, 0.45);
  background: rgba(58, 214, 222, 0.1);
  color: #3ad6de;
}

.ops-chat.dark .bubble {
  background: #1b2530;
  color: #d8dee4;
}

.ops-chat.dark .msg.user .bubble {
  background: rgba(58, 214, 222, 0.16);
  color: #d8dee4;
}

.ops-chat.dark .thinking-hint {
  color: #8b98a5;
}

.ops-chat.dark .copy-btn {
  color: #8b98a5;
}

.ops-chat.dark .copy-btn:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #3ad6de;
}

.ops-chat.dark .chat-toolbar {
  border-bottom-color: rgba(255, 255, 255, 0.08);
}

.ops-chat.dark .tool-btn {
  color: #8b98a5;
}

.ops-chat.dark .tool-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  color: #3ad6de;
}

.ops-chat.dark .chat-composer {
  border-top-color: rgba(255, 255, 255, 0.08);
}

.ops-chat.dark .composer-model :deep(.ant-select-selector) {
  background: #141d26;
  border-color: rgba(255, 255, 255, 0.14);
  color: #d8dee4;
}

.ops-chat.dark .composer-model :deep(.ant-select-selection-item),
.ops-chat.dark .composer-model :deep(.ant-select-selection-placeholder) {
  color: #d8dee4;
}

.ops-chat.dark .composer-model :deep(.ant-select-arrow) {
  color: #5f6b78;
}

.ops-chat.dark .chat-composer :deep(textarea) {
  background: #141d26;
  border-color: rgba(255, 255, 255, 0.14);
  color: #d8dee4;
}

.ops-chat.dark .chat-composer :deep(textarea::placeholder) {
  color: #5f6b78;
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
  font-size: 13px;
}

/* 删除图标平时藏着，悬停该条才出现：列表主要还是用来「打开」的。 */
.ops-history-overlay .history-del {
  flex-shrink: 0;
  padding: 2px;
  border-radius: 4px;
  color: rgba(0, 0, 0, 0.4);
  font-size: 12px;
  opacity: 0;
  transition: all 0.15s;
}

.ops-history-overlay .ant-dropdown-menu-item:hover .history-del {
  opacity: 1;
}

.ops-history-overlay .history-del:hover {
  background: rgba(255, 77, 79, 0.12);
  color: #ff4d4f;
}

.ops-history-overlay .history-meta {
  color: rgba(0, 0, 0, 0.45);
  font-size: 11px;
}

/* 深色变体（全屏终端页的 AI 面板）：菜单底色/文字与面板同族。 */
.ops-history-overlay.dark .ant-dropdown-menu {
  background: #1b2530;
}

.ops-history-overlay.dark .ant-dropdown-menu-item {
  color: #d8dee4;
}

.ops-history-overlay.dark .ant-dropdown-menu-item:hover,
.ops-history-overlay.dark .ant-dropdown-menu-item-selected {
  background: rgba(58, 214, 222, 0.12) !important;
  color: #3ad6de;
}

.ops-history-overlay.dark .ant-dropdown-menu-item-disabled {
  color: #5f6b78 !important;
}

.ops-history-overlay.dark .history-meta {
  color: #8b98a5;
}

.ops-history-overlay.dark .history-del {
  color: #8b98a5;
}

.ops-history-overlay.dark .history-del:hover {
  background: rgba(255, 77, 79, 0.18);
  color: #ff7875;
}
</style>
