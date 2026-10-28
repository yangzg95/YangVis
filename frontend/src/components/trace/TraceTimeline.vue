<template>
  <!-- 统一 trace 的折叠外壳：一行「思考过程 / N 次工具调用」摘要，展开是 TraceNodeList。
       对话气泡用它；工作流步骤体直接用 TraceNodeList（步骤本身已是容器，不再套一层）。 -->
  <div v-if="nodes.length" class="tt">
    <button type="button" class="tt-head" @click="open = !open">
      <DownOutlined :class="['tt-caret', { open }]" />
      <ThunderboltOutlined class="tt-head-icon" />
      <span class="tt-title">{{ title }}</span>
      <span class="tt-summary">
        <a-spin v-if="streaming && activeLabel" size="small" />
        <span v-if="streaming && activeLabel" class="tt-active" :title="activeLabel">
          {{ activeLabel }}
        </span>
        <span v-else-if="streaming">思考中…</span>
        <template v-else>
          <span v-if="toolCount">{{ toolCount }} 次工具调用</span>
          <span v-else>已完成</span>
          <span v-if="errorCount" class="tt-errors">（{{ errorCount }} 次失败）</span>
        </template>
      </span>
    </button>
    <div v-show="open" class="tt-body">
      <TraceNodeList :nodes="nodes" :streaming="streaming" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { DownOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import TraceNodeList from './TraceNodeList.vue'
import type { TraceNode } from '@/utils/traceModel'

const props = withDefaults(
  defineProps<{
    nodes: TraceNode[]
    /** 是否还在流式生成：进行中默认展开并显示当前工具，落定后自动折叠。 */
    streaming?: boolean
    title?: string
  }>(),
  { streaming: false, title: '思考过程' },
)

const open = ref(props.streaming)
watch(
  () => props.streaming,
  (streaming) => {
    open.value = streaming
  },
)

const toolNodes = computed(() => props.nodes.filter((node) => node.kind === 'tool'))
const toolCount = computed(() => toolNodes.value.length)
const errorCount = computed(
  () => toolNodes.value.filter((node) => node.status === 'error').length,
)
// 正在执行的那次调用：标题右侧跟着它的文案一起转；倒序找最后一个 running 的。
const activeLabel = computed(() =>
  [...props.nodes].reverse().find((node) => node.status === 'running')?.title ?? '',
)
</script>

<style scoped>
.tt {
  margin-bottom: 8px;
  border: 1px solid var(--hairline);
  border-radius: var(--radius-sm);
  background: var(--surface);
  font-size: var(--fs-sm);
  color: var(--text-3);
}
.tt-head {
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
.tt-head:hover {
  color: var(--text-2);
}
.tt-caret {
  font-size: 10px;
  transition: transform 0.18s ease;
  transform: rotate(-90deg);
}
.tt-caret.open {
  transform: rotate(0deg);
}
.tt-head-icon {
  color: var(--signal-text);
}
.tt-title {
  color: var(--text-2);
}
.tt-summary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  font-size: var(--fs-xs);
}
.tt-errors {
  color: var(--danger);
}
.tt-active {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tt-body {
  /* 长思维链 + 多轮工具可能很长：整体限高内部滚动，不把气泡撑出屏幕。 */
  max-height: 360px;
  overflow-y: auto;
  padding: 2px 10px 8px;
  border-top: 1px dashed var(--hairline);
}
</style>
