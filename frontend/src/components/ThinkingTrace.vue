<template>
  <!-- 对话气泡的过程组外壳：整组默认收成一行「执行工具 N 次，其中 M 次失败」，
       流式中展开并跟着报当前那一次调用。展开体是 TraceChain（ThoughtChain）。
       适配逻辑 chatTraceNodes 仍在 utils/traceModel.ts，工作流步骤体直接用
       TraceChain，不套这层壳。 -->
  <div v-if="nodes.length" class="tt">
    <button type="button" class="tt-head" @click="open = !open">
      <DownOutlined :class="['tt-caret', { open }]" />
      <ThunderboltOutlined class="tt-head-icon" />
      <span class="tt-label">{{ summaryText }}</span>
      <span v-if="streaming && activeLabel" class="tt-active" :title="activeLabel">
        · {{ activeLabel }}
      </span>
      <a-spin v-if="streaming" size="small" class="tt-spin" />
    </button>
    <div v-show="open" class="tt-body">
      <TraceChain :nodes="nodes" :streaming="streaming" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { DownOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import TraceChain from './trace/TraceChain.vue'
import { chatTraceNodes } from '@/utils/traceModel'
import type { TimelineEntry } from '@/api'

const props = defineProps<{
  entries: TimelineEntry[]
  /** 这条气泡是否还在流式生成：进行中默认展开并显示当前工具，落定后自动折叠。 */
  streaming?: boolean
}>()

const open = ref(!!props.streaming)
watch(
  () => props.streaming,
  (streaming) => {
    // 只在落定/报错（true→false）时强制折叠：这是「思考过程收不起来」的
    // 兜底，任何路径清掉流式标志都会收起。反向 false→true（流式中 section
    // 切换的抖动）不强行展开，避免覆盖用户正在看的手动折叠状态。
    if (!streaming) open.value = false
  },
)

const nodes = computed(() => chatTraceNodes(props.entries))
const toolNodes = computed(() => nodes.value.filter((node) => node.kind === 'tool'))
const toolCount = computed(() => toolNodes.value.length)
const errorCount = computed(
  () => toolNodes.value.filter((node) => node.status === 'error').length,
)

/** 摘要行文案：有工具就报次数，纯思考的组只说「思考过程」。 */
const summaryText = computed(() => {
  if (props.streaming) return '正在执行中'
  if (!toolCount.value) return '思考过程'
  return errorCount.value
    ? `执行工具 ${toolCount.value} 次，其中 ${errorCount.value} 次失败`
    : `执行工具 ${toolCount.value} 次`
})

// 正在执行的那次调用：摘要行后面跟着它的文案一起转；倒序找最后一个 running 的。
const activeLabel = computed(
  () => [...nodes.value].reverse().find((node) => node.status === 'running')?.action ?? '',
)
</script>

<style scoped>
/* AI 消息去气泡后，思考过程是内容区里唯一的容器：用弱填充而非纯白卡片，
   与无底色正文拉开层次，圆角放宽到 12px，贴近主流产品的思考卡观感。 */
.tt {
  margin-bottom: 10px;
  border: 1px solid var(--hairline);
  border-radius: 12px;
  background: var(--surface-2);
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
  border-radius: 12px;
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
.tt-label {
  color: var(--text-2);
}
.tt-active {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
}
.tt-spin {
  margin-left: 4px;
}
.tt-body {
  /* 高度跟随内容：内容少就收缩不留白，长到上限后内部滚动。代价是流式
     追加时框会先长高、封顶后才固定，下方消息在长高阶段会被轻微下推。 */
  max-height: 300px;
  overflow-y: auto;
  padding: 2px 10px 8px;
  border-top: 1px dashed var(--hairline);
}
@media (max-width: 768px) {
  .tt-body {
    max-height: 240px;
  }
}
</style>
