<template>
  <div class="chat-page">
    <!-- 各种前置条件提示：把缺什么讲清楚，而不是等到发送时才报错。
         状态检查本身失败要排在最前——那是网络问题，不是配置缺失。 -->
    <a-alert
      v-if="!booting && readinessFailed"
      type="error"
      show-icon
      class="gate-alert"
      message="无法确认模型配置状态"
      description="状态检查请求失败，可能是网络波动。重试成功前无法发送对话。"
    >
      <template #action>
        <a-button size="small" danger @click="loadReadiness">重试</a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="!booting && !chatReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的对话模型"
      description="对话功能需要一个已通过连通性测试的对话模型才能回答问题。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="router.push('/settings')">
          去配置
        </a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="!booting && needsKnowledge && !embeddingReady"
      type="warning"
      show-icon
      class="gate-alert"
      message="未配置可用的向量模型"
      description="当前智能体会检索知识库，因此还需要一个向量模型。也可以切换到不检索知识库的智能体。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="router.push('/settings')">
          去配置
        </a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="!booting && needsKnowledge && !projects.length"
      type="warning"
      show-icon
      class="gate-alert"
      message="还没有知识库项目"
      description="当前智能体依据知识库回答，需要先建立一个项目并上传文档。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="router.push('/knowledge')">
          去创建
        </a-button>
      </template>
    </a-alert>

    <a-alert
      v-else-if="!booting && needsKnowledge && !activeProjectId"
      type="info"
      show-icon
      class="gate-alert"
      message="请先选择项目"
      description="项目决定了这次对话能读到哪些文档。选定后才会检索并回答，避免答案来自不相干的项目。"
    />

    <a-alert
      v-else-if="!booting && needsKnowledge && kbEmpty"
      type="info"
      show-icon
      class="gate-alert"
      message="知识库还没有文档"
      description="当前智能体依据知识库回答，请先上传文档，否则只会得到「未找到相关内容」。"
    >
      <template #action>
        <a-button size="small" type="primary" @click="router.push('/knowledge')">
          去上传
        </a-button>
      </template>
    </a-alert>

    <div class="chat-body">
      <!-- 会话列表 -->
      <a-card class="side" size="small" title="会话">
        <template #extra>
          <a-button type="primary" size="small" @click="startNew">
            <PlusOutlined /> 新会话
          </a-button>
        </template>
        <!-- 会话多了之后靠滚动找不现实：标题过滤，纯前端、不打扰后端。 -->
        <a-input
          v-if="conversations.length"
          v-model:value="convKeyword"
          class="conv-search"
          size="small"
          allow-clear
          placeholder="搜索会话"
        >
          <template #prefix><SearchOutlined /></template>
        </a-input>
        <!-- 首次加载先给骨架行，避免空态一闪而过。 -->
        <div v-if="!convsLoaded && !conversations.length" class="conv-list">
          <a-skeleton
            v-for="i in 4"
            :key="i"
            active
            class="conv-skeleton"
            :title="{ width: '75%' }"
            :paragraph="false"
          />
        </div>
        <a-empty v-else-if="!conversations.length" description="还没有会话" :image="simpleEmpty" />
        <a-empty
          v-else-if="!filteredConversations.length"
          description="没有匹配的会话"
          :image="simpleEmpty"
        />
        <div v-else class="conv-list">
          <div
            v-for="conv in filteredConversations"
            :key="conv.id"
            :class="['conv-item', 'list-row', { active: conv.id === activeId }]"
            @click="selectConversation(conv.id)"
          >
            <!-- 重命名：铅笔把标题换成输入框，Enter/失焦保存、Esc 还原。 -->
            <input
              v-if="renamingId === conv.id"
              ref="renameInputRef"
              v-model="renameDraft"
              class="conv-rename"
              @click.stop
              @keydown.enter.prevent="submitRename"
              @keydown.esc.prevent="cancelRename"
              @blur="submitRename"
            />
            <div v-else class="conv-title">{{ conv.title }}</div>
            <div class="conv-actions">
              <EditOutlined
                v-if="renamingId !== conv.id"
                class="conv-action"
                @click.stop="startRename(conv)"
              />
              <a-popconfirm title="确认删除该会话？" @confirm.stop="removeConversation(conv.id)">
                <DeleteOutlined class="conv-action danger" @click.stop />
              </a-popconfirm>
            </div>
          </div>
        </div>
      </a-card>

      <!-- 消息区 -->
      <a-card class="main" size="small">
        <!-- 选择器放在正文顶部的工具栏行，而不是卡片 #title 槽里：卡片标题自带
             overflow:hidden + 省略号规则，会把下拉的底边线裁掉一截。
             四个下拉各自按需取宽、总长控制在 ~560px：之前每个都按 200px
             给，窗口稍窄就顶满整行显得挤压；现在放不下的情况靠 wrap 折行，
             而不是硬挤在一行里。 -->
        <div class="chat-toolbar">
          <a-space wrap size="small">
            <a-select
              v-model:value="activeAgentId"
              size="small"
              style="width: 150px"
              :options="agentOptions"
              :disabled="streaming"
            />
            <a-select
              v-model:value="activeModelConfigId"
              size="small"
              style="width: 160px"
              placeholder="选择模型"
              :options="modelOptions"
              :disabled="streaming"
            />
            <template v-if="needsKnowledge">
              <a-select
                :value="activeProjectId"
                size="small"
                style="width: 130px"
                placeholder="选择项目"
                :options="projectOptions"
                :disabled="streaming"
                @change="selectProject"
              />
              <a-select
                v-model:value="selectedTypeIds"
                size="small"
                mode="multiple"
                style="min-width: 140px; max-width: 220px"
                placeholder="全部知识库"
                :options="typeOptions"
                :disabled="streaming || !activeProjectId"
                :max-tag-count="2"
                allow-clear
              />
            </template>
            <span v-else-if="activeAgent" class="agent-hint">不检索知识库</span>
          </a-space>
        </div>

        <div ref="scrollEl" class="messages">
          <div v-if="!messages.length && !streaming" class="empty-state">
            <RobotOutlined class="empty-icon" />
            <div>{{ activeAgent?.description || '开始一个新的对话吧' }}</div>
          </div>

          <div v-for="msg in messages" :key="msg.key" :class="['msg', msg.role]">
            <div class="bubble">
              <!-- 助手回答按 markdown 渲染，用户输入保持纯文本：用户打的字不该
                   被当成标记语言解释，输入里一个 * 或 # 不应该改变显示效果。
                   流式中的那条走整段渲染（mermaid 围栏多半还没闭合）；落定之后
                   切分成段，mermaid 块升级成可编辑的图卡片。 -->
              <div v-if="msg.role === 'assistant'" class="content markdown">
                <div
                  v-if="msg.key === streamingBubbleKey"
                  v-html="renderCached(msg.key, msg.content)"
                />
                <template v-else>
                  <template v-for="(seg, i) in segmentsOf(msg)" :key="i">
                    <MermaidCard
                      v-if="seg.kind === 'mermaid'"
                      :source="seg.text"
                      :can-edit="!!msg.id && !streaming"
                      @save="(src) => saveDiagram(msg, i, src)"
                    />
                    <div v-else v-html="renderCached(`${msg.key}:${i}`, seg.text)" />
                  </template>
                </template>
              </div>
              <div v-else class="content" v-text="msg.content" />

              <!-- 运维写命令的确认卡片：挂在触发提议的那条助手气泡下面。 -->
              <ConfirmCard
                v-for="act in actionsOf(msg)"
                :key="act.id"
                :action="act"
                :acting="isActing(act)"
                :can-approve="auth.canOpsWrite"
                @approve="approveAction"
                @reject="rejectAction"
              />

              <!-- 复制的是原始 markdown 文本，不是渲染后的 HTML——贴到别处还能用。 -->
              <div v-if="msg.role === 'assistant' && msg.content" class="bubble-actions">
                <a-tooltip title="复制">
                  <button type="button" class="copy-btn" @click="copyMessage(msg)">
                    <CopyOutlined />
                  </button>
                </a-tooltip>
              </div>

              <div v-if="msg.failed" class="send-failed">
                发送失败
                <a-button type="link" size="small" danger @click="retryMessage(msg)">
                  重试
                </a-button>
              </div>

              <div v-if="msg.citations.length" class="citations">
                <div class="citations-title">来源</div>
                <a-tooltip
                  v-for="c in msg.citations"
                  :key="c.chunk_id"
                  :title="c.excerpt"
                  placement="top"
                >
                  <a-tag class="citation">
                    [{{ c.index }}] {{ c.filename }}
                    <span class="score">{{ c.score.toFixed(2) }}</span>
                  </a-tag>
                </a-tooltip>
              </div>
            </div>
          </div>

          <div v-if="streaming && !pendingText" class="msg assistant">
            <div class="bubble">
              <a-spin size="small" />
              <span v-if="currentStep" class="thinking">{{ currentStep }}</span>
              <span v-else class="thinking">正在思考…</span>
            </div>
          </div>
        </div>

        <div class="composer">
          <!-- 行高跟着内容走：短问题只占两行、省出消息区高度，长问题自动
               撑到 8 行后出现内部滚动，不再憋在固定 3 行里。 -->
          <a-textarea
            v-model:value="draft"
            :auto-size="{ minRows: 2, maxRows: 8 }"
            :disabled="!canSend"
            :placeholder="composerPlaceholder"
            @keydown.enter.exact="onComposerEnter"
          />
          <div class="composer-actions">
            <!-- 快捷键提示只在输入框为空时出现：开始打字的人已经不需要它了，
                 藏起来之后 textarea 右侧的留白也能收窄（见下方 padding-right）。 -->
            <span v-if="!draft" class="hint">Enter 发送，Shift + Enter 换行</span>
            <a-button v-if="streaming" danger @click="stop">停止</a-button>
            <a-button
              type="primary"
              :disabled="!canSend || !draft.trim()"
              :loading="streaming"
              @click="send"
            >
              发送
            </a-button>
          </div>
        </div>
      </a-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { message as toast, Empty } from 'ant-design-vue'
