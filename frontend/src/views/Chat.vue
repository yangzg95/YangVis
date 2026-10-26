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
        <a-button size="small" type="primary" @click="router.push('/system/settings')">
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
        <a-button size="small" type="primary" @click="router.push('/system/settings')">
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
              @change="onAgentChange"
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
            <!-- 会话级可选工具：本地方法（后端代码注册）+ 挂载的 MCP 服务，
                 两个来源都为空时整个选择器不渲染。 -->
            <a-select
              v-if="localToolOptions.length || mcpToolOptions.length"
              v-model:value="toolSelection"
              size="small"
              mode="multiple"
              style="min-width: 150px; max-width: 260px"
              placeholder="选择工具"
              :max-tag-count="2"
              allow-clear
              :disabled="streaming"
            >
              <a-select-opt-group v-if="localToolOptions.length" label="本地工具">
                <a-select-option
                  v-for="opt in localToolOptions"
                  :key="opt.value"
                  :value="opt.value"
                >
                  {{ opt.label }}
                </a-select-option>
              </a-select-opt-group>
              <a-select-opt-group v-if="mcpToolOptions.length" label="MCP 服务">
                <a-select-option
                  v-for="opt in mcpToolOptions"
                  :key="opt.value"
                  :value="opt.value"
                >
                  {{ opt.label }}
                </a-select-option>
              </a-select-opt-group>
            </a-select>
            <!-- 手动压缩对话：更早的消息压成摘要、不再进入模型上下文。
                 不足两对消息时压缩无意义，后端也会拒，直接在入口禁用。 -->
            <a-button
              size="small"
              :disabled="streaming || messages.length < 4"
              @click="openCompaction"
            >
              <template #icon><CompressOutlined /></template>
              压缩对话
            </a-button>
          </a-space>
        </div>

        <div ref="scrollEl" class="messages" @scroll="onMessagesScroll">
          <div v-if="!messages.length && !streaming" class="empty-state">
            <RobotOutlined class="empty-icon" />
            <div>{{ activeAgent?.description || '开始一个新的对话吧' }}</div>
          </div>

          <!-- 压缩分界卡：手动压缩后锚点前的消息在本视图隐藏，用这张卡交代
               去向并可展开摘要原文；自动滚动摘要也在此露出一条。 -->
          <div v-if="compaction" class="compaction-bar">
            <button
              type="button"
              class="compaction-head"
              @click="summaryExpanded = !summaryExpanded"
            >
              <CompressOutlined class="compaction-icon" />
              <span class="compaction-title">
                <template v-if="compaction.cut_message_id != null">
                  已压缩 {{ compaction.covered_count }} 条更早对话（不再进入上下文，原文保留）
                </template>
                <template v-else>
                  更早对话的滚动摘要（覆盖 {{ compaction.covered_count }} 条）
                </template>
              </span>
              <span class="compaction-toggle">{{ summaryExpanded ? '收起' : '查看摘要' }}</span>
            </button>
            <div v-if="summaryExpanded" class="compaction-detail">
              <div
                class="compaction-summary markdown"
                v-html="renderCached(`compaction:${activeId}`, compaction.summary)"
              />
              <div v-if="compaction.instruction" class="compaction-instruction">
                压缩要求：{{ compaction.instruction }}
              </div>
              <a-popconfirm
                v-if="compaction.cut_message_id != null"
                title="取消压缩后，更早的消息将重新进入回放窗口，确认？"
                ok-text="取消压缩"
                cancel-text="再想想"
                @confirm="clearCompaction"
              >
                <a-button size="small" type="link" danger>取消压缩</a-button>
              </a-popconfirm>
            </div>
          </div>

          <div v-for="msg in visibleMessages" :key="msg.key" :class="['msg', msg.role]">
            <div class="bubble">
              <!-- 思考与工具调用过程：流式中实时展示，落定后折叠，历史消息
                   从 timeline 重建。纯问答消息时间线为空，不占位置。 -->
              <ThinkingTrace
                v-if="msg.role === 'assistant'"
                :entries="msg.timeline ?? []"
                :streaming="msg.key === streamingBubbleKey"
              />
              <!-- 助手回答按 markdown 渲染，用户输入保持纯文本：用户打的字不该
                   被当成标记语言解释，输入里一个 * 或 # 不应该改变显示效果。
                   流式中的那条走整段渲染（mermaid 围栏多半还没闭合）；落定之后
                   切分成段，mermaid 块升级成可编辑的图卡片。
                   只有思考过程、还没吐正文时不渲染空的正文容器。 -->
              <div v-if="msg.role === 'assistant' && msg.content" class="content markdown">
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
              <div v-else class="content">
                <!-- 附件挂在提问上方：图片给缩略图（点开可看原图），文档只露
                     文件名。只传附件不写字时，气泡里就只有附件。 -->
                <div v-if="msg.attachments?.length" class="msg-attachments">
                  <template v-for="att in msg.attachments" :key="att.id ?? att.filename">
                    <a-image
                      v-if="att.kind === 'image' && att.url"
                      :src="att.url"
                      :width="140"
                      class="msg-image"
                    />
                    <span v-else class="msg-file">
                      <FileOutlined />
                      <span class="msg-file-name" :title="att.filename">{{ att.filename }}</span>
                    </span>
                  </template>
                </div>
                <div v-if="msg.content" v-text="msg.content" />
              </div>

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

          <!-- 第一个过程事件（思考/工具/正文）到达前的占位；事件一到，过程就
               挂到助手气泡自身的时间线上，这个独立占位随即消失。 -->
          <div v-if="streaming && !streamBubbleShown" class="msg assistant">
            <div class="bubble">
              <a-spin size="small" />
              <span class="thinking">正在思考…</span>
            </div>
          </div>

          <!-- 回到底部：内容末尾的 sticky 元素，上翻时钉在滚动区底沿，
               不随消息滚走。点击后恢复贴底跟随。 -->
          <transition name="jump-fade">
            <button
              v-if="!stickToBottom"
              type="button"
              class="jump-bottom"
              title="回到底部"
              @click="pinToBottom"
            >
              <VerticalAlignBottomOutlined />
            </button>
          </transition>
        </div>

        <div class="composer">
          <!-- 深链带入的简历上下文：先挂起不自动发，用户在上方选好智能体/模型/工具、
               写好第一个问题再发送；发送时才回源拉最新版并建会话。 -->
          <div v-if="pendingContext" class="context-bar">
            <FileTextOutlined class="context-icon" />
            <div class="context-meta">
              <span class="context-title">
                已附带在线简历《{{ pendingContext.title }}》（约 {{ pendingContext.charCount }} 字）
              </span>
              <span class="context-sub">
                将随第一条消息发送；发送前可在上方选择智能体、模型与工具，直接发送则默认让
                AI 通读分析
              </span>
            </div>
            <button
              type="button"
              class="context-remove"
              title="移除简历上下文"
              :disabled="contextLaunching"
              @click="pendingContext = null"
            >
              <CloseOutlined />
            </button>
          </div>
          <!-- 上传区：回形针常驻，待发附件以缩略图/文件名条展示在上方。
               图片能否发给当前模型由 attachmentGate 在前端先拦一道。 -->
          <div class="attach-bar">
            <a-tooltip :title="`上传文件或图片（最多 ${MAX_ATTACHMENTS} 个）`">
              <button
                type="button"
                class="attach-btn"
                :disabled="!canSend || pendingAttachments.length >= MAX_ATTACHMENTS"
                @click="openFilePicker"
              >
                <PaperClipOutlined />
              </button>
            </a-tooltip>
            <span class="attach-count" v-if="pendingAttachments.length">
              {{ pendingAttachments.length }}/{{ MAX_ATTACHMENTS }}
            </span>
            <span v-if="attachmentGate" class="attach-warn">{{ attachmentGate }}</span>
          </div>
          <input
            ref="fileInput"
            type="file"
            multiple
            class="file-input"
            accept=".pdf,.docx,.txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.log,.yaml,.yml,.xml,.toml,.ini,.conf,.properties,.env,.sql,.py,.js,.mjs,.cjs,.ts,.tsx,.jsx,.vue,.java,.kt,.go,.rs,.c,.h,.cpp,.hpp,.cs,.php,.rb,.sh,.bash,.zsh,.bat,.ps1,.html,.htm,.css,.scss,.less,.png,.jpg,.jpeg,.gif,.webp,.bmp"
            @change="onFilesPicked"
          />
          <div v-if="pendingAttachments.length" class="pending-list">
            <div
              v-for="att in pendingAttachments"
              :key="att.key"
              :class="['pending-item', { 'is-error': att.status === 'error' }]"
            >
              <img
                v-if="att.kind === 'image' && att.previewUrl"
                :src="att.previewUrl"
                class="pending-thumb"
                alt=""
              />
              <span v-else class="pending-file-icon"><FileOutlined /></span>
              <span class="pending-meta">
                <span class="pending-name" :title="att.filename">{{ att.filename }}</span>
                <span class="pending-sub">
                  <a-spin v-if="att.status === 'uploading'" size="small" />
                  <template v-else-if="att.status === 'error'">
                    {{ att.error || '上传失败' }}
                  </template>
                  <template v-else>{{ formatSize(att.size) }}</template>
                </span>
              </span>
              <button
                type="button"
                class="pending-remove"
                title="移除"
                @click="removePending(att)"
              >
                <CloseOutlined />
              </button>
            </div>
          </div>
          <!-- 行高跟着内容走：短问题只占两行、省出消息区高度，长问题自动
               撑到 8 行后出现内部滚动，不再憋在固定 3 行里。 -->
          <a-textarea
            ref="composerRef"
            v-model:value="draft"
            :auto-size="{ minRows: 2, maxRows: 8 }"
            :disabled="!canSend"
            :placeholder="composerPlaceholder"
            @keydown.enter.exact="onComposerEnter"
            @paste="onComposerPaste"
            @compositionstart="onComposerCompositionStart"
            @compositionend="onComposerCompositionEnd"
          />
          <div class="composer-actions">
            <!-- 快捷键提示只在输入框为空时出现：开始打字的人已经不需要它了，
                 藏起来之后 textarea 右侧的留白也能收窄（见下方 padding-right）。 -->
            <span v-if="!draft" class="hint">Enter 发送，Shift + Enter 换行</span>
            <a-button v-if="streaming" danger @click="stop">停止</a-button>
            <a-button
              type="primary"
              :disabled="
                !canSend ||
                attachmentsBusy ||
                contextLaunching ||
                (!draft.trim() && !readyAttachments.length && !pendingContext)
              "
              :loading="streaming || contextLaunching"
              @click="send"
            >
              发送
            </a-button>
          </div>
        </div>
      </a-card>

      <!-- 手动压缩向导：选保留轮数与压缩要求 → 流式预览摘要 → 确认后应用。
           预览全程不写库，应用是独立请求；关掉弹窗即放弃预览并掐断流。 -->
      <a-modal
        v-model:open="compactModalOpen"
        title="压缩对话"
        :width="620"
        :mask-closable="false"
        ok-text="应用压缩"
        cancel-text="取消"
        :confirm-loading="compactApplying"
        :ok-button-props="{ disabled: compactBusy || !compactPreview.trim() }"
        @ok="applyCompactionFromModal"
        @cancel="closeCompaction"
      >
        <div class="compact-form">
          <div class="compact-row">
            <span>保留最近</span>
            <a-input-number
              v-model:value="compactKeepTurns"
              :min="COMPACT_KEEP_TURNS_MIN"
              :max="COMPACT_KEEP_TURNS_MAX"
              :disabled="compactBusy"
            />
            <span>轮对话，更早的内容压成摘要</span>
          </div>
          <a-textarea
            v-model:value="compactInstruction"
            :rows="2"
            :maxlength="COMPACT_INSTRUCTION_MAX"
            show-count
            :disabled="compactBusy"
            placeholder="压缩要求（可选），如：保留未决错误、文件路径与结论"
          />
          <div class="compact-preview-head">
            <a-button
              size="small"
              :loading="compactBusy"
              :disabled="streaming"
              @click="runCompactPreview"
            >
              {{ compactPreview ? '重新生成' : '生成预览' }}
            </a-button>
            <a-button v-if="compactBusy" size="small" @click="stopCompactPreview">
              停止
            </a-button>
            <span v-if="compactMeta" class="compact-meta">
              将压缩 {{ compactMeta.covered_count }} 条，保留 {{ compactMeta.kept_count }} 条
            </span>
          </div>
          <div v-if="compactBusy || compactPreview" class="compact-preview-wrap">
            <div
              class="compact-preview markdown"
              v-html="renderCached('compact-preview', compactPreview)"
            />
            <span v-if="compactBusy" class="compact-caret" />
          </div>
          <a-alert
            v-else
            type="info"
            show-icon
            message="摘要生成后可在此预览，确认应用才会生效；更早的消息原文不会删除。"
          />
        </div>
      </a-modal>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { message as toast, Empty } from 'ant-design-vue'
