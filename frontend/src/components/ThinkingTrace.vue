<template>
  <!-- 对话气泡的「思考过程」：把 chat timeline 适配成统一 trace 节点后交给统一组件。
       适配逻辑（含 text/confirm 过滤、llm 节点）都在 utils/traceModel.ts，这里只转发。 -->
  <TraceTimeline :nodes="nodes" :streaming="streaming" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import TraceTimeline from './trace/TraceTimeline.vue'
import { chatTraceNodes } from '@/utils/traceModel'
import type { TimelineEntry } from '@/api'

const props = defineProps<{
  entries: TimelineEntry[]
  /** 这条气泡是否还在流式生成：进行中展开并转圈，落定后自动折叠。 */
  streaming: boolean
}>()

const nodes = computed(() => chatTraceNodes(props.entries))
</script>