import {
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
} from '@ant-design/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import MermaidCard from '@/components/MermaidCard.vue'
import ConfirmCard from '@/components/ConfirmCard.vue'
import { useAuthStore } from '@/stores/auth'
import { createMarkdown } from '@/utils/markdown'
import { useOpsConfirm, actionFromConfirm } from '@/utils/useOpsConfirm'
import { useStreamRender } from '@/utils/streamRender'
import {
  replaceMermaidSegment,
  splitMessageSegments,
  type MessageSegment,
} from '@/utils/messageSegments'
import { copyText } from '@/utils/clipboard'
import { loadReportForChat } from '@/utils/report'
import {
  agentsApi,
  chatApi,
  knowledgeApi,
  settingsApi,
  type Agent,
  type Citation,
  type Conversation,
  type KbProject,
  type KnowledgeType,
  type ModelConfig,
  type OpsAction,
} from '@/api'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const simpleEmpty = Empty.PRESENTED_IMAGE_SIMPLE

// markdown-it 基座是全站共用的那套（html: false 安全底线 + 外链新标签页），
// 见 utils/markdown.ts；本页没有额外渲染钩子。
const md = createMarkdown()

// 流式渲染走「缓存 + 节拍器」：历史消息内容没变直接回旧 HTML，正在追加的那条
// 按 ~100ms 的节拍重解析，不再每 token 全量渲染（详见 streamRender.ts 头注）。
const { schedule, beginStream, endStream, renderCached, clearCache } = useStreamRender((text) =>
  md.render(text || ''),
)