import {
  CloseOutlined,
  CompressOutlined,
  CopyOutlined,
  DeleteOutlined,
  EditOutlined,
  FileOutlined,
  FileTextOutlined,
  PaperClipOutlined,
  PlusOutlined,
  RobotOutlined,
  SearchOutlined,
  VerticalAlignBottomOutlined,
} from '@ant-design/icons-vue'
import { useRoute, useRouter } from 'vue-router'
import MermaidCard from '@/components/MermaidCard.vue'
import ConfirmCard from '@/components/ConfirmCard.vue'
import ThinkingTrace from '@/components/ThinkingTrace.vue'
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
import { registerToolLabels } from '@/utils/chatTools'
import {
  agentsApi,
  chatApi,
  knowledgeApi,
  mcpApi,
  resumeBuilderApi,
  settingsApi,
  toolsApi,
  type Agent,
  type ChatAttachment,
  type Citation,
  type CompactMeta,
  type CompactionState,
  type Conversation,
  type KbProject,
  type KnowledgeType,
  type LocalTool,
  type McpServer,
  type ModelConfig,
  type OpsAction,
  type StreamHandlers,
  type TimelineEntry,
} from '@/api'
import { resumeDocHasContent, resumeDocToMarkdown } from '@/utils/resumeDocMarkdown'

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
  /** 思考与工具调用的交错时间线；用户消息恒无此字段，纯问答也为空。 */
  timeline?: TimelineEntry[]
  /** 用户提问时带的附件；助手消息恒为空。 */
  attachments?: ChatAttachment[]
  /** 这条提问的回答失败过，界面上给出重试入口。 */
  failed?: boolean
}

// 正在流式追加的那条气泡的 key。它在模板里走整段 markdown 渲染（图围栏多半
// 还没闭合），finish() 清空后切换成分段渲染，mermaid 块此时才升级成图卡片。
const streamingBubbleKey = ref<string | null>(null)
// 流式气泡是否已经进了消息列表（第一个思考/工具/正文事件到达时上线）。
// 上线之前显示独立的「正在思考…」占位，上线之后过程挂在气泡自身的时间线上。
const streamBubbleShown = computed(
  () =>
    !!streamingBubbleKey.value &&
    messages.value.some((m) => m.key === streamingBubbleKey.value),
)

// ---- 运维写命令确认 ----------------------------------------------------------
// 当前会话的全部待确认项（含已处理的），确认卡片挂在触发提议的那条助手气泡
// 下面。历史回放时按 message_id 对位；流式进行中服务端还没回填 message_id，
// 这一小段时间挂到正在流式的那条气泡上。
const actions = ref<OpsAction[]>([])
const { isActing, approve, reject } = useOpsConfirm()

// 最后一条助手气泡：无主待确认项的兜底挂载点。
const tailAssistantKey = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i -= 1) {
    if (messages.value[i].role === 'assistant') return messages.value[i].key
  }
  return null
})

