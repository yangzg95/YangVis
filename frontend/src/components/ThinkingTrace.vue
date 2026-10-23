<template>
  <!-- 思考与工具调用的交错时间线。流式中默认展开（看得到它在忙什么），
       回答落定后自动收成一行摘要，点开仍可回看完整过程。 -->
  <div v-if="visible.length" class="trace">
    <button type="button" class="trace-head" @click="open = !open">
      <DownOutlined :class="['trace-caret', { open }]" />
      <ThunderboltOutlined class="trace-head-icon" />
      <span class="trace-title">思考过程</span>
      <span class="trace-summary">
        <a-spin v-if="streaming && activeLabel" size="small" />
        <span v-if="streaming && activeLabel" class="trace-active" :title="activeLabel">
          {{ activeLabel }}
        </span>
        <span v-else-if="streaming">思考中…</span>
        <template v-else>
          <span v-if="stepCount">{{ stepCount }} 次工具调用</span>
          <span v-else>已完成</span>
          <span v-if="errorCount" class="trace-summary-errors">（{{ errorCount }} 次失败）</span>
        </template>
      </span>
    </button>
    <div v-show="open" class="trace-body">
      <template v-for="(entry, i) in visible" :key="i">
        <!-- 思维链：推理模型才有（reasoning_content），普通模型不会出现这一段。 -->
        <div v-if="entry.type === 'reason'" class="trace-reason">
          <div class="trace-label"><BulbOutlined /><span>思考</span></div>
          <div class="trace-reason-text">{{ entry.detail }}</div>
        </div>
        <!-- 工具调用：进行中转圈，返回后打勾（失败红叉）；点开看完整入参与返回。 -->
        <div v-else class="trace-step" :class="{ 'is-error': entry.is_error }">
          <button type="button" class="trace-step-head" @click="toggleStep(i)">
            <span class="trace-step-status">
              <a-spin v-if="entry.running" size="small" />
              <CloseCircleFilled v-else-if="entry.is_error" class="trace-step-error" />
              <CheckCircleFilled v-else class="trace-step-done" />
            </span>
            <ToolOutlined class="trace-step-tool" />
            <span class="trace-step-text" :title="toolLabel(entry.tool, entry.input)">
              {{ toolLabel(entry.tool, entry.input) }}
            </span>
            <DownOutlined
              v-if="argsText(entry.input) || entry.output != null"
              :class="['trace-step-caret', { open: stepOpen(i, entry) }]"
            />
          </button>
          <div v-if="stepOpen(i, entry)" class="trace-step-detail">
            <div v-if="argsText(entry.input)" class="trace-block">
              <div class="trace-block-label">入参</div>
              <pre class="trace-code">{{ argsText(entry.input) }}</pre>
            </div>
            <div class="trace-block">
              <div class="trace-block-label">
                <span :class="{ 'trace-error-text': entry.is_error }">
                  {{ entry.is_error ? '错误返回' : '返回结果' }}
                </span>
                <button
                  v-if="entry.output != null"
                  type="button"
                  class="trace-copy"
                  @click="copyOutput(i, entry.output as string)"
                >
                  <CopyOutlined />
                  {{ copiedIndex === i ? '已复制' : '复制' }}
                </button>
              </div>
              <pre
                v-if="entry.output != null"
                class="trace-code trace-code-output"
                :class="{ 'trace-code-error': entry.is_error }"
              >{{ entry.output }}</pre>
              <div v-else-if="entry.running" class="trace-waiting">等待工具返回…</div>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import {
  BulbOutlined,
  CheckCircleFilled,
  CloseCircleFilled,
  CopyOutlined,
  DownOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from '@ant-design/icons-vue'
import { toolLabel } from '@/utils/chatTools'
import { copyText } from '@/utils/clipboard'
import type { TimelineEntry } from '@/api'

const props = defineProps<{
  entries: TimelineEntry[]
  /** 这条气泡是否还在流式生成：进行中展开并转圈，落定后自动折叠。 */
  streaming: boolean
}>()

// 历史消息挂载时 streaming 即为 false，默认收起；新回答挂载时为 true，展开。
const open = ref(props.streaming)
watch(
  () => props.streaming,
  (streaming) => {
    open.value = streaming
  },
)

// timeline 里的 text 条目就是正文本身（已在气泡正文区渲染），confirm 条目是
// 写命令确认提议（由它自己的确认卡片呈现），这两类都不属于「思考过程」。
const visible = computed(() =>
  props.entries.filter((entry) => entry.type !== 'text' && entry.type !== 'confirm'),
)
const steps = computed(() =>
  visible.value.filter((entry): entry is TimelineEntry => entry.type === 'step'),
)
const stepCount = computed(() => steps.value.length)
const errorCount = computed(
  () => steps.value.filter((entry) => entry.is_error).length,
)
// 当前正在执行的那次调用：标题右侧跟着它的简短文案一起转。
const activeLabel = computed(() => {
  // 最后一个还在转圈的就是当前工具；倒序拷贝找，避免 findLast 的兼容面。
  const running = [...steps.value].reverse().find((entry) => entry.running)
  if (running) return toolLabel(running.tool, running.input)
  // 没有在跑的工具、但仍在流式：可能正处于推理或组织回答阶段。
  return ''
})

// 每个步骤卡片的展开状态：没手动点过就走默认——进行中展开看入参，流式中
// 最新一次返回也展开（看一眼结果）；回答落定后默认全部收起。
const toggled = reactive<Record<number, boolean>>({})
function toggleStep(index: number) {
  toggled[index] = !stepOpen(index, visible.value[index])
}
function stepOpen(index: number, entry: TimelineEntry): boolean {
  if (index in toggled) return toggled[index]
  if (entry.running) return true
  return (
    props.streaming &&
    entry.output != null &&
    steps.value[steps.value.length - 1] === entry
  )
}

function argsText(input: Record<string, unknown> | undefined): string {
  if (!input || !Object.keys(input).length) return ''
  try {
    return JSON.stringify(input, null, 2)
  } catch {
    return String(input)
  }
}

const copiedIndex = ref<number | null>(null)
let copiedTimer: ReturnType<typeof setTimeout> | undefined
async function copyOutput(index: number, output: string) {
  if (await copyText(output)) {
    copiedIndex.value = index
    if (copiedTimer) clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => {
      copiedIndex.value = null
    }, 1500)
  }
}
</script>