interface Bubble {
  key: string
  /** 落库后的消息 id；流式刚结束才有。手动改图要拿它调更新接口。 */
  id?: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  /** 这条提问的回答失败过，界面上给出重试入口。 */
  failed?: boolean
}

// 正在流式追加的那条气泡的 key。它在模板里走整段 markdown 渲染（图围栏多半
// 还没闭合），finish() 清空后切换成分段渲染，mermaid 块此时才升级成图卡片。
const streamingBubbleKey = ref<string | null>(null)

// ---- 运维写命令确认 ----------------------------------------------------------
// 当前会话的全部待确认项（含已处理的），确认卡片挂在触发提议的那条助手气泡
// 下面。历史回放时按 message_id 对位；流式进行中服务端还没回填 message_id，
// 这一小段时间挂到正在流式的那条气泡上。
const actions = ref<OpsAction[]>([])
const { isActing, approve, reject } = useOpsConfirm()

function actionsOf(msg: Bubble): OpsAction[] {
  return actions.value.filter((a) =>
    a.message_id != null ? a.message_id === msg.id : msg.key === streamingBubbleKey.value,
  )
}

// 消息 → 分段的缓存：与 renderCached 同理，内容没变就不重新切分，避免每个
// token 都把全部历史消息重切一遍。
const segmentCache = new Map<string, { content: string; segments: MessageSegment[] }>()

function segmentsOf(msg: Bubble): MessageSegment[] {
  const hit = segmentCache.get(msg.key)
  if (hit && hit.content === msg.content) return hit.segments
  const segments = splitMessageSegments(msg.content)
  segmentCache.set(msg.key, { content: msg.content, segments })
  return segments
}

/** 两套渲染缓存一起清（streamRender 的 HTML 缓存 + 这里的分段缓存）。 */
function clearRenderCaches() {
  clearCache()
  segmentCache.clear()
}

const booting = ref(true)
const chatReady = ref(false)
// readiness 检查本身失败（网络抖动等）要单独讲，不能冒充「未配置模型」。
const readinessFailed = ref(false)
const embeddingReady = ref(false)
const kbEmpty = ref(true)

const agents = ref<Agent[]>([])
const activeAgentId = ref<number | undefined>(undefined)
const chatModels = ref<ModelConfig[]>([])
const activeModelConfigId = ref<number | undefined>(undefined)
const projects = ref<KbProject[]>([])
const activeProjectId = ref<number | null>(null)
const types = ref<KnowledgeType[]>([])
const selectedTypeIds = ref<number[]>([])
const conversations = ref<Conversation[]>([])
// 首屏骨架用：第一次拉取完成前不亮「还没有会话」空态。
const convsLoaded = ref(false)
const activeId = ref<number | null>(null)
const messages = ref<Bubble[]>([])

// 会话列表搜索：纯前端按标题过滤。
const convKeyword = ref('')
const filteredConversations = computed(() => {
  const keyword = convKeyword.value.trim().toLowerCase()
  if (!keyword) return conversations.value
  return conversations.value.filter((c) => c.title.toLowerCase().includes(keyword))
})

// ---- 会话重命名 ------------------------------------------------------------
// 铅笔把标题原地换成输入框；Enter/失焦保存，Esc 还原。空标题不提交。
const renamingId = ref<number | null>(null)
const renameDraft = ref('')
const renameInputRef = ref<HTMLInputElement[]>([])

function startRename(conv: Conversation) {
  renamingId.value = conv.id
  renameDraft.value = conv.title
  void nextTick(() => renameInputRef.value[0]?.select())
}

function cancelRename() {
  renamingId.value = null
}

async function submitRename() {
  const id = renamingId.value
  if (id === null) return
  const title = renameDraft.value.trim()
  const conv = conversations.value.find((c) => c.id === id)
  renamingId.value = null
  // 空标题或没改动都不打扰后端。
  if (!title || !conv || title === conv.title) return
  await chatApi.updateConversation(id, { title })
  conv.title = title
}