function actionsOf(msg: Bubble): OpsAction[] {
  return actions.value.filter((a) => {
    if (a.message_id != null) return a.message_id === msg.id
    // message_id 没回填（整轮回答没落库就断了：停止/刷新/断线）：宁可挂到最后
    // 一条助手气泡，也不能让它谁都不匹配——这条命令在界面上根本不存在。
    return msg.key === streamingBubbleKey.value || msg.key === tailAssistantKey.value
  })
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
// 会话级可选工具目录：本地工具来自代码注册（只读目录），MCP 服务来自 /mcp 管理页。
const localTools = ref<LocalTool[]>([])
const mcpServers = ref<McpServer[]>([])
// 选择器的复合值：local:<key> / mcp:<id>。本地 key 受 ^[a-z0-9_]+$ 约束，冒号无碰撞。
const toolSelection = ref<string[]>([])
const localToolOptions = computed(() =>
  localTools.value.map((tool) => ({ label: tool.title, value: `local:${tool.key}` })),
)
// 停用的 MCP 不进选择器：选了也会在发送时被后端挡住，不如一开始就不给选。
const mcpToolOptions = computed(() =>
  mcpServers.value
    .filter((server) => server.enabled)
    .map((server) => ({ label: server.name, value: `mcp:${server.id}` })),
)
const selectedLocalToolKeys = computed(() =>
  toolSelection.value
    .filter((value) => value.startsWith('local:'))
    .map((value) => value.slice('local:'.length)),
)
const selectedMcpServerIds = computed(() =>
  toolSelection.value
    .filter((value) => value.startsWith('mcp:'))
    .map((value) => Number(value.slice('mcp:'.length))),
)
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
  try {
    await chatApi.updateConversation(id, { title })
  } catch {
    return // 拦截器已弹 toast；本地标题维持原样，下次刷新自然对齐。
  }
  conv.title = title
}

const draft = ref('')
const composerRef = ref<{ blur: () => void } | null>(null)
const streaming = ref(false)
const pendingText = ref('')
const scrollEl = ref<HTMLElement | null>(null)

// ---- 待发附件 ---------------------------------------------------------------
// 上传与发送是两次请求：先拿到服务端 id 做预览/移除，发送时只带 id 列表。
// 数量与大小上限跟后端 settings.CHAT_* 对齐，超限在这里就拦掉。
const MAX_ATTACHMENTS = 6
const MAX_FILE_BYTES = 10 * 1024 * 1024
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']
const FILE_EXTENSIONS = [
  'pdf', 'docx',
  'txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'jsonl', 'log',
  'yaml', 'yml', 'xml', 'toml', 'ini', 'conf', 'properties', 'env',
  'sql', 'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'vue',
  'java', 'kt', 'go', 'rs', 'c', 'h', 'cpp', 'hpp', 'cs',
  'php', 'rb', 'sh', 'bash', 'zsh', 'bat', 'ps1',
  'html', 'htm', 'css', 'scss', 'less',
]

interface PendingAttachment {
  /** 本地行标识：上传回来之前没有服务端 id。 */
  key: string
  id?: number
  kind: 'image' | 'file'
  filename: string
  mime: string | null
  size: number
  /** 图片预览地址：上传中为 blob URL，成功后换成服务端 URL。 */
  previewUrl?: string
  status: 'uploading' | 'ready' | 'error'
  error?: string
}

const fileInput = ref<HTMLInputElement | null>(null)
const pendingAttachments = ref<PendingAttachment[]>([])

const readyAttachments = computed(() =>
  pendingAttachments.value.filter((a) => a.status === 'ready'),
)
const attachmentsBusy = computed(() =>
  pendingAttachments.value.some((a) => a.status !== 'ready'),
)
const imageVisionBlocked = computed(
  () =>
    readyAttachments.value.some((a) => a.kind === 'image') &&
    !activeModelSupportsVision.value,
)
/** 附件区的一句话状态：为空表示当前这批可以随消息发出。 */
const attachmentGate = computed(() => {
  if (!pendingAttachments.value.length) return ''
  if (pendingAttachments.value.some((a) => a.status === 'uploading')) {
    return '附件正在上传…'
  }
  if (pendingAttachments.value.some((a) => a.status === 'error')) {
    return '有附件上传失败，请移除后重试'
  }
  if (imageVisionBlocked.value) return '当前模型未开启图片理解，图片不能发送'
  return ''
})

function fileExtension(name: string): string {
  const index = name.lastIndexOf('.')
  return index >= 0 ? name.slice(index + 1).toLowerCase() : ''
}

function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || IMAGE_EXTENSIONS.includes(fileExtension(file.name))
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

/** 上传失败时的可读原因：业务拒绝走 envelope，网络错误走 Error。 */
function errorText(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    const text = (err as { message?: unknown }).message
    if (typeof text === 'string' && text) return text
  }
  return '上传失败，请稍后重试'
}

/** 前端先做一遍格式/大小检查，省得把大文件传完才被后端拒绝。 */
function validateFile(file: File): string {
  const image = isImageFile(file)
  if (image) {
    if (fileExtension(file.name) === 'bmp') return '图片请使用 PNG / JPEG / GIF / WebP 格式'
    if (file.size > MAX_IMAGE_BYTES) {
      return `图片超过 ${MAX_IMAGE_BYTES / 1024 / 1024}MB 上限`
    }
    return ''
  }
  if (!FILE_EXTENSIONS.includes(fileExtension(file.name))) {
    return '暂不支持该文件格式'
  }
  if (file.size > MAX_FILE_BYTES) {
    return `文件超过 ${MAX_FILE_BYTES / 1024 / 1024}MB 上限`
  }
  return ''
}

function openFilePicker() {
  fileInput.value?.click()
}

function onFilesPicked(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  // 清空 value：同一个文件连选两次也要能再次触发 change。
  input.value = ''
  if (files.length) void addFiles(files)
}

function onComposerPaste(event: ClipboardEvent) {
  const files = Array.from(event.clipboardData?.files ?? [])
  if (!files.length) return
  // 有文件时拦下默认粘贴：截图否则会被编码成一串图片字符塞进输入框。
  event.preventDefault()
  void addFiles(files)
}

async function addFiles(files: File[]) {
  const slots = MAX_ATTACHMENTS - pendingAttachments.value.length
  if (slots <= 0) {
    toast.warning(`最多上传 ${MAX_ATTACHMENTS} 个附件`)
    return
  }
  if (files.length > slots) {
    toast.warning(`最多上传 ${MAX_ATTACHMENTS} 个附件，超出部分已忽略`)
  }
  for (const file of files.slice(0, slots)) {
    const problem = validateFile(file)
    if (problem) {
      toast.warning(`${file.name}：${problem}`)
      continue
    }
    const kind = isImageFile(file) ? 'image' : 'file'
    const item: PendingAttachment = {
      key: `p${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      kind,
      filename: file.name,
      mime: file.type || null,
      size: file.size,
      previewUrl: kind === 'image' ? URL.createObjectURL(file) : undefined,
      status: 'uploading',
    }
    pendingAttachments.value.push(item)
    void uploadAttachment(item, file)
  }
}

async function uploadAttachment(item: PendingAttachment, file: File) {
  try {
    const uploaded = await chatApi.uploadAttachment(file)
    const live = pendingAttachments.value.find((a) => a.key === item.key)
    if (!live) {
      // 上传途中被移除了：把刚建好的服务端行也删掉，别留孤儿。
      void chatApi.deleteAttachment(uploaded.id).catch(() => undefined)
      return
    }
    live.id = uploaded.id
    live.filename = uploaded.filename
    live.mime = uploaded.mime
    live.size = uploaded.size
    live.status = 'ready'
    if (live.kind === 'image') {
      if (live.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(live.previewUrl)
      live.previewUrl = uploaded.url ?? undefined
    }
  } catch (err) {
    const live = pendingAttachments.value.find((a) => a.key === item.key)
    if (!live) return
    live.status = 'error'
    live.error = errorText(err)
  }
}

function releasePending(item: PendingAttachment, remote: boolean) {
  if (item.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl)
  if (remote && item.id) void chatApi.deleteAttachment(item.id).catch(() => undefined)
}

function removePending(item: PendingAttachment) {
  const index = pendingAttachments.value.indexOf(item)
  if (index === -1) return
  pendingAttachments.value.splice(index, 1)
  releasePending(item, true)
}

/** 发出去之后服务端会绑定这些行，remote 传 false；丢弃未发附件时才删远端。 */
function clearPending({ remote = false } = {}) {
  const items = pendingAttachments.value
  pendingAttachments.value = []
  for (const item of items) releasePending(item, remote)
}

/** 发送快照：只取已上传成功的，图片的预览地址此时已是服务端 URL。 */
function attachmentSnapshot(): ChatAttachment[] {
  return readyAttachments.value.map((a) => ({
    id: a.id as number,
    kind: a.kind,
    filename: a.filename,
    mime: a.mime,
    size: a.size,
    url: a.kind === 'image' ? a.previewUrl ?? null : null,
  }))
}

/** 失败重试且后端没有这条记录时，把附件放回待发区，别让用户重传一遍。 */
function restorePending(attachments: ChatAttachment[]) {
  for (const att of attachments) {
    if (att.id == null) continue
    pendingAttachments.value.push({
      key: `p${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      id: att.id,
      kind: att.kind,
      filename: att.filename,
      mime: att.mime,
      size: att.size,
      previewUrl: att.kind === 'image' && att.url ? att.url : undefined,
      status: 'ready',
    })
  }
}

