<template>
  <div :class="['msg', msg.role]">
    <div class="bubble">
      <!-- 助手气泡按 timeline 原顺序切成「过程组 / 正文段 / 确认卡」交替渲染：
           每波思考+工具调用就地待在它那轮解说文字旁边，多轮排查时读起来才是
           「思考（工具）→ 结论 → 再思考（工具）→ 再结论」，而不是全部过程
           堆在气泡头部、所有正文沉在下面。 -->
      <template v-if="msg.role === 'assistant'">
        <template v-for="sec in assistantSections(msg, actionById, streamingKey)" :key="sec.key">
          <ThinkingTrace
            v-if="sec.type === 'trace'"
            :entries="sec.entries ?? []"
            :streaming="!!sec.streaming"
          />
          <!-- 正文段按 markdown 渲染：流式中的活跃段走整段渲染（mermaid
               围栏多半还没闭合）；落定段切分，mermaid 块升级成可编辑图卡。 -->
          <div v-else-if="sec.type === 'text' && sec.text" class="content markdown">
            <div v-if="sec.streaming" v-html="render(sec.key, sec.text)" />
            <template v-else>
              <template v-for="(part, pi) in sectionParts(sec)" :key="pi">
                <MermaidCard
                  v-if="part.seg.kind === 'mermaid'"
                  :source="part.seg.text"
                  :can-edit="!!msg.id && !streaming"
                  @save="(src) => onSavePart(part.globalIndex, src)"
                />
                <div v-else v-html="render(`${sec.key}:p${pi}`, part.seg.text)" />
              </template>
            </template>
          </div>
          <!-- 写命令确认卡：按提议发生的时刻就地插在过程与正文之间。 -->
          <ConfirmCard
            v-else-if="sec.type === 'confirm' && sec.action"
            :action="sec.action"
            :acting="isActing(sec.action)"
            :can-approve="canOpsWrite"
            @approve="onApprove"
            @reject="onReject"
          />
        </template>
      </template>
      <div v-else class="content">
        <!-- 附件挂在提问上方：图片给缩略图（点开可看原图），文档只露
             文件名。只传附件不写字时，气泡里就只有附件。
             用户输入保持纯文本：打的字不该被当成标记语言解释。 -->
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

      <!-- 兜底：时间线里没有 confirm 条目的待确认项（老消息、以及未落库
           就中断的极端情况）仍挂在气泡末尾。 -->
      <ConfirmCard
        v-for="act in actionsOf(msg, actions, streamingKey, tailKey)"
        :key="act.id"
        :action="act"
        :acting="isActing(act)"
        :can-approve="canOpsWrite"
        @approve="onApprove"
        @reject="onReject"
      />

      <!-- 复制的是原始 markdown 文本，不是渲染后的 HTML——贴到别处还能用。 -->
      <div v-if="msg.role === 'assistant' && msg.content" class="bubble-actions">
        <a-tooltip title="复制">
          <button type="button" class="copy-btn" @click="emit('copy', msg)">
            <CopyOutlined />
          </button>
        </a-tooltip>
      </div>

      <div v-if="msg.failed" class="send-failed">
        发送失败
        <a-button type="link" size="small" danger @click="emit('retry', msg)">
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
</template>

<script setup lang="ts">
import MermaidCard from '@/components/MermaidCard.vue'
import ConfirmCard from '@/components/ConfirmCard.vue'
import ThinkingTrace from '@/components/ThinkingTrace.vue'
import { CopyOutlined, FileOutlined } from '@ant-design/icons-vue'
import type { OpsAction } from '@/api'
import { actionsOf, assistantSections, sectionParts, type Bubble } from '@/utils/chatSections'

const props = defineProps<{
  msg: Bubble
  /** 当前会话的全部待确认项（含已处理的）。 */
  actions: OpsAction[]
  /** 待确认项 id → 实体：时间线里的 confirm 条目靠它就地对位出卡片。 */
  actionById: Map<number, OpsAction>
  /** 正在流式追加的那条气泡的 key。 */
  streamingKey: string | null
  /** 最后一条助手气泡：无主待确认项的兜底挂载点。 */
  tailKey: string | null
  /** 是否正在流式：流式期间图卡不可编辑。 */
  streaming: boolean
  /** 当前用户是否有运维写权限。 */
  canOpsWrite: boolean
  isActing: (a: OpsAction) => boolean
  /** 渲染 markdown 的缓存入口（原 renderCached）。 */
  render: (key: string, text: string) => string
}>()

const emit = defineEmits<{
  approve: [action: OpsAction]
  reject: [action: OpsAction]
  copy: [msg: Bubble]
  retry: [msg: Bubble]
  save: [payload: { msg: Bubble; index: number; source: string }]
}>()

function onApprove(action: OpsAction) {
  emit('approve', action)
}

function onReject(action: OpsAction) {
  emit('reject', action)
}

function onSavePart(index: number, source: string) {
  emit('save', { msg: props.msg, index, source })
}
</script>

<style scoped>
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
  background: var(--hover-neutral);
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
/* 发送失败的提问下方给重试入口（气泡内，右对齐跟随用户消息）。 */
.send-failed {
  margin-top: 6px;
  font-size: var(--fs-xs);
  color: var(--danger);
  text-align: right;
}

@media (max-width: 768px) {
  /* 触屏没有 hover：操作入口常显，不然手机上找不到复制。 */
  .bubble-actions {
    opacity: 1;
  }
  /* 小屏放宽气泡：78% 在手机上白白浪费一截宽度。 */
  .bubble {
    max-width: 92%;
  }
}
</style>