const draft = ref('')
const streaming = ref(false)
const pendingText = ref('')
// 模型当前正在调用什么工具。ReAct 下它可能查好几轮，把这个过程露出来，
// 免得用户对着一个转圈的图标干等，不知道后台在忙什么。
const currentStep = ref('')
const scrollEl = ref<HTMLElement | null>(null)

// 每种工具调用配一句进度文案。运维工具的关键信息在 input.command / input.statement
// 里（检索的在 input.query），统一从这里取，别在各处重复 if-else。
function stepText(tool: string, input?: Record<string, unknown>): string {
  const arg = (key: string) => {
    const value = input?.[key]
    return typeof value === 'string' && value.length <= 80 ? value : ''
  }
  switch (tool) {
    case 'search_knowledge_base':
      return `正在检索：${arg('query')}`
    case 'current_datetime':
      return '正在查询当前时间…'
    case 'list_servers':
      return '正在查看服务器列表…'
    case 'run_server_command':
      return `正在服务器上执行：${arg('command')}`
    case 'list_databases':
      return '正在查看数据库列表…'
    case 'run_database_query':
      return '正在执行只读查询…'
    case 'propose_command':
      return `正在提议执行：${arg('command')}`
    default:
      return '正在调用工具…'
  }
}

let abort: (() => void) | null = null

const activeAgent = computed(
  () => agents.value.find((a) => a.id === activeAgentId.value) || null,
)
// 选择器只列「在对话中可选」的；activeAgent 仍从全量里找，
// 好让绑定了隐藏智能体（运维专家这类后台人设）的老会话还能显示出名字。
const agentOptions = computed(() =>
  agents.value.filter((a) => a.chat_visible).map((a) => ({ value: a.id, label: a.name })),
)
const needsKnowledge = computed(() => activeAgent.value?.use_knowledge ?? false)

// 只有测通过的模型才真的能用，其余的摆进下拉里只会换来发送时一条报错。
const usableModels = computed(() => chatModels.value.filter((m) => m.last_test_ok))
const modelOptions = computed(() =>
  usableModels.value.map((m) => ({ value: m.id, label: m.title })),
)
const defaultModelConfigId = computed(
  () => usableModels.value.find((m) => m.is_default)?.id ?? usableModels.value[0]?.id,
)

const projectOptions = computed(() =>
  projects.value.map((p) => ({ value: p.id, label: p.name })),
)
const typeOptions = computed(() =>
  types.value.map((t) => ({ value: t.id, label: t.name })),
)

const canSend = computed(() => {
  if (!chatReady.value || streaming.value) return false
  // 依赖知识库的智能体如果没法检索，回答永远只能是「没找到」。
  if (needsKnowledge.value && !embeddingReady.value) return false
  // 后端对没指定范围的知识库提问是直接拒绝的，而不会去搜索所有项目；
  // 在这里就禁用发送，等于在请求发出前把这件事讲明白。
  if (needsKnowledge.value && !activeProjectId.value) return false
  return true
})

const composerPlaceholder = computed(() => {
  if (!chatReady.value) return '请先配置对话模型'
  if (needsKnowledge.value && !embeddingReady.value) return '请先配置向量模型'
  if (needsKnowledge.value && !activeProjectId.value) return '请先选择项目'
  return '输入你的问题…'
})

// ---- 加载 ------------------------------------------------------------------

async function loadReadiness() {
  try {
    const status = await settingsApi.status()
    chatReady.value = status.chat.ready
    embeddingReady.value = status.embedding.ready
    kbEmpty.value = status.kb.doc_count === 0
    readinessFailed.value = false
  } catch {
    // 检查失败 ≠ 没配置：亮「检查失败可重试」的 gate，而不是「未配置模型」。
    readinessFailed.value = true
  }
}

async function loadAgents() {
  const data = await agentsApi.list(true)
  agents.value = data.items
  if (!activeAgentId.value) {
    // 默认选第一个「在对话中可选」的；一个都没有就空着，发送时会提示。
    activeAgentId.value = agents.value.find((a) => a.chat_visible)?.id
  }
}

async function loadChatModels() {
  try {
    const data = await settingsApi.listModels('chat')
    chatModels.value = data.items
    if (!activeModelConfigId.value || !usableModels.value.some((m) => m.id === activeModelConfigId.value)) {
      activeModelConfigId.value = defaultModelConfigId.value
    }
  } catch {
    chatModels.value = []
  }
}

async function loadProjects() {
  try {
    const data = await knowledgeApi.listProjects()
    projects.value = data.items
    if (!projects.value.some((p) => p.id === activeProjectId.value)) {
      activeProjectId.value = projects.value[0]?.id ?? null
    }
    await loadTypes()
  } catch {
    projects.value = []
  }
}

async function loadTypes() {
  const projectId = activeProjectId.value
  if (!projectId) {
    types.value = []
    selectedTypeIds.value = []
    return
  }
  try {
    const data = await knowledgeApi.listTypes(projectId)
    types.value = data.items
    // 只保留当前项目下还存在的类型；来自别的项目的 id 会让检索范围变成空，
    // 结果就是什么都查不到还不报错。
    const alive = new Set(types.value.map((t) => t.id))
    selectedTypeIds.value = selectedTypeIds.value.filter((id) => alive.has(id))
  } catch {
    types.value = []
  }
}