// ---- 贴底滚动 ---------------------------------------------------------------
// 用户上翻查看历史时，新 token 不该硬把视图拽回底部；只有原本贴着底，
// 才跟着回答往下滚。
const STICK_THRESHOLD = 40
const stickToBottom = ref(true)

function onMessagesScroll() {
  const el = scrollEl.value
  if (el) {
    stickToBottom.value =
      el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD
  }
}

// 流式过程中调用：尊重贴底状态，用户上翻时不动视图。
function scrollToBottom() {
  void nextTick(() => {
    const el = scrollEl.value
    if (el && stickToBottom.value) el.scrollTop = el.scrollHeight
  })
}

// 用户主动动作（发送 / 重试 / 审批 / 切换会话）：强制贴底跟随。
function pinToBottom() {
  stickToBottom.value = true
  scrollToBottom()
}

// ---- 回答时间线：思考过程 + 工具调用过程 -------------------------------------
// 流式期间按到达顺序累积，渲染成交错的「思考 / 调工具」时间线；落库后后端按
// 同一结构存在消息的 timeline 字段里，历史回放直接用它重建。

// 流式助手气泡：创建时就包成 reactive。SSE 回调闭包一直拿着这个引用往
// timeline / content 上追加，若 push 进 messages 的是裸对象，闭包里的 raw
// 引用绕过 Vue 代理陷阱，思考/工具增量不会触发重渲染（表现为时间线冻在
// 第一帧、头部永远「思考中…」，直到正文 token 借别的刷新才解冻）。
function newAssistantBubble(): Bubble {
  return reactive<Bubble>({
    key: `a${Date.now()}`,
    role: 'assistant',
    content: '',
    citations: [],
    timeline: [],
  })
}

function appendReason(bubble: Bubble, delta: string) {
  const timeline = (bubble.timeline ??= [])
  const last = timeline[timeline.length - 1]
  // 与后端 timeline 的合并口径一致：连续的推理增量并进同一条，
  // 中间被工具调用隔开的思考才另起一段。
  if (last && last.type === 'reason') {
    last.detail = `${last.detail ?? ''}${delta}`
  } else {
    timeline.push({ type: 'reason', detail: delta })
  }
}

function appendStep(
  bubble: Bubble,
  tool: string,
  input?: Record<string, unknown>,
  runId = '',
) {
  // 新工具开始意味着上一个已经返回：把之前还在转圈的步骤全部收掉，
  // 只保留最新一个处于进行中。
  const timeline = (bubble.timeline ??= [])
  for (const entry of timeline) {
    if (entry.type === 'step' && entry.running) entry.running = false
  }
  timeline.push({
    type: 'step',
    tool,
    query: typeof input?.query === 'string' ? input.query : '',
    input,
    run_id: runId,
    running: true,
  })
}

// 工具返回：按 run_id 找回步骤挂上结果并停止转圈；没有 run_id（兼容旧帧）
// 时挂到最后一个还没结果的步骤，与后端的兜底配对同口径。
function attachStepResult(
  bubble: Bubble,
  runId: string,
  output: string,
  isError: boolean,
) {
  const timeline = bubble.timeline ?? []
  let target: TimelineEntry | undefined
  if (runId) target = [...timeline].reverse().find((e) => e.type === 'step' && e.run_id === runId)
  if (!target) {
    target = [...timeline].reverse().find((e) => e.type === 'step' && e.output == null)
  }
  if (!target) return
  target.output = output
  target.is_error = isError
  target.running = false
}

function settleSteps(bubble: Bubble) {
  for (const entry of bubble.timeline ?? []) {
    if (entry.type === 'step') entry.running = false
  }
}

/**
 * 首答 / 重试 / 确认后续答三条流共用的 SSE 回调：思考增量、工具步骤、正文
 * token、确认卡片与落定收尾都走同一套。差异只在出错后各自怎么收拾本地状态，
 * 由 onFailed 传入。
 */