<style scoped>
.trace {
  margin-bottom: 8px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: var(--fs-sm);
  color: var(--text-3);
}
.trace-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-sm);
  line-height: 1.5;
  cursor: pointer;
}
.trace-head:hover {
  color: var(--text-2);
}
.trace-caret {
  font-size: 10px;
  transition: transform 0.18s ease;
  transform: rotate(-90deg);
}
.trace-caret.open {
  transform: rotate(0deg);
}
.trace-head-icon {
  color: var(--signal-text);
}
.trace-title {
  color: var(--text-2);
}
.trace-summary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  font-size: var(--fs-xs);
}
.trace-summary-errors,
.trace-error-text {
  color: var(--danger, #ff4d4f);
}
.trace-active {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-body {
  /* 长思维链 + 多轮工具可能很长：整体限高内部滚动，不把气泡撑出屏幕。 */
  max-height: 360px;
  overflow-y: auto;
  padding: 2px 10px 8px;
  border-top: 1px dashed var(--hairline);
}
.trace-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 6px 0 2px;
  font-size: var(--fs-xs);
}
/* 思维链用更弱一档的文字色，明确它不是正式回答。 */
.trace-reason-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-3);
  line-height: 1.6;
}
.trace-step {
  margin-top: 6px;
}
.trace-step-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 2px 0;
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}
.trace-step-head:hover {
  color: var(--text-2);
}
.trace-step-status {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  flex-shrink: 0;
}
.trace-step-done {
  color: var(--success);
}
.trace-step-error {
  color: var(--danger, #ff4d4f);
}
.trace-step-tool {
  flex-shrink: 0;
  font-size: var(--fs-xs);
}
.trace-step-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-step-caret {
  flex-shrink: 0;
  font-size: 10px;
  transition: transform 0.18s ease;
  transform: rotate(-90deg);
}
.trace-step-caret.open {
  transform: rotate(0deg);
}
.trace-step-detail {
  margin: 4px 0 4px 22px;
}
.trace-block + .trace-block {
  margin-top: 6px;
}
.trace-block-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2px;
  font-size: var(--fs-xs);
  color: var(--text-3);
}
.trace-copy {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 0 4px;
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.trace-copy:hover {
  color: var(--signal-text);
}
/* 入参/结果：等宽字体 + 自动换行；结果限高滚动，检索类工具动辄几千字。 */
.trace-code {
  margin: 0;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--surface-2, rgba(0, 0, 0, 0.04));
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: var(--fs-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-2);
}
.trace-code-output {
  max-height: 180px;
  overflow-y: auto;
}
.trace-code-error {
  color: var(--danger, #ff4d4f);
}
.trace-waiting {
  padding: 2px 0;
  font-size: var(--fs-xs);
  color: var(--text-3);
  opacity: 0.7;
}
</style>