function selectProject(id: number) {
  activeProjectId.value = id
  selectedTypeIds.value = []
  void loadTypes()
}

async function loadConversations() {
  try {
    const data = await chatApi.listConversations()
    conversations.value = data.items
  } finally {
    // 只决定首屏骨架何时撤掉；失败也撤，空态和报错提示接管。
    convsLoaded.value = true
  }
}

// 切换会话的竞态守卫：快速连点时慢响应不得覆盖新选择。
// 序号管「后发起的覆盖先发起的」，id 比对管「等响应期间又切走了」。
let convLoadSeq = 0

async function selectConversation(id: number) {
  if (streaming.value) return
  const seq = ++convLoadSeq
  activeId.value = id
  // 草稿不跨会话带过去：那是写给上一个人的话。
  draft.value = ''
  clearRenderCaches()
  let data
  let acts
  try {
    // 确认卡片与消息一起回放：普通会话的 actions 恒为空列表，不多一次请求。
    ;[data, acts] = await Promise.all([chatApi.listMessages(id), chatApi.listActions(id)])
  } catch {
    // 拦截器已经提示过了；这里只要不把过期/失败的结果应用上去。
    return
  }
  if (seq !== convLoadSeq || id !== activeId.value) return
  messages.value = data.items.map((m) => ({
    key: `m${m.id}`,
    id: m.id,
    role: m.role,
    content: m.content,
    citations: m.citations,
  }))
  actions.value = acts.items
  const conv = conversations.value.find((c) => c.id === id)
  if (conv?.agent_id) activeAgentId.value = conv.agent_id
  // 重新打开会话时要恢复它当初依据的检索范围，而不是沿用最后一次选的那个 ——
  // 否则接着追问，答案会是从另一批文档里找出来的。模型也是同样的道理：
  // 会话记住的那份还在就切回去，被删掉了就退回默认。
  if (conv?.project_id && conv.project_id !== activeProjectId.value) {
    activeProjectId.value = conv.project_id
    await loadTypes()
    // loadTypes 又等了一轮网络，期间可能再次切换，应用前复查。
    if (seq !== convLoadSeq || id !== activeId.value) return
  }
  selectedTypeIds.value = conv?.type_ids ? [...conv.type_ids] : []
  activeModelConfigId.value =
    conv?.model_config_id && usableModels.value.some((m) => m.id === conv.model_config_id)
      ? conv.model_config_id
      : defaultModelConfigId.value
  scrollToBottom()
}

function startNew() {
  if (streaming.value) return
  // 让还在路上的 selectConversation 响应失效，别把旧会话的消息灌进新会话。
  convLoadSeq++
  activeId.value = null
  messages.value = []
  actions.value = []
  draft.value = ''
  clearRenderCaches()
}

async function removeConversation(id: number) {
  await chatApi.deleteConversation(id)
  toast.success('已删除')
  if (activeId.value === id) startNew()
  await loadConversations()
}

// ---- 基于报告发起对话 ---------------------------------------------------------
// 报告页（ReportView）「发起对话」新开标签页进 /customer-service?report=<kind>/<id>。
// query 只传引用不传全文：报告全文由这里重新拉取（noopener 下拿不到 opener 的
// sessionStorage，URL 也塞不下几千字），数据以后端为准。

const REPORT_QUERY_RE = /^(resume|comparison|toolkit)\/(\d+)$/
/** 首条消息上限与后端 CompletionRequest.message 的 20000 对齐。 */
const REPORT_MESSAGE_LIMIT = 20000
/** 报告讨论的默认智能体（内置，backend/services/agents.py 里 seed）。
 *  找不到（后端没重启 seed、或被用户停用）就退回当前选中的智能体。 */
const REPORT_DISCUSS_AGENT_SLUG = 'career-advisor'