function makeStreamHandlers(
  bubble: Bubble,
  streamActions: OpsAction[],
  onFailed: (message: string) => void,
): StreamHandlers {
  // 第一个过程事件（思考 / 工具 / 正文 / 提议）到达就让气泡上线，不必再等
  // 第一个正文 token——思考与调工具本身就是要展示的内容。失败时在 onError
  // 里把没有正文的空气泡撤掉，不会留下空助手消息。
  const ensureBubble = () => {
    if (!messages.value.includes(bubble)) messages.value.push(bubble)
  }
  return {
    onCitations: (citations) => {
      bubble.citations = citations
    },
    onStep: ({ tool, input, run_id }) => {
      ensureBubble()
      appendStep(bubble, tool, input, run_id)
      scrollToBottom()
    },
    onStepResult: ({ run_id, output, is_error }) => {
      attachStepResult(bubble, run_id, output, is_error)
      scrollToBottom()
    },
    onReason: (delta) => {
      ensureBubble()
      appendReason(bubble, delta)
      scrollToBottom()
    },
    onToken: (token) => {
      ensureBubble()
      // 开始吐正文说明这一轮工具都返回了；之后若再调工具，新的 step 会重新转圈。
      settleSteps(bubble)
      pendingText.value += token
      bubble.content = pendingText.value
      schedule()
      scrollToBottom()
    },
    onConfirm: (data) => {
      // 模型提议写命令后可能直接结束本轮、一个 token 都不吐：先保证
      // 气泡存在，确认卡片才有地方挂。
      ensureBubble()
      const action = actionFromConfirm(data, activeId.value)
      actions.value.push(action)
      streamActions.push(action)
      scrollToBottom()
    },
    onDone: ({ message_id }) => {
      // 记下落库 id，这条气泡里的图才可以手动改（改完要能回写到这条消息）。
      bubble.id = message_id
      settleSteps(bubble)
      // 回填本轮提议的 message_id：卡片从「流式气泡」改挂到落库消息上，
      // 与历史回放的定位方式一致。
      for (const a of streamActions) a.message_id = message_id
      finish()
      void loadConversations()
    },
    onError: ({ message }) => {
      toast.error(message)
      settleSteps(bubble)
      // 只收到思考/工具过程、一个字正文都没吐、也没提议写命令：撤掉空气泡，
      // 后续交给 onFailed（挂重试入口等）。有确认卡片的气泡要留下来挂卡片。
      if (!bubble.content && !streamActions.length) {
        const index = messages.value.indexOf(bubble)
        if (index >= 0) messages.value.splice(index, 1)
      }
      onFailed(message)
    },
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
  usableModels.value.map((m) => ({
    value: m.id,
    // 图片能力直接标在选择器里：用户不必先传一张图才知道这个模型收不收。
    label: m.supports_vision ? `${m.title}（图片）` : m.title,
  })),
)
const defaultModelConfigId = computed(
  () => usableModels.value.find((m) => m.is_default)?.id ?? usableModels.value[0]?.id,
)
const activeModelConfig = computed(
  () => usableModels.value.find((m) => m.id === activeModelConfigId.value) || null,
)
const activeModelSupportsVision = computed(
  () => !!activeModelConfig.value?.supports_vision,
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
  if (pendingContext.value) return '留空直接发送则让 AI 通读简历并分析，或输入你的第一个问题…'
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

// 工具目录：本地工具目录同时登记思考时间线里的中文标题（utils/chatTools）。
// 两个来源各自独立失败、互不连坐（MCP 列表挂了不影响本地工具可选）。
async function loadToolCatalogs() {
  const [localResult, mcpResult] = await Promise.allSettled([
    toolsApi.listLocal(),
    mcpApi.list(),
  ])
  if (localResult.status === 'fulfilled') {
    localTools.value = localResult.value.items
    registerToolLabels(localTools.value)
  }
  if (mcpResult.status === 'fulfilled') {
    mcpServers.value = mcpResult.value.items
  }
}

// ---- 手动对话压缩 -----------------------------------------------------------
// 服务端用 cut_message_id 锚点分界：该 id 及之前的消息不再进入模型上下文，但
// 原文一条不删。前端口径与之一致——锚点前的消息只是在本会话视图里藏起来，
// 分界卡可展开看摘要，取消压缩或重新进入会话即恢复服务端口径。
const compaction = ref<CompactionState | null>(null)
const summaryExpanded = ref(false)
const compactModalOpen = ref(false)
const compactKeepTurns = ref(6)
const compactInstruction = ref('')
const compactPreview = ref('')
const compactMeta = ref<CompactMeta | null>(null)
const compactBusy = ref(false)
const compactApplying = ref(false)
/** 边界与后端 COMPACT_* 常量对齐。 */
const COMPACT_KEEP_TURNS_MIN = 1
const COMPACT_KEEP_TURNS_MAX = 20
const COMPACT_INSTRUCTION_MAX = 500
let stopCompactStream: (() => void) | null = null

// 锚点之前的消息在当前视图隐藏；无锚点（自动滚动摘要）时一条都不藏。
// 没有 id 的是流式中的临时气泡，永远可见。
const visibleMessages = computed(() => {
  const cut = compaction.value?.cut_message_id
  if (cut == null) return messages.value
  return messages.value.filter((m) => m.id == null || m.id > cut)
})

function openCompaction() {
  if (streaming.value || !activeId.value) return
  compactKeepTurns.value = 6
  // 再次压缩默认带上次的要求：渐进式压缩沿用同一条摘要偏好。
  compactInstruction.value = compaction.value?.instruction ?? ''
  compactPreview.value = ''
  compactMeta.value = null
  compactBusy.value = false
  compactModalOpen.value = true
  // 打开即流式生成预览，用户调参后再手动「重新生成」。
  runCompactPreview()
}

function runCompactPreview() {
  if (!activeId.value) return
  stopCompactStream?.()
  compactPreview.value = ''
  compactMeta.value = null
  compactBusy.value = true
  beginStream('compact-preview')
  stopCompactStream = chatApi.previewCompaction(
    activeId.value,
    {
      keep_turns: compactKeepTurns.value ?? 6,
      instruction: compactInstruction.value.trim() || undefined,
    },
    {
      onCompactMeta: (data) => {
        compactMeta.value = data
      },
      onToken: (token) => {
        compactPreview.value += token
        schedule()
      },
      onCompactDone: () => finishCompactPreview(),
      onError: (data) => {
        finishCompactPreview()
        toast.error(data.message)
      },
    },
  )
}

function finishCompactPreview() {
  compactBusy.value = false
  stopCompactStream = null
  endStream()
}

/** 用户点「停止」：中断请求，已收到的半截预览保留在框里供阅读。 */
function stopCompactPreview() {
  stopCompactStream?.()
  stopCompactStream = null
  finishCompactPreview()
}

function closeCompaction() {
  // 关弹窗即放弃这次预览：挂着的流必须收掉，否则它会在后台继续烧 token。
  if (compactBusy.value) stopCompactPreview()
  compactModalOpen.value = false
}

async function applyCompactionFromModal() {
  if (!activeId.value || !compactMeta.value || compactBusy.value) return
  const summary = compactPreview.value.trim()
  if (!summary) return
  compactApplying.value = true
  try {
    const state = await chatApi.applyCompaction(activeId.value, {
      cut_message_id: compactMeta.value.cut_message_id,
      summary,
      instruction: compactInstruction.value.trim() || null,
    })
    compaction.value = state
    compactModalOpen.value = false
    toast.success('已压缩，更早的对话不再进入上下文')
  } catch {
    // 拦截器已弹 toast（常见：切点过期，重新生成即可）。
  } finally {
    compactApplying.value = false
  }
}

async function clearCompaction() {
  if (!activeId.value) return
  try {
    await chatApi.clearCompaction(activeId.value)
    compaction.value = null
    summaryExpanded.value = false
    toast.success('已取消压缩，更早的消息恢复进入回放窗口')
  } catch {
    // 拦截器已弹 toast。
  }
}

// 切换会话的竞态守卫：快速连点时慢响应不得覆盖新选择。
// 序号管「后发起的覆盖先发起的」，id 比对管「等响应期间又切走了」。
let convLoadSeq = 0

async function selectConversation(id: number) {
  if (streaming.value) return
  const seq = ++convLoadSeq
  activeId.value = id
  // 草稿与待发附件都不跨会话带过去：那是写给上一个人的话。
  draft.value = ''
  // 深链带入但还没发送的简历上下文也不跨会话：它只属于这次发起。
  pendingContext.value = null
  clearPending({ remote: true })
  clearRenderCaches()
  let data
  let acts
  let compact
  try {
    // 确认卡片、压缩状态与消息一起回放：普通会话的 actions 恒为空、compaction
    // 恒为 null，多两个轻请求换一次渲染齐套，不额外转圈。
    ;[data, acts, compact] = await Promise.all([
      chatApi.listMessages(id),
      chatApi.listActions(id),
      chatApi.getCompaction(id),
    ])
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
    // 老消息没有 timeline：空数组，时间线不渲染，只显示正文。
    timeline: m.timeline ?? [],
    attachments: m.attachments ?? [],
  }))
  actions.value = acts.items
  compaction.value = compact
  summaryExpanded.value = false
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
  // 恢复会话记住的工具选择，但按现存目录裁剪：本地工具下线、MCP 被删除/停用的
  // 勾选项直接消失（同 type_ids/model 的裁剪哲学），免得发送时才被后端挡回。
  const aliveLocalKeys = new Set(localTools.value.map((tool) => tool.key))
  const aliveMcpIds = new Set(
    mcpServers.value.filter((server) => server.enabled).map((server) => server.id),
  )
  toolSelection.value = [
    ...(conv?.local_tool_keys ?? [])
      .filter((key) => aliveLocalKeys.has(key))
      .map((key) => `local:${key}`),
    ...(conv?.mcp_server_ids ?? [])
      .filter((id) => aliveMcpIds.has(id))
      .map((id) => `mcp:${id}`),
  ]
  pinToBottom()
}

function startNew() {
  if (streaming.value) return
  // 让还在路上的 selectConversation 响应失效，别把旧会话的消息灌进新会话。
  convLoadSeq++
  activeId.value = null
  // 新建会话等于放弃这次深链带入的挂起上下文。
  pendingContext.value = null
  messages.value = []
  actions.value = []
  compaction.value = null
  summaryExpanded.value = false
  draft.value = ''
  clearPending({ remote: true })
  clearRenderCaches()
}

async function removeConversation(id: number) {
  try {
    await chatApi.deleteConversation(id)
  } catch {
    return // 拦截器已弹 toast。
  }
  toast.success('已删除')
  if (activeId.value === id) startNew()
  await loadConversations()
}

// ---- 带上下文发起对话（报告讨论 / 在线简历咨询）-------------------------------
// 报告页「发起对话」进 /chat?report=<kind>/<id>，在线简历列表「AI 对话」进
// /chat?resume-doc=<id>，都是新开标签页。query 只传引用不传全文：全文由这里
// 重新拉取（noopener 下拿不到 opener 的 sessionStorage，URL 也塞不下几千字），
// 数据以后端为准。

const REPORT_QUERY_RE = /^(resume|comparison|toolkit)\/(\d+)$/
const RESUME_DOC_QUERY_RE = /^\d+$/
/** 首条消息上限与后端 CompletionRequest.message 的 20000 对齐。 */
const CONTEXT_MESSAGE_LIMIT = 20000
/** 上下文讨论的默认智能体（内置「求职顾问」，backend/services/agents.py 里 seed）。
 *  找不到（后端没重启 seed、或被用户停用）就退回当前选中的智能体。 */
const CONTEXT_DEFAULT_AGENT_SLUG = 'career-advisor'

/**
 * 上下文会话的「种子消息」登记簿，按会话 id 记。
 * 在这类会话里中途换智能体时前端会另开一条新会话——换人不能丢上下文（否则新
 * 智能体拿到的是空对话），所以重开时拿同一份种子消息再播一遍种。只活在当前
 * 页内：以后从侧栏重新打开老会话，就是普通会话的语义。
 */
