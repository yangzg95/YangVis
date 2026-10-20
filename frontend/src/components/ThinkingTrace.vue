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
        <!-- 工具调用：进行中转圈，返回后打勾；标题取自 utils/chatTools 的统一映射。 -->
        <div v-else class="trace-step">
          <span class="trace-step-status">
            <a-spin v-if="entry.running" size="small" />
            <CheckCircleFilled v-else class="trace-step-done" />
          </span>
          <ToolOutlined class="trace-step-tool" />
          <span class="trace-step-text" :title="toolLabel(entry.tool, entry.input)">
            {{ toolLabel(entry.tool, entry.input) }}
          </span>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  BulbOutlined,
  CheckCircleFilled,
  DownOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from '@ant-design/icons-vue'
import { toolLabel } from '@/utils/chatTools'
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

// timeline 里的 text 条目就是正文本身（已在气泡正文区渲染），时间线只呈现
// 思考与工具调用，跳过它们。
const visible = computed(() =>
  props.entries.filter((entry) => entry.type !== 'text'),
)
const stepCount = computed(
  () => visible.value.filter((entry) => entry.type === 'step').length,
)
// 当前正在执行的那次调用：标题右侧跟着它的简短文案一起转。
const activeLabel = computed(() => {
  const steps = visible.value.filter((entry) => entry.type === 'step')
  // 最后一个还在转圈的就是当前工具；倒序拷贝找，避免 findLast 的兼容面。
  const running = [...steps].reverse().find((entry) => entry.running)
  if (running) return toolLabel(running.tool, running.input)
  // 没有在跑的工具、但仍在流式：可能正处于推理或组织回答阶段。
  return ''
})
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
.trace-active {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-body {
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
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
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
.trace-step-tool {
  flex-shrink: 0;
  font-size: var(--fs-xs);
}
.trace-step-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