async function handleReportDiscuss() {
  const raw = route.query.report
  const match = REPORT_QUERY_RE.exec(typeof raw === 'string' ? raw : '')
  // 垃圾参数静默忽略——与路由 \d+ 约束「不进页面」同一哲学。
  if (!match) return

  // 先清 query 再干活：即使后面任何一步失败，刷新这个标签页都不会重发一遍。
  void router.replace({ query: {} })

  let title: string
  let markdown: string
  try {
    ;({ title, markdown } = await loadReportForChat(match[1], Number(match[2])))
  } catch {
    // 拦截器已经弹过错误提示；query 已清，页面停在普通空白会话态即可。
    return
  }

  let firstMessage = `以下是报告《${title}》的完整内容，请先阅读，稍后我会基于它向你提问。\n\n---\n\n${markdown}`
  if (firstMessage.length > REPORT_MESSAGE_LIMIT) {
    // 防御后端哪天再收紧上限：宁可截断并明说，也不让 AI 悄悄基于残缺报告作答。
    firstMessage = `${firstMessage.slice(0, REPORT_MESSAGE_LIMIT)}\n\n（报告内容过长，已按长度上限截断）`
  }

  // 报告讨论默认用「求职顾问」：要在 canSend 判断之前切换，gate 检查
  // （比如知识库项目）针对的才是这场对话真正要用的智能体。messages 还是空的，
  // watch(activeAgentId) 的 startNew 保护不会误触发。
  const advisor = agents.value.find(
    (a) => a.slug === REPORT_DISCUSS_AGENT_SLUG && a.chat_visible,
  )
  if (advisor) activeAgentId.value = advisor.id

  if (!canSend.value) {
    // 没配模型这类 gate 场景：不建孤儿空会话，内容放输入框，用户按 gate 提示
    // 配好后点发送即可，内容不丢。
    draft.value = firstMessage
    toast.warning('报告内容已放入输入框，按上方提示完成配置后即可发送')
    return
  }

  startNew()
  // 显式建会话而不是让首条消息触发自动建：自动建的标题是从首问截 40 字
  // （derive_title），首问是报告全文时标题会是一截报告正文。
  const rawTitle = `报告讨论 · ${title}`
  const convTitle = rawTitle.length <= 50 ? rawTitle : `${rawTitle.slice(0, 50)}…`
  let conv
  try {
    conv = await chatApi.createConversation({
      title: convTitle,
      agent_id: activeAgentId.value,
      model_config_id: activeModelConfigId.value,
      // 与 send() 同一规则：只有依赖知识库的智能体才带检索范围。
      project_id: needsKnowledge.value ? activeProjectId.value ?? undefined : undefined,
      type_ids:
        needsKnowledge.value && selectedTypeIds.value.length
          ? [...selectedTypeIds.value]
          : undefined,
    })
  } catch {
    // 拦截器已弹错；内容放输入框，用户可手动重试。
    draft.value = firstMessage
    return
  }
  // 不走 selectConversation：新会话的消息/actions 必然为空（白拉两次），它还会
  // 重置 agent/模型选择——而我们正是按当前选择建的会话，重置是绕回原点的空转。
  conversations.value.unshift(conv)
  activeId.value = conv.id
  draft.value = firstMessage
  send()
}

// ---- 发送 ------------------------------------------------------------------

function onComposerEnter(e: KeyboardEvent) {
  // 中文输入法里按 Enter 是选词、不是发送：这种 keydown 的 isComposing 为 true。
  // 不拦的话消息会被提前发出去，随后输入法把整段文字回填进输入框，
  // 看起来就像「发出去了但没清空」。preventDefault 也只在真正发送时才调，
  // 否则会把选词那一次 Enter 吞掉。
  if (e.isComposing) return
  e.preventDefault()
  send()
}

function send() {
  const question = draft.value.trim()
  if (!question || !canSend.value) return

  const userBubble: Bubble = {
    key: `u${Date.now()}`,
    role: 'user',
    content: question,
    citations: [],
  }
  messages.value.push(userBubble)
  draft.value = ''
  scrollToBottom()

  streaming.value = true
  pendingText.value = ''

  const bubble: Bubble = {
    key: `a${Date.now()}`,
    role: 'assistant',
    content: '',
    citations: [],
  }
  // 本轮流里提议的待确认项：done 时统一回填 message_id。
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key

  abort = chatApi.streamCompletion(
    {
      message: question,
      conversation_id: activeId.value ?? undefined,
      agent_id: activeAgentId.value,
      model_config_id: activeModelConfigId.value,
      // 只有依赖知识库的智能体才需要传；其他智能体压根不做检索。
      project_id: needsKnowledge.value ? activeProjectId.value ?? undefined : undefined,
      // 空数组表示「整个项目」，后端会当作没传来处理。
      type_ids:
        needsKnowledge.value && selectedTypeIds.value.length
          ? [...selectedTypeIds.value]
          : undefined,
    },
    {
      onMeta: ({ conversation_id }) => {
        activeId.value = conversation_id
      },
      onCitations: (citations) => {
        bubble.citations = citations
      },
      onStep: ({ tool, input }) => {
        // 模型每发起一次工具调用就换一行状态。它可能会查好几轮，只显示当前
        // 这一轮：这是个进度提示，不是操作日志。
        currentStep.value = stepText(tool, input)
      },
      onToken: (token) => {
        // 第一个 token 一到就说明它不查了、开始作答了。
        currentStep.value = ''
        // bubble 要等第一个 token 到了才 push 进去，这样请求失败时也不会
        // 在界面上留下一条空的助手消息。
        if (!pendingText.value) messages.value.push(bubble)
        pendingText.value += token
        bubble.content = pendingText.value
        schedule()
        scrollToBottom()
      },
      onConfirm: (data) => {
        // 模型提议写命令后可能直接结束本轮、一个 token 都不吐：先保证
        // 气泡存在，确认卡片才有地方挂。
        if (!messages.value.includes(bubble)) messages.value.push(bubble)
        const action = actionFromConfirm(data, activeId.value)
        actions.value.push(action)
        streamActions.push(action)
        scrollToBottom()
      },
      onDone: ({ message_id }) => {
        // 记下落库 id，这条气泡里的图才可以手动改（改完要能回写到这条消息）。
        bubble.id = message_id
        // 回填本轮提议的 message_id：卡片从「流式气泡」改挂到落库消息上，
        // 与历史回放的定位方式一致。
        for (const a of streamActions) a.message_id = message_id
        finish()
        void loadConversations()
      },
      onError: ({ message }) => {
        toast.error(message)
        // 提问还在、回答没落成：挂上「可重试」标记，别让用户重新打一遍。
        userBubble.failed = true
        finish()
      },
    },
  )
}