const contextSeeds = new Map<number, { firstMessage: string; convTitle: string }>()

/** 简历深链带入、但还没发送的上下文：发送前用户可在工具栏自由换智能体/模型/工具、
 *  输入第一个问题；点发送时才回源拉最新内容并建会话。 */
interface PendingResumeContext {
  docId: number
  title: string
  charCount: number
}
const pendingContext = ref<PendingResumeContext | null>(null)
// 挂起上下文的首条消息正在回源拉取/建会话：期间禁用发送，防连点建出两条会话。
const contextLaunching = ref(false)
/** 挂起卡片直接点发送（没写首问）时用的默认开场。 */
const DEFAULT_RESUME_QUESTION = '请通读这份简历，给出整体分析与改进建议'
/** 简历咨询会话默认勾选的「读取在线简历」本地工具（backend builtins/resume_tools.py），
 *  模型之后每轮都能回源读最新版；目录里没有（后端未更新）就跳过。 */
const RESUME_DOC_TOOL_KEY = 'get_resume_doc'

function truncateContextMessage(message: string): string {
  // 防御后端哪天再收紧上限：宁可截断并明说，也不让 AI 悄悄基于残缺内容作答。
  return message.length > CONTEXT_MESSAGE_LIMIT
    ? `${message.slice(0, CONTEXT_MESSAGE_LIMIT)}\n\n（上下文内容过长，已按长度上限截断）`
    : message
}

/**
 * 用当前选中的智能体/模型建一条带种子消息的会话并立即发送。深链首答与
 * 「换智能体重新分析」两条路共用：后者直接按新选择再播一次种。
 * gate 未过或建会话失败时不建孤儿会话，内容放进输入框不丢。
 */
async function seedContextConversation(firstMessageIn: string, convTitle: string) {
  const firstMessage = truncateContextMessage(firstMessageIn)

  if (!canSend.value) {
    // 没配模型这类 gate 场景：不建孤儿空会话，内容放输入框，用户按 gate 提示
    // 配好后点发送即可，内容不丢。
    draft.value = firstMessage
    toast.warning('内容已放入输入框，按上方提示完成配置后即可发送')
    return
  }

  startNew()
  const conv = await createSeededConversation(convTitle, firstMessage)
  if (!conv) {
    // 拦截器已弹错；内容放输入框，用户可手动重试。
    draft.value = firstMessage
    return
  }
  draft.value = firstMessage
  send()
}

/**
 * 按工具栏当前选择（智能体/模型/知识库范围/工具）显式建会话并登记种子消息。
 * 调用方负责先 startNew()。失败返回 null（拦截器已弹错），不建孤儿会话。
 */
async function createSeededConversation(convTitle: string, firstMessage: string) {
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
      // 工具选择与普通发送同口径：建会话时就把当前勾选记住。
      local_tool_keys: selectedLocalToolKeys.value,
      mcp_server_ids: selectedMcpServerIds.value,
    })
  } catch {
    return null
  }
  // 不走 selectConversation：新会话的消息/actions 必然为空（白拉两次），它还会
  // 重置 agent/模型选择——而我们正是按当前选择建的会话，重置是绕回原点的空转。
  conversations.value.unshift(conv)
  activeId.value = conv.id
  contextSeeds.set(conv.id, { firstMessage, convTitle })
  return conv
}

/**
 * 发送挂起的简历上下文：回源拉最新已保存版本（用户可能在工作台改完又存了一次），
 * 把简历全文与用户首问拼成首条消息，显式建会话后走普通发送；已上传的附件留在
 * 待发区，send() 会一并带上。
 */
async function launchPendingResume(question: string) {
  const pending = pendingContext.value
  if (!pending || contextLaunching.value || !canSend.value) return
  contextLaunching.value = true
  try {
    let detail
    try {
      detail = await resumeBuilderApi.detail(pending.docId)
    } catch {
      // 拦截器已弹错；卡片与首问原样保留，用户重试即可。
      return
    }
    if (!resumeDocHasContent(detail.doc)) {
      toast.warning('这份在线简历现在是空的，先在简历工作台填写内容后再发送')
      return
    }

    const ask = question.trim() || DEFAULT_RESUME_QUESTION
    const markdown = resumeDocToMarkdown(detail.doc)
    const intro = `以下是我的在线简历《${detail.title}》（文档 id：${detail.id}）的完整内容，请先通读，稍后我会围绕它向你咨询，或请你分析并给出建议。`
    // 工具在勾选时才提示模型去调；被用户手动摘掉就别提一个调不了的工具。
    const toolLine = toolSelection.value.includes(`local:${RESUME_DOC_TOOL_KEY}`)
      ? `如需读取服务端最新版本（简历可能被修改过），调用 get_resume_doc 工具，doc_id=${detail.id}。`
      : ''
    const firstMessage = truncateContextMessage(
      [intro, toolLine, `---\n\n${markdown}`, `---\n\n${ask}`].filter(Boolean).join('\n\n'),
    )
    const rawTitle = `简历咨询 · ${detail.title}`
    const convTitle = rawTitle.length <= 50 ? rawTitle : `${rawTitle.slice(0, 50)}…`

    // 拉取通过才做内存复位。刻意不调 startNew()：它会 clearPending({remote:true})
    // 把已上传附件从服务端删掉，而附件要随这条首条消息一起走。
    convLoadSeq++
    activeId.value = null
    messages.value = []
    actions.value = []
    draft.value = ''
    clearRenderCaches()
    // 摘卡片：建会话失败时要把卡片挂回来。附件始终留在待发区，不动它。
    pendingContext.value = null
    const conv = await createSeededConversation(convTitle, firstMessage)
    if (!conv) {
      pendingContext.value = {
        docId: detail.id,
        title: detail.title,
        charCount: markdown.length,
      }
      draft.value = question
      return
    }
    draft.value = firstMessage
    send()
  } finally {
    contextLaunching.value = false
  }
}

/** 深链入口：默认选「求职顾问」、起会话标题，再交给共用的播种流程。 */
async function launchContextConversation(options: {
  title: string
  firstMessage: string
  convTitlePrefix: string
}) {
  // 默认切到「求职顾问」：要在 canSend 判断之前切换，gate 检查（比如知识库
  // 项目）针对的才是这场对话真正要用的智能体。这里是程序改写选择器，不走
  // 下拉框的 change，onAgentChange 的重开保护不会被它误触发。
  const advisor = agents.value.find(
    (a) => a.slug === CONTEXT_DEFAULT_AGENT_SLUG && a.chat_visible,
  )
  if (advisor) activeAgentId.value = advisor.id

  // 显式建会话而不是让首条消息触发自动建：自动建的标题是从首问截 40 字
  // （derive_title），首问是长文时标题会是一截正文。
  const rawTitle = `${options.convTitlePrefix} · ${options.title}`
  const convTitle = rawTitle.length <= 50 ? rawTitle : `${rawTitle.slice(0, 50)}…`
  await seedContextConversation(options.firstMessage, convTitle)
}

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

  await launchContextConversation({
    title,
    convTitlePrefix: '报告讨论',
    firstMessage: `以下是报告《${title}》的完整内容，请先阅读，稍后我会基于它向你提问。\n\n---\n\n${markdown}`,
  })
}

async function handleResumeDocChat() {
  const raw = route.query['resume-doc']
  if (typeof raw !== 'string' || !RESUME_DOC_QUERY_RE.test(raw)) return
  void router.replace({ query: {} })

  let detail
  try {
    detail = await resumeBuilderApi.detail(Number(raw))
  } catch {
    // 简历不存在/无权访问时拦截器已弹错；页面停在普通空白会话态。
    return
  }

  if (!resumeDocHasContent(detail.doc)) {
    toast.warning('这份在线简历还是空的，先在简历工作台填写一些内容再发起对话')
    return
  }

  // 不自动发送：默认选好求职顾问，把简历挂成「待发送上下文」卡片。用户先在工具栏
  // 选智能体/模型/工具、写好第一个问题，点发送时才回源拉最新内容建会话（见
  // launchPendingResume）。这里是程序改值，不触发 onAgentChange 的重开保护。
  const advisor = agents.value.find(
    (a) => a.slug === CONTEXT_DEFAULT_AGENT_SLUG && a.chat_visible,
  )
  if (advisor) activeAgentId.value = advisor.id

  pendingContext.value = {
    docId: detail.id,
    title: detail.title,
    charCount: resumeDocToMarkdown(detail.doc).length,
  }

  // 默认勾上「读取在线简历」：用户在对话中途回工作台保存了修改，模型可随时回源
  // 读到最新版，不用用户复述。只在工具目录里有这个 key 时勾选（后端版本兜底）。
  const toolValue = `local:${RESUME_DOC_TOOL_KEY}`
  if (
    localTools.value.some((tool) => tool.key === RESUME_DOC_TOOL_KEY) &&
    !toolSelection.value.includes(toolValue)
  ) {
    toolSelection.value = [...toolSelection.value, toolValue]
  }
}