// 重试一条失败的提问：后端会先把库里那条悬空的 user 消息删掉再重新回答，
// 历史里不会出现重复提问（见 routers/chat.py 的 retry 端点）。
function retryMessage(msg: Bubble) {
  if (streaming.value || !msg.failed) return
  const idx = messages.value.indexOf(msg)
  if (idx === -1) return

  // meta 都没收到过的失败（网络层就断了）：后端大概率没有这条记录，
  // 删掉本地气泡、把内容放回发送框重走正常发送。
  if (!activeId.value) {
    messages.value.splice(idx, 1)
    draft.value = msg.content
    send()
    return
  }

  // 清掉这条提问之后的残文（失败时已生成的半截回答），重新挂流。
  messages.value.splice(idx + 1)
  msg.failed = false

  streaming.value = true
  pendingText.value = ''

  const bubble: Bubble = {
    key: `a${Date.now()}`,
    role: 'assistant',
    content: '',
    citations: [],
  }
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key
  scrollToBottom()

  abort = chatApi.streamRetry(activeId.value, {
    onCitations: (citations) => {
      bubble.citations = citations
    },
    onStep: ({ tool, input }) => {
      currentStep.value = stepText(tool, input)
    },
    onToken: (token) => {
      currentStep.value = ''
      if (!pendingText.value) messages.value.push(bubble)
      pendingText.value += token
      bubble.content = pendingText.value
      schedule()
      scrollToBottom()
    },
    onConfirm: (data) => {
      if (!messages.value.includes(bubble)) messages.value.push(bubble)
      const action = actionFromConfirm(data, activeId.value)
      actions.value.push(action)
      streamActions.push(action)
      scrollToBottom()
    },
    onDone: ({ message_id }) => {
      bubble.id = message_id
      for (const a of streamActions) a.message_id = message_id
      finish()
      void loadConversations()
    },
    onError: ({ message }) => {
      toast.error(message)
      // 又失败了：残文清掉，提问重新挂回「可重试」。
      messages.value.splice(idx + 1)
      msg.failed = true
      finish()
    },
  })
}

// 确认一条写命令：exec 结果由 useOpsConfirm 写回卡片，这里把续答接进一个
// 新气泡——与普通回答共用同一条流式渲染管线。
function approveAction(action: OpsAction) {
  if (streaming.value) {
    // 一次只跑一条流：回答进行中发起的确认没有气泡机制可挂。
    toast.warning('请等当前回答结束后再确认')
    return
  }
  streaming.value = true
  pendingText.value = ''

  const bubble: Bubble = {
    key: `a${Date.now()}`,
    role: 'assistant',
    content: '',
    citations: [],
  }
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key
  scrollToBottom()

  abort = approve(action, {
    onExec: () => {
      // 执行结果写回卡片后滚到底，让输出块进入视野。
      scrollToBottom()
    },
    onStep: ({ tool, input }) => {
      currentStep.value = stepText(tool, input)
    },
    onCitations: (citations) => {
      bubble.citations = citations
    },
    onToken: (token) => {
      currentStep.value = ''
      if (!pendingText.value) messages.value.push(bubble)
      pendingText.value += token
      bubble.content = pendingText.value
      schedule()
      scrollToBottom()
    },
    onConfirm: (data) => {
      // 续答里模型又提议了新的写命令：挂到续答气泡下，同一条管线。
      if (!messages.value.includes(bubble)) messages.value.push(bubble)
      const act = actionFromConfirm(data, activeId.value)
      actions.value.push(act)
      streamActions.push(act)
      scrollToBottom()
    },
    onDone: ({ message_id }) => {
      bubble.id = message_id
      for (const a of streamActions) a.message_id = message_id
      finish()
      void loadConversations()
    },
    onError: ({ message }) => {
      toast.error(message)
      finish()
    },
  })
}

function rejectAction(action: OpsAction) {
  void reject(action)
}

function stop() {
  abort?.()
  finish()
}

function finish() {
  streaming.value = false
  pendingText.value = ''
  currentStep.value = ''
  abort = null
  streamingBubbleKey.value = null
  // 收尾推一次全量渲染：停止/出错时残文也要以完整 markdown 落定。
  endStream()
}