// ---- 深链打开指定会话 -------------------------------------------------------
// 首页「最近对话」带 ?conv=<id> 进这里，省掉一次侧栏点击。id 不在列表里
// （已删除或不属于当前用户）就静默停在初始空态。
const CONVERSATION_QUERY_RE = /^\d+$/

async function handleConversationDeepLink() {
  const raw = route.query.conv
  if (typeof raw !== 'string' || !CONVERSATION_QUERY_RE.test(raw)) return
  // 与报告深链同一约定：先清 query，刷新这个标签页不会重复打开。
  void router.replace({ query: {} })
  const id = Number(raw)
  // keep-alive 复活时列表可能是首屏拉的那份，也可能从没拉过（深链比 onMounted
  // 的并发加载更早完成）——缺就补一次，别拿旧列表判存在性。
  if (!convsLoaded.value) await loadConversations()
  if (conversations.value.some((conv) => conv.id === id)) {
    await selectConversation(id)
  }
}

// 对话页挂在 keep-alive 里：页面已经开着时从首页再点一条会话，onMounted 不会
// 重跑，所以路由上的 query 变化得走同一套深链处理。
watch(
  () => [route.path, route.query.conv] as const,
  ([path, conv]) => {
    if (path !== '/chat' || !conv || booting.value) return
    void handleConversationDeepLink()
  },
)

// ---- 发送 ------------------------------------------------------------------

// 输入法合成期间按发送要缓一缓：这时 v-model 里还没有合成窗口里的文字，
// 而 ant-design-vue 会在 compositionend 里把原文回填进 v-model。直接发的话
// 一是消息缺最后一段，二是发送时的清空随后被回填覆盖，看起来像「已发出但
// 输入框没清空」。先挂起发送，等合成结束、回填落定后再走正常发送。
let composerComposing = false
let pendingSend = false

// 回答流式期间输入框是禁用的，用户不可能往里写字；这段时间 v-model 被任何
// 事件回写（禁用引发的失焦 change、输入法 compositionend 回填）都算脏数据，
// 直接清掉，保证「消息发出去 = 输入框变空」。
watch(draft, (v) => {
  if (streaming.value && v) draft.value = ''
})

function onComposerCompositionStart() {
  composerComposing = true
}

function onComposerCompositionEnd() {
  composerComposing = false
  if (pendingSend) {
    pendingSend = false
    // 回填是同步事件链的一部分，nextTick 再发读到的就是完整文本。
    void nextTick(send)
  }
}

function onComposerEnter(e: KeyboardEvent) {
  // 中文输入法里按 Enter 是选词、不是发送：这种 keydown 的 isComposing 为 true。
  // 不拦的话消息会被提前发出去，随后输入法把整段文字回填进输入框，
  // 看起来就像「发出去了但没清空」。preventDefault 也只在真正发送时才调，
  // 否则会把选词那一次 Enter 吞掉。keyCode 229 是输入法还在处理按键的
  // 标记，部分输入法提交候选的那次 Enter 只报 229、不报 isComposing。
  if (e.isComposing || e.keyCode === 229) return
  e.preventDefault()
  send()
}