function scrollToBottom() {
  void nextTick(() => {
    const el = scrollEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function copyMessage(msg: Bubble) {
  if (await copyText(msg.content)) toast.success('已复制')
  else toast.error('复制失败，浏览器拒绝了剪贴板访问')
}

// 手动改图落库：替换消息里第 segIndex 个图段的源码并整体回写。历史回放给
// 模型的就是这条消息的内容，所以保存之后，下一轮提问 AI 看到的就是改后的图。
async function saveDiagram(msg: Bubble, segIndex: number, newSource: string) {
  if (!msg.id) return
  const content = replaceMermaidSegment(msg.content, segIndex, newSource)
  try {
    await chatApi.updateMessage(msg.id, content)
  } catch {
    // 拦截器已经弹过错误提示；本地内容保持原样即可。
    return
  }
  msg.content = content
  clearRenderCaches()
  toast.success('已保存，后续对话将基于你修改后的图')
}

watch(activeAgentId, () => {
  // 聊到一半换智能体，会让两个人设的发言混在同一段历史里。
  if (!streaming.value && messages.value.length) startNew()
})

onMounted(async () => {
  try {
    await Promise.all([
      loadReadiness(),
      loadAgents(),
      loadChatModels(),
      loadConversations(),
      loadProjects(),
    ])
  } finally {
    booting.value = false
  }
  // 报告页「发起对话」带着 ?report=kind/id 进这里：能不能直接发送取决于
  // 模型/智能体/项目是否就绪，所以要等上面全部加载完再处理。
  await handleReportDiscuss()
})

onUnmounted(() => abort?.())
</script>

<style scoped>
.chat-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.gate-alert {
  margin-bottom: 12px;
}
.chat-body {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  flex: 1;
  min-height: 0;
}
/* 窄屏上下堆叠：会话列表限高可滚，消息区吃剩余高度。 */
@media (max-width: 900px) {
  .chat-body {
    grid-template-columns: 1fr;
    grid-template-rows: auto 1fr;
  }
  .side {
    max-height: 200px;
  }
}
.side,
.main {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.side :deep(.ant-card-body),
.main :deep(.ant-card-body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.conv-list {
  overflow-y: auto;
}
.conv-search {
  margin-bottom: 8px;
}
/* 骨架行的内边距对齐真实会话项，加载完不跳。 */
.conv-skeleton {
  padding: 8px 10px;
}
/* 发送失败的提问下方给重试入口（气泡内，右对齐跟随用户消息）。 */
.send-failed {
  margin-top: 6px;
  font-size: 12px;
  color: #ff4d4f;
  text-align: right;
}
/* hover/active 底色用全局 .list-row（style.css），这里只留布局。 */
.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
}
.conv-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
/* 操作图标（重命名/删除）只在悬停/键盘焦点进入该行时出现：常显的话每行
   都挂一排图标，列表全是噪音。保留占位（不 display:none），避免悬停时
   标题被挤宽。 */
.conv-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s;
}
.conv-item:hover .conv-actions,
.conv-item:focus-within .conv-actions {
  opacity: 1;
}
.conv-action {
  color: var(--text-3);
  transition: color 0.15s;
}
.conv-action:hover {
  color: var(--signal-text);
}
.conv-action.danger:hover {
  color: #ff4d4f;
}
/* 原地重命名的输入框：与标题同字号同行高，切换时不跳。 */
.conv-rename {
  flex: 1;
  min-width: 0;
  padding: 0 4px;
  border: 1px solid var(--signal-border);
  border-radius: 4px;
  font-size: 13px;
  line-height: inherit;
  outline: none;
}
/* 工具栏行：承担原来卡片标题的角色，规格对齐左侧卡片头部（上 8 + 控件 24
   + 下 8 + 1px 线），负 margin 顶到卡片边缘让分隔线通栏——两张卡片的
   分隔线才能落在同一水平线上。 */
.chat-toolbar {
  margin: -12px -12px 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--hairline);
}
.agent-hint {
  margin-left: 10px;
  font-size: 12px;
  font-weight: normal;
  color: var(--text-3);
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 4px 4px 12px;
}
.empty-state {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--text-3);
}
/* 空态是大片留白里的唯一视觉锚点：放大并给一点信号色，不然在白底上
   完全没存在感。 */
.empty-icon {
  font-size: 48px;
  color: var(--signal-text);
}
.msg {
  display: flex;
  margin-bottom: 14px;
}
.msg.user {
  justify-content: flex-end;
}
.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--paper);
  line-height: 1.7;
}
.msg.user .bubble {
  background: var(--signal-bg-strong);
}
.content {
  white-space: pre-wrap;
  word-break: break-word;
}
/* markdown 排版在全局 assets/markdown.css（.markdown）；这里只留 scoped
   才能打赢的覆盖：.content 的 pre-wrap 会叠到块级元素上，多出一堆空行。 */
.content.markdown {
  white-space: normal;
}
/* 气泡操作行（复制）：悬停整条消息才出现，常显会让每段回答都挂一排图标。 */
.bubble-actions {
  display: flex;
  margin-top: 6px;
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
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-3);
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s;
}

.copy-btn:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--signal-text);
}

.citations {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed var(--hairline);
}
.citations-title {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 6px;
}
.citation {
  margin-bottom: 4px;
  cursor: help;
}
.score {
  color: var(--text-3);
  margin-left: 4px;
}
.thinking {
  margin-left: 8px;
  color: var(--text-3);
}
.composer {
  position: relative;
  border-top: 1px solid #f0f0f0;
  padding-top: 12px;
}
/* 按钮和提示悬浮在输入框右下角，不再单独占一行；给 textarea 右侧留出
   空隙，避免输入的长文本钻到按钮底下。打字时提示已隐藏、只剩发送按钮，
   留白只需盖住按钮；流式期间出现的「停止」按钮更宽，但那时输入框是
   禁用态、不会有文本和它重叠。 */
.composer :deep(.ant-input) {
  padding-right: 110px;
}
.composer-actions {
  position: absolute;
  right: 12px;
  bottom: 10px;
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