function send() {
  if (composerComposing) {
    pendingSend = true
    // 主动结束合成，免得点了发送却在等输入法自己提交；blur 会让它马上
    // 补一次 compositionend，发送在下一拍继续。
    composerRef.value?.blur()
    return
  }
  // 先失焦再清空：发送会把输入框禁用，禁用使聚焦的 textarea 失焦并触发
  // change，antd 在 change 里把失焦瞬间的旧值回写进 v-model，正好盖掉这次
  // 清空——表现为消息发出去了、输入框还留着整段原文。
  composerRef.value?.blur()
  const question = draft.value.trim()
  const attachments = readyAttachments.value
  // 挂着简历上下文时允许空首问（走默认分析开场），其余情况空内容照旧不发。
  if (
    (!question && !attachments.length && !pendingContext.value) ||
    !canSend.value ||
    contextLaunching.value
  )
    return
  if (attachmentsBusy.value) {
    toast.warning('附件还在上传，请稍候')
    return
  }
  if (imageVisionBlocked.value) {
    toast.warning('当前模型未开启图片理解，请换用支持图片的模型或移除图片')
    return
  }

  // 挂起的简历上下文走异步首条流程（回源拉最新 → 建会话 → 带首问发送），
  // 不走下面的常规建连。
  if (pendingContext.value) {
    void launchPendingResume(question)
    return
  }

  const snapshot = attachmentSnapshot()
  const userBubble: Bubble = {
    key: `u${Date.now()}`,
    role: 'user',
    content: question,
    citations: [],
    attachments: snapshot,
  }
  messages.value.push(userBubble)
  draft.value = ''
  // 附件即将随消息绑定到服务端，这里只清本地预览，不发删除请求。
  clearPending()
  pinToBottom()

  streaming.value = true
  pendingText.value = ''

  const bubble = newAssistantBubble()
  // 本轮流里提议的待确认项：done 时统一回填 message_id。
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key

  abort = chatApi.streamCompletion(
    {
      message: question,
      ...(snapshot.length ? { attachment_ids: snapshot.map((a) => a.id) } : {}),
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
      // 工具选择每轮显式带数组：空数组就是清空（与不传=沿用会话选择相对）。
      local_tool_keys: selectedLocalToolKeys.value,
      mcp_server_ids: selectedMcpServerIds.value,
    },
    {
      onMeta: ({ conversation_id, local_tool_keys, mcp_server_ids }) => {
        activeId.value = conversation_id
        // 以服务端落库后的选择为准回写选择器（自动建会话的首问也由此记住）。
        toolSelection.value = [
          ...(local_tool_keys ?? []).map((key) => `local:${key}`),
          ...(mcp_server_ids ?? []).map((id) => `mcp:${id}`),
        ]
      },
      ...makeStreamHandlers(bubble, streamActions, () => {
        // 提问还在、回答没落成：挂上「可重试」标记，别让用户重新打一遍。
        // 工厂已弹过错误提示、撤掉了没有正文的空气泡。
        userBubble.failed = true
        finish()
      }),
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
  // 删掉本地气泡、把内容和附件放回发送框重走正常发送。
  if (!activeId.value) {
    messages.value.splice(idx, 1)
    draft.value = msg.content
    if (msg.attachments?.length) restorePending(msg.attachments)
    send()
    return
  }

  // 清掉这条提问之后的残文（失败时已生成的半截回答），重新挂流。
  messages.value.splice(idx + 1)
  msg.failed = false

  streaming.value = true
  pendingText.value = ''

  const bubble = newAssistantBubble()
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key
  pinToBottom()

  abort = chatApi.streamRetry(
    activeId.value,
    makeStreamHandlers(bubble, streamActions, () => {
      // 又失败了：残文（含只有过程的空气泡）清掉，提问重新挂回「可重试」。
      messages.value.splice(idx + 1)
      msg.failed = true
      finish()
    }),
  )
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

  const bubble = newAssistantBubble()
  const streamActions: OpsAction[] = []
  beginStream(bubble.key)
  streamingBubbleKey.value = bubble.key
  pinToBottom()

  abort = approve(action, {
    onExec: () => {
      // 执行结果写回卡片后滚到底，让输出块进入视野。
      scrollToBottom()
    },
    ...makeStreamHandlers(bubble, streamActions, () => {
      finish()
    }),
  })
}

function rejectAction(action: OpsAction) {
  void reject(action)
}

function stop() {
  const key = streamingBubbleKey.value
  abort?.()
  finish()
  if (key) {
    const index = messages.value.findIndex((m) => m.key === key)
    const bubble = index >= 0 ? messages.value[index] : null
    // 一个正文 token 都没吐、也没提议写命令就被停掉：撤掉只有过程的空气泡。
    // 后端在 aborted 分支不发 done，这条本地气泡本来也挂不上落库消息。
    if (bubble && !bubble.content && !actions.value.some((a) => a.message_id == null)) {
      messages.value.splice(index, 1)
    }
  }
}

function finish() {
  streaming.value = false
  pendingText.value = ''
  abort = null
  streamingBubbleKey.value = null
  // 收尾推一次全量渲染：停止/出错时残文也要以完整 markdown 落定。
  endStream()
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

// 聊到一半换智能体要开一条新会话（上下文会话则带种子重开，见函数体），不能让
// 两个人设的发言混在同一段历史里。挂在下拉框的 change 上而不是
// watch(activeAgentId)：selectConversation 恢复会话自带的智能体时也写这个 ref，
// watch 会把那次写入当成用户换人设，刚拉到的消息立刻被清空——「要点两次才显示」。
function onAgentChange() {
  if (streaming.value || !messages.value.length) return
  // 上下文会话（简历咨询/报告讨论）换智能体 = 让新人设基于同一份简历/报告
  // 重新分析：另开一条、把种子消息再播一遍，而不是开出没有上下文的空会话。
  // 标题追加智能体名，侧栏里同一简历的多条会话才分得清。普通会话照旧开空白
  // 新会话——两个人设的发言不该混在同一段历史里。
  const seed = activeId.value != null ? contextSeeds.get(activeId.value) : undefined
  if (seed) {
    const suffix = activeAgent.value ? `（${activeAgent.value.name}）` : ''
    void seedContextConversation(seed.firstMessage, `${seed.convTitle}${suffix}`)
  } else {
    startNew()
  }
}

onMounted(async () => {
  try {
    await Promise.all([
      loadReadiness(),
      loadAgents(),
      loadChatModels(),
      loadConversations(),
      loadProjects(),
      loadToolCatalogs(),
    ])
  } finally {
    booting.value = false
  }
  // 报告页「发起对话」带着 ?report=kind/id、在线简历列表「AI 对话」带着
  // ?resume-doc=<id> 进这里：能不能直接发送取决于模型/智能体/项目是否就绪，
  // 所以要等上面全部加载完再处理。
  await handleReportDiscuss()
  await handleResumeDocChat()
  await handleConversationDeepLink()
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
/* 桌面窄窗（非手机断点，见 style.css 头注约定）：上下堆叠，会话列表
   限高可滚，消息区吃剩余高度。 */
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
  font-size: var(--fs-xs);
  color: var(--danger);
  text-align: right;
}
/* hover/active 底色用全局 .list-row（style.css），这里只留布局。 */
.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--tool-radius);
}
.conv-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
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
  color: var(--danger);
}
/* 原地重命名的输入框：与标题同字号同行高，切换时不跳。 */
.conv-rename {
  flex: 1;
  min-width: 0;
  padding: 0 4px;
  border: 1px solid var(--signal-border);
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
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
  font-size: var(--fs-xs);
  font-weight: normal;
  color: var(--text-3);
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 4px 4px 12px;
}

/* 压缩分界卡：交代被压掉消息的去向，整行可点展开摘要原文。 */
.compaction-bar {
  margin: 4px 8px 12px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}
.compaction-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 0;
  background: transparent;
  font-size: var(--fs-sm);
  color: var(--text-2);
  text-align: left;
  cursor: pointer;
}
.compaction-head:hover {
  background: rgba(0, 0, 0, 0.03);
}
.compaction-icon {
  flex: none;
  color: var(--signal-text);
}
.compaction-title {
  flex: 1;
}
.compaction-toggle {
  flex: none;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.compaction-detail {
  padding: 0 12px 8px 32px;
}
.compaction-summary {
  max-height: 240px;
  overflow-y: auto;
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.compaction-instruction {
  margin-top: 6px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}

/* 压缩向导弹窗 */
.compact-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.compact-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
  color: var(--text-2);
}
.compact-preview-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.compact-meta {
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.compact-preview-wrap {
  position: relative;
  max-height: 300px;
  overflow-y: auto;
  padding: 10px 12px;
  border: 1px solid var(--hairline);
  border-radius: 8px;
  background: var(--surface);
}
.compact-preview {
  font-size: var(--fs-sm);
  color: var(--text-2);
}
/* 流式追加中的闪烁光标：挂在预览框末尾而不是塞进 markdown HTML 里。 */
.compact-caret {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin-left: 2px;
  vertical-align: text-bottom;
  background: var(--signal-text);
  animation: compact-blink 1s steps(2, start) infinite;
}
@keyframes compact-blink {
  to {
    visibility: hidden;
  }
}

/* 回到底部：sticky 钉在消息区右下角，发丝线 + 浮层阴影与全站口径一致。 */
.jump-bottom {
  position: sticky;
  bottom: 16px;
  z-index: var(--z-sticky);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  margin: 0 8px 8px auto;
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

.jump-fade-enter-active,
.jump-fade-leave-active {
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.jump-fade-enter-from,
.jump-fade-leave-to {
  opacity: 0;
  transform: translateY(8px);
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
  font-size: var(--fs-6xl);
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
/* 提问附件：图片给缩略图（ant-image 自带点击预览），文档只露文件名。 */
.msg-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.msg-image {
  display: block;
  overflow: hidden;
  border-radius: var(--radius-sm);
}
.msg-file {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 2px 8px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: var(--fs-xs);
  color: var(--text-2);
}
.msg-file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-sm);
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
  font-size: var(--fs-xs);
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
/* 挂起的简历上下文：输入框上方的一条提示卡，提醒先选智能体/工具再发送。 */
.context-bar {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
  padding: 8px 10px;
  background: #f3f9fa;
  border: 1px solid #d4e7ea;
  border-radius: var(--tool-radius);
}
.context-icon {
  margin-top: 2px;
  color: #0a7e86;
  font-size: 15px;
}
.context-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12.5px;
  line-height: 1.5;
}
.context-title {
  color: #1f2329;
  font-weight: 600;
}
.context-sub {
  color: #6b7280;
}
.context-remove {
  flex: none;
  border: 0;
  padding: 2px 4px;
  background: transparent;
  color: #8a8f99;
  cursor: pointer;
  border-radius: 4px;
}
.context-remove:hover:not(:disabled) {
  color: #b4534f;
  background: #fdf0ef;
}
.context-remove:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
/* 上传入口：常驻在输入框上方一行。就一个回形针，不抢视觉重量。 */
.attach-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 24px;
  margin-bottom: 4px;
}
.attach-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-base);
  cursor: pointer;
  transition: all 0.15s;
}
.attach-btn:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.06);
  color: var(--signal-text);
}
.attach-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.attach-count {
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.attach-warn {
  font-size: var(--fs-xs);
  color: var(--warning);
}
.file-input {
  display: none;
}
/* 待发附件条：缩略图 + 文件名 + 大小/进度，移除按钮固定在行尾。 */
.pending-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.pending-item {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 260px;
  padding: 4px 6px 4px 4px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
}
.pending-item.is-error {
  border-color: var(--danger-soft);
  background: var(--danger-bg);
}
.pending-thumb {
  display: block;
  width: 40px;
  height: 40px;
  object-fit: cover;
  border-radius: 4px;
}
.pending-file-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: 4px;
  background: var(--paper);
  color: var(--text-3);
  font-size: var(--fs-lg);
}
.pending-meta {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.pending-name {
  overflow: hidden;
  font-size: var(--fs-xs);
  color: var(--text-1);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pending-sub {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.pending-item.is-error .pending-sub {
  color: var(--danger);
}
.pending-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
}
.pending-remove:hover {
  background: rgba(0, 0, 0, 0.06);
  color: var(--danger);
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

@media (max-width: 768px) {
  /* 触屏没有 hover：操作入口常显，不然手机上找不到重命名/删除/复制。 */
  .conv-actions,
  .bubble-actions {
    opacity: 1;
  }
  /* 小屏放宽气泡：78% 在手机上白白浪费一截宽度。 */
  .bubble {
    max-width: 92%;
  }
}
</style>
